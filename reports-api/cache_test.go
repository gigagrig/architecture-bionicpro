package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type memoryStore struct {
	mu      sync.Mutex
	periods []period
	objects map[string][]byte
	failure error
	writes  int
}

func (s *memoryStore) catalog(context.Context) ([]period, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]period{}, s.periods...), s.failure
}
func (s *memoryStore) exists(_ context.Context, key string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.objects[key]
	return ok, s.failure
}
func (s *memoryStore) put(_ context.Context, key string, data []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failure != nil {
		return s.failure
	}
	s.objects[key] = data
	s.writes++
	return nil
}
func (s *memoryStore) presign(_ context.Context, key string) (string, error) {
	return "http://minio:9000/bionicpro-reports/" + key + "?private-signature=test", nil
}

func reportLink(t *testing.T, w *httptest.ResponseRecorder) string {
	t.Helper()
	if w.Code != 200 {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	var result struct {
		URL string `json:"download_url"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	return result.URL
}

func authorize(s *server, token, uri string) *httptest.ResponseRecorder {
	r := httptest.NewRequest("GET", "/cdn-authorize", nil)
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("X-Original-URI", uri)
	w := httptest.NewRecorder()
	s.authorizeDownload(w, r)
	return w
}

func TestRepeatUsesS3AndPublicationChangesPath(t *testing.T) {
	s, sign, calls := fixture(t, false)
	token := sign(nil, jwt.SigningMethodRS256)
	query := "from=2026-09-28&to=2026-09-30"
	first := reportLink(t, request(s, token, query))
	second := reportLink(t, request(s, token, query))
	if first != second || *calls != 1 {
		t.Fatalf("repeated generation: calls=%d", *calls)
	}
	store := s.store.(*memoryStore)
	store.mu.Lock()
	store.periods[0].Batch = "00000000-0000-0000-0000-000000000009"
	store.mu.Unlock()
	updated := reportLink(t, request(s, token, query))
	if strings.Split(first, "?")[0] == strings.Split(updated, "?")[0] || *calls != 2 || store.writes != 2 {
		t.Fatal("new publication reused old file")
	}
	if strings.Contains(first, "owner-a") {
		t.Fatal("subject leaked into path")
	}
	for _, data := range store.objects {
		if !strings.Contains(string(data), `"prosthesis_id":"mine"`) {
			t.Fatal("report content not saved")
		}
	}
}

func TestConcurrentMissIsGeneratedOnce(t *testing.T) {
	s, sign, calls := fixture(t, false)
	token := sign(nil, jwt.SigningMethodRS256)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if w := request(s, token, "from=2026-09-28&to=2026-09-30"); w.Code != 200 {
				t.Errorf("status %d", w.Code)
			}
		}()
	}
	wg.Wait()
	if *calls != 1 || s.store.(*memoryStore).writes != 1 {
		t.Fatal("misses were not coalesced")
	}
}

func TestLinksRequireOwnerSignatureAndExpiry(t *testing.T) {
	s, sign, calls := fixture(t, false)
	owner := sign(nil, jwt.SigningMethodRS256)
	link := reportLink(t, request(s, owner, "from=2026-09-28&to=2026-09-30"))
	if w := authorize(s, owner, link); w.Code != 204 || !strings.HasPrefix(w.Header().Get("X-Report-Origin"), "http://minio:9000/") {
		t.Fatalf("authorization %d", w.Code)
	}
	u, _ := url.Parse(link)
	key := strings.TrimPrefix(u.Path, "/cdn/")
	for _, test := range []struct{ token, uri string }{
		{"", link}, {sign(jwt.MapClaims{"sub": "owner-b"}, jwt.SigningMethodRS256), link},
		{owner, s.downloadURL(key, time.Now().Add(-time.Second).Unix())},
		{owner, s.downloadURL(key, time.Now().Add(time.Hour).Unix())},
		{owner, link + "&signature=duplicate"}, {owner, link + "&extra=1"},
		{owner, strings.Replace(link, "signature=", "signature=0", 1)},
		{owner, strings.Replace(link, "/cdn/", "/cdn/%72", 1)},
	} {
		if w := authorize(s, test.token, test.uri); w.Code != 401 && w.Code != 403 {
			t.Fatalf("bad link accepted %d", w.Code)
		}
	}
	if *calls != 1 {
		t.Fatal("download authorization accessed OLAP")
	}
}

func TestStorageErrorDoesNotFallBackToOLAP(t *testing.T) {
	s, sign, calls := fixture(t, false)
	s.store.(*memoryStore).failure = errors.New("S3 unavailable")
	if w := request(s, sign(nil, jwt.SigningMethodRS256), "from=2026-09-28&to=2026-09-30"); w.Code != 503 {
		t.Fatal(w.Code)
	}
	if *calls != 0 {
		t.Fatal("S3 error increased OLAP load")
	}
}

func TestS3DistinguishesMissingFromDenied(t *testing.T) {
	status := http.StatusNotFound
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "HEAD" {
			t.Errorf("expected HEAD, got %s", r.Method)
		}
		w.WriteHeader(status)
	}))
	defer origin.Close()
	store, err := newS3Store(origin.URL, "api", "secret", "reports")
	if err != nil {
		t.Fatal(err)
	}
	found, err := store.exists(context.Background(), "reports/test.json")
	if err != nil || found {
		t.Fatalf("missing object: found=%v err=%v", found, err)
	}
	status = http.StatusForbidden
	found, err = store.exists(context.Background(), "reports/test.json")
	if err == nil || found {
		t.Fatal("denied request treated as a cache miss")
	}
}
