// reports-api reads published daily reports. OAuth tokens arrive only from the BFF.
package main

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"log"
	"math/big"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/sync/singleflight"
)

type claims struct {
	jwt.RegisteredClaims
	AuthorizedParty string `json:"azp"`
}

type verifier struct {
	issuer, jwksURL string
	client          *http.Client
	mu              sync.Mutex
	keys            map[string]*rsa.PublicKey
	updated         time.Time
}

func (v *verifier) key(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if k := v.keys[kid]; k != nil && time.Since(v.updated) < 5*time.Minute {
		return k, nil
	}
	// Bound repeated unknown-kid requests without delaying normal cached verification.
	if time.Since(v.updated) < 5*time.Second {
		return nil, errors.New("unknown key")
	}
	req, err := http.NewRequestWithContext(ctx, "GET", v.jwksURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Forwarded-Proto", "https")
	res, err := v.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return nil, errors.New("JWKS unavailable")
	}
	var source struct {
		Keys []struct{ Kid, Kty, Alg, Use, N, E string } `json:"keys"`
	}
	if err = json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&source); err != nil {
		return nil, err
	}
	keys := make(map[string]*rsa.PublicKey)
	for _, key := range source.Keys {
		if key.Kty != "RSA" || (key.Alg != "" && key.Alg != "RS256") || (key.Use != "" && key.Use != "sig") {
			continue
		}
		n, errN := base64.RawURLEncoding.DecodeString(key.N)
		e, errE := base64.RawURLEncoding.DecodeString(key.E)
		if errN != nil || errE != nil || len(n) < 256 || len(e) > 4 {
			continue
		}
		exponent := new(big.Int).SetBytes(e).Int64()
		if exponent < 3 || exponent%2 == 0 {
			continue
		}
		keys[key.Kid] = &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(exponent)}
	}
	v.keys, v.updated = keys, time.Now()
	if key := keys[kid]; key != nil {
		return key, nil
	}
	return nil, errors.New("unknown key")
}

func (v *verifier) subject(r *http.Request) (string, error) {
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") || len(header) > 16384 {
		return "", errors.New("bearer required")
	}
	c := &claims{}
	token, err := jwt.ParseWithClaims(strings.TrimPrefix(header, "Bearer "), c, func(t *jwt.Token) (any, error) {
		kid, ok := t.Header["kid"].(string)
		if !ok || kid == "" {
			return nil, errors.New("kid required")
		}
		return v.key(r.Context(), kid)
	}, jwt.WithValidMethods([]string{"RS256"}), jwt.WithIssuer(v.issuer),
		jwt.WithAudience("reports-api"), jwt.WithExpirationRequired(), jwt.WithIssuedAt())
	if err != nil || !token.Valid || c.Subject == "" || c.IssuedAt == nil || c.AuthorizedParty != "bionicpro-auth" {
		return "", errors.New("invalid token")
	}
	return c.Subject, nil
}

type clickhouse struct {
	endpoint, user, password string
	client                   *http.Client
}

func (c *clickhouse) query(ctx context.Context, sql string, params url.Values, target any) error {
	u, err := url.Parse(c.endpoint)
	if err != nil {
		return err
	}
	q := u.Query()
	for k, values := range params {
		for _, value := range values {
			q.Add("param_"+k, value)
		}
	}
	q.Set("wait_end_of_query", "1")
	u.RawQuery = q.Encode()
	req, err := http.NewRequestWithContext(ctx, "POST", u.String(), strings.NewReader(sql+" FORMAT JSON"))
	if err != nil {
		return err
	}
	req.SetBasicAuth(c.user, c.password)
	res, err := c.client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		return errors.New("ClickHouse query failed")
	}
	return json.NewDecoder(io.LimitReader(res.Body, 8<<20)).Decode(target)
}

type period struct {
	Day       string `json:"day"`
	Batch     string `json:"batch_id"`
	Published string `json:"published_at"`
}
type row struct {
	Day         string   `json:"day"`
	Prosthesis  string   `json:"prosthesis_id"`
	Model       string   `json:"model"`
	Samples     uint64   `json:"samples"`
	Movements   uint64   `json:"movements"`
	Errors      uint64   `json:"errors"`
	AvgResponse *float64 `json:"avg_response_ms"`
	MaxResponse *float64 `json:"max_response_ms"`
	MinBattery  *float64 `json:"min_battery_pct"`
}
type server struct {
	auth    *verifier
	db      *clickhouse
	store   reportStore
	linkKey []byte
	flights singleflight.Group
	slots   chan struct{}
}

