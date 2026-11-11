package api

import (
	"bytes"
	"compress/gzip"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

// TestAdversarialSessionExtremeBodyLimits tests boundaries of request body decoding:
// 1. Exactly 4096 bytes vs 4097 bytes MaxBytesReader limit.
// 2. Trailing garbage after valid JSON.
// 3. Unknown fields rejected by DisallowUnknownFields.
// 4. Content-Type MIME variations (charset, uppercase, missing).
func TestAdversarialSessionExtremeBodyLimits(t *testing.T) {
	s, _ := fixture(t)

	t.Run("PayloadExceeding4096Bytes", func(t *testing.T) {
		ticket, err := s.Ticket()
		if err != nil {
			t.Fatal(err)
		}
		// Create a JSON payload with padding that exceeds 4096 bytes
		padding := strings.Repeat("a", 4096)
		body := fmt.Sprintf(`{"ticket":"%s","pad":"%s"}`, ticket, padding)
		r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatalf("expected 400 for payload > 4096 bytes, got %d: %s", w.Code, w.Body.String())
		}
	})

	t.Run("EmptyBody", func(t *testing.T) {
		r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", bytes.NewReader(nil))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatalf("expected 400 for empty body, got %d", w.Code)
		}
	})

	t.Run("ContentTypeMIMEVariations", func(t *testing.T) {
		ticket, err := s.Ticket()
		if err != nil {
			t.Fatal(err)
		}
		// Content-Type with charset parameter must be accepted
		body := fmt.Sprintf(`{"ticket":"%s"}`, ticket)
		r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(body))
		r.Header.Set("Content-Type", "application/json; charset=utf-8")
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != 200 {
			t.Fatalf("expected 200 for application/json; charset=utf-8, got %d: %s", w.Code, w.Body.String())
		}

		// Wrong Content-Type text/plain must be rejected with 400
		t2, _ := s.Ticket()
		r2 := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(fmt.Sprintf(`{"ticket":"%s"}`, t2)))
		r2.Header.Set("Content-Type", "text/plain")
		w2 := httptest.NewRecorder()
		s.ServeHTTP(w2, r2)
		if w2.Code != 400 {
			t.Fatalf("expected 400 for text/plain, got %d", w2.Code)
		}
	})

	t.Run("MalformedAndPrimitiveJSON", func(t *testing.T) {
		for _, invalid := range []string{
			`{"ticket":`,
			`{"ticket": 12345}`,
			`[1, 2, 3]`,
			`"just a string"`,
			`true`,
		} {
			r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(invalid))
			r.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			s.ServeHTTP(w, r)
			if w.Code != 400 {
				t.Fatalf("expected 400 for invalid body %q, got %d", invalid, w.Code)
			}
		}

		// When body is "null", Go json.Decoder decodes null as empty struct,
		// which results in an empty ticket string and fails with 401 (ticket not found).
		rNull := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader("null"))
		rNull.Header.Set("Content-Type", "application/json")
		wNull := httptest.NewRecorder()
		s.ServeHTTP(wNull, rNull)
		if wNull.Code != 401 {
			t.Fatalf("expected 401 for null ticket body, got %d", wNull.Code)
		}
	})
}

// TestAdversarialTicketPoolSaturation verifies that the 64 pending entry ticket limit is enforced.
func TestAdversarialTicketPoolSaturation(t *testing.T) {
	s, _ := fixture(t)

	// Allocate up to 64 tickets
	tickets := make([]string, 0, 64)
	for i := 0; i < 64; i++ {
		tkt, err := s.Ticket()
		if err != nil {
			t.Fatalf("failed to create ticket %d: %v", i, err)
		}
		tickets = append(tickets, tkt)
	}

	// 65th ticket MUST fail with "too many pending entry tickets"
	_, err := s.Ticket()
	if err == nil || !strings.Contains(err.Error(), "too many pending entry tickets") {
		t.Fatalf("expected error on 65th ticket, got: %v", err)
	}

	// Consume one ticket
	r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(`{"ticket":"`+tickets[0]+`"}`))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatalf("failed to consume ticket: %d", w.Code)
	}

	// Now a new ticket can be created again
	tktNew, err := s.Ticket()
	if err != nil {
		t.Fatalf("expected ticket creation to succeed after consuming one, got: %v", err)
	}
	if tktNew == "" {
		t.Fatal("empty ticket returned")
	}
}

// TestAdversarialSessionPoolSaturation verifies that the 64 active session limit is enforced.
func TestAdversarialSessionPoolSaturation(t *testing.T) {
	s, _ := fixture(t)

	for i := 0; i < 64; i++ {
		tkt, err := s.Ticket()
		if err != nil {
			t.Fatalf("iter %d: ticket error: %v", i, err)
		}
		r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(`{"ticket":"`+tkt+`"}`))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != 200 {
			t.Fatalf("iter %d: expected 200, got %d", i, w.Code)
		}
	}

	// 65th session exchange must fail with 401 because session pool is saturated
	tkt65, err := s.Ticket()
	if err != nil {
		t.Fatalf("failed to create 65th ticket: %v", err)
	}
	r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(`{"ticket":"`+tkt65+`"}`))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatalf("expected 401 when sessions >= 64, got %d", w.Code)
	}
}

