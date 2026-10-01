package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type reportStore interface {
	catalog(context.Context) ([]period, error)
	exists(context.Context, string) (bool, error)
	put(context.Context, string, []byte) error
	presign(context.Context, string) (string, error)
}

type s3Store struct {
	client *minio.Client
	bucket string
}

func newS3Store(endpoint, access, secret, bucket string) (*s3Store, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.Path != "" {
		return nil, errors.New("invalid S3 endpoint")
	}
	c, err := minio.New(u.Host, &minio.Options{Creds: credentials.NewStaticV4(access, secret, ""),
		Secure: u.Scheme == "https", Region: "us-east-1", BucketLookup: minio.BucketLookupPath})
	if err != nil {
		return nil, err
	}
	return &s3Store{client: c, bucket: bucket}, nil
}

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func (s *s3Store) catalog(ctx context.Context) ([]period, error) {
	object, err := s.client.GetObject(ctx, s.bucket, "catalog/current.json", minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	defer object.Close()
	var catalog struct {
		Schema  int      `json:"schema"`
		Periods []period `json:"periods"`
	}
	if err = json.NewDecoder(io.LimitReader(object, 1<<20)).Decode(&catalog); err != nil {
		return nil, err
	}
	if catalog.Schema != 1 {
		return nil, errors.New("unknown catalog schema")
	}
	seen := make(map[string]bool)
	for _, p := range catalog.Periods {
		_, err := time.Parse("2006-01-02", p.Day)
		if err != nil || !uuidPattern.MatchString(p.Batch) || seen[p.Day] {
			return nil, errors.New("invalid catalog")
		}
		seen[p.Day] = true
	}
	return catalog.Periods, nil
}

func (s *s3Store) exists(ctx context.Context, key string) (bool, error) {
	_, err := s.client.StatObject(ctx, s.bucket, key, minio.StatObjectOptions{})
	if err == nil {
		return true, nil
	}
	if minio.ToErrorResponse(err).Code == "NoSuchKey" {
		return false, nil
	}
	return false, err // Permission errors/outages must never trigger OLAP generation.
}

func (s *s3Store) put(ctx context.Context, key string, data []byte) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, bytes.NewReader(data), int64(len(data)),
		minio.PutObjectOptions{ContentType: "application/json", CacheControl: "max-age=86400, immutable",
			ContentDisposition: `attachment; filename="bionicpro-report.json"`, DisableMultipart: true})
	return err
}

func (s *s3Store) presign(ctx context.Context, key string) (string, error) {
	u, err := s.client.PresignedGetObject(ctx, s.bucket, key, time.Minute, nil)
	if err != nil {
		return "", err
	}
	return u.String(), nil
}

func (s *server) mac(value string) string {
	h := hmac.New(sha256.New, s.linkKey)
	_, _ = h.Write([]byte(value))
	return hex.EncodeToString(h.Sum(nil))
}

func (s *server) objectKey(subject, from, to string, periods []period) string {
	versions, _ := json.Marshal(periods)
	digest := sha256.Sum256(versions)
	return "reports/v1/" + s.mac("owner:"+subject) + "/" + from + "_" + to + "/" + hex.EncodeToString(digest[:]) + ".json"
}

func (s *server) downloadURL(key string, expires int64) string {
	path := "/cdn/" + key
	exp := strconv.FormatInt(expires, 10)
	return path + "?expires=" + exp + "&signature=" + s.mac("download:"+path+":"+exp)
}

func (s *server) ensureReport(ctx context.Context, key string, params url.Values, periods []period) error {
	found, err := s.store.exists(ctx, key)
	if err != nil || found {
		return err
	}
	// Coalesce simultaneous misses per immutable object in this API instance.
	// The bounded background task survives one caller disconnecting; others can wait.
	result := s.flights.DoChan(key, func() (any, error) {
		work, cancel := context.WithTimeout(context.Background(), 25*time.Second)
		defer cancel()
		select {
		case s.slots <- struct{}{}:
			defer func() { <-s.slots }()
		case <-work.Done():
			return nil, work.Err()
		}
		found, err := s.store.exists(work, key)
		if err != nil || found {
			return nil, err
		}
		var data struct {
			Data []row `json:"data"`
		}
		err = s.db.query(work, `SELECT day, prosthesis_id, model, samples, movements, errors,
			avg_response_ms, max_response_ms, min_battery_pct FROM reporting.report_mart
			WHERE subject = {subject:String} AND day >= {from:Date} AND day < {to:Date}
			AND batch_id IN {batches:Array(UUID)} ORDER BY day, prosthesis_id`, params, &data)
		if err != nil {
			return nil, err
		}
		if data.Data == nil {
			data.Data = []row{}
		}
		body, err := json.Marshal(map[string]any{"from": params.Get("from"), "to": params.Get("to"),
			"timezone": "UTC", "periods": periods, "rows": data.Data})
		if err != nil {
			return nil, err
		}
		return nil, s.store.put(work, key, body)
	})
	select {
	case res := <-result:
		return res.Err
	case <-ctx.Done():
		return ctx.Err()
	}
}

var downloadPath = regexp.MustCompile(`^/cdn/reports/v1/([0-9a-f]{64})/[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9a-f]{64}\.json$`)

func (s *server) authorizeDownload(w http.ResponseWriter, r *http.Request) {
	subject, err := s.auth.subject(r)
	if err != nil {
		respond(w, 401, map[string]string{"detail": "Требуется вход"})
		return
	}
	if r.Method != "GET" {
		respond(w, 405, map[string]string{"detail": "Используйте GET"})
		return
	}
	uri, err := url.ParseRequestURI(r.Header.Get("X-Original-URI"))
	if err != nil {
		respond(w, 403, nil)
		return
	}
	match := downloadPath.FindStringSubmatch(uri.Path)
	q, err := url.ParseQuery(uri.RawQuery)
	exp, expErr := strconv.ParseInt(q.Get("expires"), 10, 64)
	now := time.Now().Unix()
	if err != nil || expErr != nil || len(match) != 2 || uri.RawPath != "" || len(q) != 2 ||
		len(q["expires"]) != 1 || len(q["signature"]) != 1 || exp <= now || exp > now+300 ||
		!hmac.Equal([]byte(match[1]), []byte(s.mac("owner:"+subject))) ||
		!hmac.Equal([]byte(q.Get("signature")), []byte(s.mac("download:"+uri.Path+":"+q.Get("expires")))) {
		respond(w, 403, nil)
		return
	}
	origin, err := s.store.presign(r.Context(), strings.TrimPrefix(uri.Path, "/cdn/"))
	if err != nil {
		respond(w, 503, nil)
		return
	}
	// This private header is consumed by Nginx's auth subrequest, never by the browser.
	w.Header().Set("X-Report-Origin", origin)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(204)
}