func respond(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (s *server) reports(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	r = r.WithContext(ctx)
	subject, err := s.auth.subject(r)
	if err != nil {
		respond(w, 401, map[string]string{"detail": "Требуется вход"})
		return
	}
	if r.Method != "GET" {
		respond(w, 405, map[string]string{"detail": "Используйте GET"})
		return
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		respond(w, 400, map[string]string{"detail": "Некорректные параметры"})
		return
	}
	for k, values := range q {
		if (k != "from" && k != "to") || len(values) != 1 {
			respond(w, 400, map[string]string{"detail": "Допустимы только from и to; пользователь определяется сессией"})
			return
		}
	}
	start, e1 := time.Parse("2006-01-02", q.Get("from"))
	end, e2 := time.Parse("2006-01-02", q.Get("to"))
	if e1 != nil || e2 != nil || !end.After(start) || end.Sub(start) > 31*24*time.Hour {
		respond(w, 400, map[string]string{"detail": "Задайте даты YYYY-MM-DD, from < to, не более 31 дня; to не включается"})
		return
	}
	params := url.Values{"from": {q.Get("from")}, "to": {q.Get("to")}}
	var ready struct {
		Data []period `json:"data"`
	}
	ready.Data, err = s.store.catalog(r.Context())
	if err != nil {
		respond(w, 503, map[string]string{"detail": "Хранилище отчётов временно недоступно"})
		return
	}
	byDay := make(map[string]period)
	for _, p := range ready.Data {
		byDay[p.Day] = p
	}
	selected := []period{}
	missing, batches := []string{}, []string{}
	for d := start; d.Before(end); d = d.AddDate(0, 0, 1) {
		if p, ok := byDay[d.Format("2006-01-02")]; ok {
			batches = append(batches, p.Batch)
			selected = append(selected, p)
		} else {
			missing = append(missing, d.Format("2006-01-02"))
		}
	}
	if len(missing) > 0 {
		respond(w, 409, map[string]any{"detail": "Данные за часть периода ещё не подготовлены", "missing_days": missing})
		return
	}
	// Pin immutable batches from the manifest: a concurrent ETL publication cannot
	// change the version between readiness verification and reading the report.
	// HTTP Array(UUID) parameters use ClickHouse's quoted text format, not JSON.
	// Values come from UUID columns and still pass through the typed parameter parser.
	params.Set("batches", "['"+strings.Join(batches, "','")+"']")
	params.Set("subject", subject)
	key := s.objectKey(subject, q.Get("from"), q.Get("to"), selected)
	err = s.ensureReport(r.Context(), key, params, selected)
	if err != nil {
		respond(w, 503, map[string]string{"detail": "Хранилище отчётов временно недоступно"})
		return
	}
	expires := time.Now().Add(5 * time.Minute).Unix()
	respond(w, 200, map[string]any{"from": q.Get("from"), "to": q.Get("to"), "timezone": "UTC",
		"download_url": s.downloadURL(key, expires), "expires_at": expires})
}

func required(name string) string {
	v := os.Getenv(name)
	if v == "" {
		log.Fatalf("Required setting: %s", name)
	}
	return v
}

func main() {
	client := &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	storage, err := newS3Store(required("S3_ENDPOINT"), required("S3_ACCESS_KEY"), required("S3_SECRET_KEY"), required("S3_BUCKET"))
	if err != nil {
		log.Fatal("Invalid S3 configuration")
	}
	s := &server{auth: &verifier{issuer: required("OIDC_ISSUER"), jwksURL: required("OIDC_JWKS_URL"), client: client},
		db:    &clickhouse{endpoint: required("CLICKHOUSE_URL"), user: "reports_api", password: required("CLICKHOUSE_REPORTS_PASSWORD"), client: client},
		store: storage, linkKey: []byte(required("REPORT_LINK_KEY")), slots: make(chan struct{}, 8)}
	mux := http.NewServeMux()
	mux.HandleFunc("/reports", s.reports)
	mux.HandleFunc("/cdn-authorize", s.authorizeDownload)
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) { respond(w, 200, map[string]string{"status": "ok"}) })
	httpServer := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 60 * time.Second}
	log.Print("reports-api listening on :8080")
	log.Fatal(httpServer.ListenAndServe())
}