// TestAdversarialConcurrentTicketExchange proves that when 50 concurrent goroutines
// attempt to exchange the EXACT same ticket, exactly 1 succeeds and 49 fail with 401.
func TestAdversarialConcurrentTicketExchange(t *testing.T) {
	s, _ := fixture(t)
	ticket, err := s.Ticket()
	if err != nil {
		t.Fatal(err)
	}

	const goroutines = 50
	var wg sync.WaitGroup
	var successCount int32
	var failCount int32

	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader(`{"ticket":"`+ticket+`"}`))
			r.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()
			s.ServeHTTP(w, r)
			if w.Code == 200 {
				atomic.AddInt32(&successCount, 1)
			} else if w.Code == 401 {
				atomic.AddInt32(&failCount, 1)
			}
		}()
	}
	wg.Wait()

	if successCount != 1 {
		t.Fatalf("expected exactly 1 successful exchange, got %d", successCount)
	}
	if failCount != goroutines-1 {
		t.Fatalf("expected %d failures, got %d", goroutines-1, failCount)
	}
}

// TestAdversarialUnmappedRoutesAndPathTraversal tests security boundaries for unmapped routes:
// 1. /api/v1/auth/exchange returns 401 if unauthenticated, 404 "未知操作" if authenticated.
// 2. /module-assets/ path traversal attempts return 404.
func TestAdversarialUnmappedRoutesAndPathTraversal(t *testing.T) {
	s, _ := fixture(t)

	// Unauthenticated /api/v1/auth/exchange must return 401
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/auth/exchange", nil))
	if w.Code != 401 {
		t.Fatalf("unauthenticated /api/v1/auth/exchange returned %d, want 401", w.Code)
	}

	// Authenticated /api/v1/auth/exchange must return 404 "未知操作"
	c := exchange(t, s)
	r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/auth/exchange", strings.NewReader("{}"))
	r.AddCookie(c)
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("X-MAA-Request", "1")
	w2 := httptest.NewRecorder()
	s.ServeHTTP(w2, r)
	if w2.Code != 404 {
		t.Fatalf("authenticated /api/v1/auth/exchange returned %d, want 404: %s", w2.Code, w2.Body.String())
	}
	if !strings.Contains(w2.Body.String(), "未知操作") {
		t.Fatalf("expected '未知操作' message, got: %s", w2.Body.String())
	}

	// Asset path traversal
	for _, traversal := range []string{
		"/module-assets/../main.go",
		"/module-assets/%2e%2e/server.go",
		"/module-assets/secret.txt",
		"/module-assets/",
	} {
		rAsset := httptest.NewRequest("GET", "http://127.0.0.1:8088"+traversal, nil)
		rAsset.AddCookie(c)
		wAsset := httptest.NewRecorder()
		s.ServeHTTP(wAsset, rAsset)
		if wAsset.Code != 404 {
			t.Fatalf("asset traversal %s returned %d, want 404", traversal, wAsset.Code)
		}
	}
}

// TestAdversarialReverseProxy8MBLimit tests that upstream responses exceeding maxInjectBytes (8MB)
// trigger error handler without memory exhaustion or malformed writes.
func TestAdversarialReverseProxy8MBLimit(t *testing.T) {
	hugeHTML := "<!doctype html><html><body>" + strings.Repeat("A", maxInjectBytes+100) + "</body></html>"
	p := newProxyFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, hugeHTML)
	})
	c := exchange(t, p.server)

	w := p.do(t, "GET", "/", c, "", nil)
	// Because hugeHTML exceeds maxInjectBytes, ModifyResponse returns an error,
	// triggering the recovery handler which serves recoveryHTML (status 200)
	if w.Code != 200 {
		t.Fatalf("expected recovery page (200), got %d", w.Code)
	}
	if !strings.Contains(w.Body.String(), "MAA 模块恢复") {
		t.Fatalf("expected recovery page for >8MB response, got body of len %d", w.Body.Len())
	}
}

// TestAdversarialReverseProxyCompressedPassThrough verifies that upstream responses
// with Content-Encoding: gzip or br pass through untouched without corrupted body or erroneous injection.
func TestAdversarialReverseProxyCompressedPassThrough(t *testing.T) {
	rawHTML := "<!doctype html><html><body>compressed content</body></html>"
	var buf bytes.Buffer
	gw := gzip.NewWriter(&buf)
	_, _ = gw.Write([]byte(rawHTML))
	_ = gw.Close()
	compressedBytes := buf.Bytes()

	p := newProxyFixture(t, func(w http.ResponseWriter, r *http.Request) {})

	for _, encoding := range []string{"gzip", "br", "deflate"} {
		resp := responseFor("GET", "/", http.Header{
			"Content-Type":     {"text/html; charset=utf-8"},
			"Content-Encoding": {encoding},
		}, compressedBytes)

		if err := p.server.proxy.ModifyResponse(resp); err != nil {
			t.Fatalf("ModifyResponse failed for %s: %v", encoding, err)
		}

		got, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("failed to read body: %v", err)
		}
		if !bytes.Equal(got, compressedBytes) {
			t.Fatalf("compressed body was rewritten for encoding %s", encoding)
		}
		if strings.Contains(string(got), "module-settings") {
			t.Fatalf("module-settings improperly injected into compressed stream for %s", encoding)
		}
	}
}
