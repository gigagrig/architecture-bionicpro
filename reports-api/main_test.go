package main

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

func fixture(t *testing.T, missing bool) (*server, func(jwt.MapClaims, jwt.SigningMethod) string, *int) {
	t.Helper()
	private, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": []any{map[string]string{
			"kid": "test", "kty": "RSA", "alg": "RS256", "use": "sig",
			"n": base64.RawURLEncoding.EncodeToString(private.N.Bytes()),
			"e": base64.RawURLEncoding.EncodeToString(big.NewInt(int64(private.E)).Bytes()),
		}}})
	}))
	t.Cleanup(jwks.Close)
	calls := new(int)
	db := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		(*calls)++
		body, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(body), "FROM reporting.cdc_report_mart") {
			t.Error("API must read the CDC mart")
		}
		if r.URL.Query().Get("param_subject") != "owner-a" {
			t.Error("query not scoped to signed subject")
		}
		if !strings.Contains(r.URL.Query().Get("param_batches"), "00000000-0000-0000-0000-000000000002") {
			t.Error("batch version not pinned")
		}
		_, _ = w.Write([]byte(`{"data":[{"day":"2026-09-28","prosthesis_id":"mine","samples":3,"movements":30,"errors":1,"avg_response_ms":90.0}]}`))
	}))
	t.Cleanup(db.Close)
	s := &server{auth: &verifier{issuer: "https://issuer", jwksURL: jwks.URL, client: jwks.Client()}, db: &clickhouse{endpoint: db.URL, client: db.Client()}}
	sign := func(overrides jwt.MapClaims, method jwt.SigningMethod) string {
		c := jwt.MapClaims{"iss": "https://issuer", "aud": "reports-api", "sub": "owner-a", "azp": "bionicpro-auth", "iat": time.Now().Unix(), "exp": time.Now().Add(time.Minute).Unix()}
		for k, v := range overrides {
			c[k] = v
		}
		token := jwt.NewWithClaims(method, c)
		token.Header["kid"] = "test"
		var key any = private
		if method.Alg() == "HS256" {
			key = []byte("bad-key")
		}
		value, err := token.SignedString(key)
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	catalog := []period{{Day: "2026-09-28", Batch: "00000000-0000-0000-0000-000000000001"}, {Day: "2026-09-29", Batch: "00000000-0000-0000-0000-000000000002"}}
	if missing {
		catalog[1] = period{Day: "2026-09-30", Batch: "00000000-0000-0000-0000-000000000003"}
	}
	s.store = &memoryStore{periods: catalog, objects: map[string][]byte{}}
	s.linkKey = []byte("test-private-link-secret")
	s.slots = make(chan struct{}, 8)
	return s, sign, calls
}

func request(s *server, token, query string) *httptest.ResponseRecorder {
	r := httptest.NewRequest("GET", "/reports?"+query, nil)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	s.reports(w, r)
	return w
}

func TestTokenValidation(t *testing.T) {
	s, sign, calls := fixture(t, false)
	for _, test := range []struct {
		name   string
		fields jwt.MapClaims
		method jwt.SigningMethod
	}{
		{"expired", jwt.MapClaims{"exp": time.Now().Add(-time.Second).Unix()}, jwt.SigningMethodRS256},
		{"issuer", jwt.MapClaims{"iss": "other"}, jwt.SigningMethodRS256},
		{"audience", jwt.MapClaims{"aud": "other"}, jwt.SigningMethodRS256},
		{"client", jwt.MapClaims{"azp": "other"}, jwt.SigningMethodRS256},
		{"no-subject", jwt.MapClaims{"sub": ""}, jwt.SigningMethodRS256},
		{"no-exp", jwt.MapClaims{"exp": nil}, jwt.SigningMethodRS256},
		{"no-issued", jwt.MapClaims{"iat": nil}, jwt.SigningMethodRS256},
		{"future-issued", jwt.MapClaims{"iat": time.Now().Add(time.Hour).Unix()}, jwt.SigningMethodRS256},
		{"algorithm", nil, jwt.SigningMethodHS256},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := request(s, sign(test.fields, test.method), "from=2026-09-28&to=2026-09-30"); got.Code != 401 {
				t.Fatalf("got %d", got.Code)
			}
		})
	}
	if got := request(s, "", "from=2026-09-28&to=2026-09-30"); got.Code != 401 {
		t.Fatal(got.Code)
	}
	valid := sign(nil, jwt.SigningMethodRS256)
	parts := strings.Split(valid, ".")
	parts[1] = base64.RawURLEncoding.EncodeToString([]byte(`{"iss":"https://issuer","aud":"reports-api","sub":"owner-b","azp":"bionicpro-auth","exp":9999999999,"iat":1}`))
	if got := request(s, strings.Join(parts, "."), "from=2026-09-28&to=2026-09-30"); got.Code != 401 {
		t.Fatal("tampered token accepted")
	}
	if *calls != 0 {
		t.Fatal("unauthenticated request accessed ClickHouse")
	}
}

func TestOwnerAndPinnedBatches(t *testing.T) {
	s, sign, calls := fixture(t, false)
	got := request(s, sign(nil, jwt.SigningMethodRS256), "from=2026-09-28&to=2026-09-30")
	if got.Code != 200 || !strings.Contains(got.Body.String(), `"download_url":"/cdn/`) || *calls != 1 {
		t.Fatalf("status=%d body=%s calls=%d", got.Code, got.Body, *calls)
	}
	if got.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("missing cache policy")
	}
}

func TestGapRefusesPartialReport(t *testing.T) {
	s, sign, calls := fixture(t, true)
	got := request(s, sign(nil, jwt.SigningMethodRS256), "from=2026-09-28&to=2026-10-01")
	if got.Code != 409 || !strings.Contains(got.Body.String(), "2026-09-29") || *calls != 0 {
		t.Fatalf("%d %s", got.Code, got.Body)
	}
}

func TestInvalidPeriodAndOwnerOverride(t *testing.T) {
	s, sign, calls := fixture(t, false)
	for _, query := range []string{
		"from=2026-09-28&to=2026-09-30&user_id=owner-b",
		"from=2026-09-28&to=2026-09-30&subject=owner-b",
		"from=2026-09-28&from=2026-09-29&to=2026-09-30",
		"from=2026-09-28&to=2026-09-28", "from=2026-09-30&to=2026-09-28",
		"from=2026-01-01&to=2026-09-30", "from=2026-09-28%27&to=2026-09-30", "",
	} {
		if got := request(s, sign(nil, jwt.SigningMethodRS256), query); got.Code != 400 {
			t.Fatalf("query=%s got=%d", query, got.Code)
		}
	}
	if *calls != 0 {
		t.Fatal("invalid request accessed ClickHouse")
	}
}
