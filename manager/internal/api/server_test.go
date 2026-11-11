package api

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

type fixtureBackend struct {
	checks   int
	checkErr error
}

func (b *fixtureBackend) Status() any {
	return map[string]any{"configured": false, "can_install": false}
}
func (b *fixtureBackend) Check(context.Context) error    { b.checks++; return b.checkErr }
func (b *fixtureBackend) Apply(context.Context) error    { return nil }
func (b *fixtureBackend) Rollback(context.Context) error { return nil }

func fixture(t *testing.T) (*Server, *fixtureBackend) {
	t.Helper()
	b := &fixtureBackend{}
	s, err := New(Config{Host: "127.0.0.1:8088", Backend: b})
	if err != nil {
		t.Fatal(err)
	}
	return s, b
}

func exchange(t *testing.T, s *Server) *http.Cookie {
	t.Helper()
	ticket, err := s.Ticket()
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader("{\"ticket\":\""+ticket+"\"}"))
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatalf("exchange %d %s", w.Code, w.Body.String())
	}
	c := w.Result().Cookies()[0]
	if !c.HttpOnly || c.SameSite != http.SameSiteStrictMode {
		t.Fatal("unsafe cookie")
	}
	return c
}

func postJSON(t *testing.T, s *Server, c *http.Cookie, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest("POST", "http://127.0.0.1:8088"+path, strings.NewReader(body))
	r.AddCookie(c)
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("X-MAA-Request", "1")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}

func TestSessionHostAndCSRF(t *testing.T) {
	s, b := fixture(t)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("GET", "http://127.0.0.1:8088/api/v1/status", nil))
	if w.Code != 401 {
		t.Fatal("unauthenticated status")
	}
	c := exchange(t, s)
	for _, tc := range []struct {
		host, origin, header string
		want                 int
	}{
		{"127.0.0.1:8088", "http://127.0.0.1:8088", "1", 200},
		{"evil.invalid", "http://evil.invalid", "1", 403},
		{"127.0.0.1:8088", "http://evil.invalid", "1", 403},
		{"127.0.0.1:8088", "http://127.0.0.1:8088", "", 403},
	} {
		r := httptest.NewRequest("POST", "http://"+tc.host+"/api/v1/update/check", strings.NewReader("{}"))
		r.AddCookie(c)
		r.Header.Set("Origin", tc.origin)
		r.Header.Set("X-MAA-Request", tc.header)
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("got %d want %d", w.Code, tc.want)
		}
	}
	if b.checks != 1 {
		t.Fatalf("unauthorized checks: %d", b.checks)
	}
}

func TestTicketSingleUseAndNoArbitraryArguments(t *testing.T) {
	s, _ := fixture(t)
	ticket, _ := s.Ticket()
	for i, want := range []int{200, 401} {
		r := httptest.NewRequest("POST", "http://127.0.0.1:8088/api/v1/session", strings.NewReader("{\"ticket\":\""+ticket+"\"}"))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		s.ServeHTTP(w, r)
		if w.Code != want {
			t.Fatalf("attempt %d got %d", i, w.Code)
		}
	}
	c := exchange(t, s)
	for _, body := range []string{"{}", "{\"confirm\":true,\"url\":\"https://evil.invalid\"}", "{\"confirm\":true} {}"} {
		if w := postJSON(t, s, c, "/api/v1/update/apply", body); w.Code != 400 {
			t.Fatalf("accepted body %s: %d", body, w.Code)
		}
	}
}

func TestAuthenticatedStatus(t *testing.T) {
	s, _ := fixture(t)
	c := exchange(t, s)
	r := httptest.NewRequest("GET", "http://127.0.0.1:8088/api/v1/status", nil)
	r.AddCookie(c)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	var got map[string]any
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &got) != nil || got["configured"] != false {
		t.Fatal(w.Body.String())
	}
}

func TestCheckFailureIsNotReportedAsARejectedOperation(t *testing.T) {
	b := &fixtureBackend{checkErr: errors.New("release source unreachable")}
	s, err := New(Config{Host: "127.0.0.1:8088", Backend: b})
	if err != nil {
		t.Fatal(err)
	}
	c := exchange(t, s)
	if w := postJSON(t, s, c, "/api/v1/update/check", "{}"); w.Code != 502 {
		t.Fatalf("failed check reported %d: %s", w.Code, w.Body.String())
	}
	if w := postJSON(t, s, c, "/api/v1/update/apply", "{\"confirm\":true}"); w.Code != 200 {
		t.Fatalf("apply reported %d: %s", w.Code, w.Body.String())
	}
}

// proxyFixture stands in for the loopback WebUI so the reverse proxy paths are
// exercised end to end.
type proxyFixture struct {
	server   *Server
	backend  *fixtureBackend
	upstream *httptest.Server
	mu       sync.Mutex
	requests []string
	cookies  []string
}

func newProxyFixture(t *testing.T, handler http.HandlerFunc) *proxyFixture {
	t.Helper()
	p := &proxyFixture{backend: &fixtureBackend{}}
	p.upstream = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p.mu.Lock()
		p.requests = append(p.requests, r.Method+" "+r.URL.Path)
		p.cookies = append(p.cookies, r.Header.Get("Cookie"))
		p.mu.Unlock()
		handler(w, r)
	}))
	t.Cleanup(p.upstream.Close)
	s, err := New(Config{Host: "127.0.0.1:8088", Backend: p.backend, Upstream: p.upstream.URL})
	if err != nil {
		t.Fatal(err)
	}
	p.server = s
	return p
}

func (p *proxyFixture) do(t *testing.T, method, path string, cookie *http.Cookie, contentType string, body io.Reader) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest(method, "http://127.0.0.1:8088"+path, body)
	if cookie != nil {
		r.AddCookie(cookie)
	}
	if contentType != "" {
		r.Header.Set("Content-Type", contentType)
	}
	w := httptest.NewRecorder()
	p.server.ServeHTTP(w, r)
	return w
}

func (p *proxyFixture) upstreamRequests() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.requests...)
}

func (p *proxyFixture) upstreamCookies() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.cookies...)
}

func TestProxyKeepsBodylessWritesAndRefusesFormPosts(t *testing.T) {
	page := "<html><body>webui</body></html>"
	p := newProxyFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, page)
	})
	c := exchange(t, p.server)
	for _, tc := range []struct {
		contentType string
		want        int
	}{
		{"", 200}, // the existing WebUI POSTs update, startup-health and kill without a body
		{"application/json", 200},
		{"application/x-www-form-urlencoded", 403},
		{"multipart/form-data; boundary=x", 403},
		{"text/plain; charset=utf-8", 403},
	} {
		if w := p.do(t, "POST", "/api/kill", c, tc.contentType, nil); w.Code != tc.want {
			t.Fatalf("POST content-type %q -> %d, want %d", tc.contentType, w.Code, tc.want)
		}
	}
	if w := p.do(t, "POST", "/api/v1/update/check", c, "application/json", strings.NewReader("{}")); w.Code != 403 {
		t.Fatalf("module api write without the marker header: %d", w.Code)
	}
	if got := p.upstreamRequests(); len(got) != 2 {
		t.Fatalf("upstream saw %v, want exactly the two accepted writes", got)
	}
	for _, cookie := range p.upstreamCookies() {
		if cookie != "" {
			t.Fatalf("session cookie leaked upstream: %q", cookie)
		}
	}
}

func TestProxyInjectsModuleAssetsAndKeepsTheBodyTag(t *testing.T) {
	page := "<!doctype html><html><head><title>t</title></head><body><div id=\"app\"></div></body></html>"
	p := newProxyFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, page)
	})
	c := exchange(t, p.server)
	w := p.do(t, "GET", "/", c, "", nil)
	if w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	body := w.Body.String()
	if got := strings.Count(body, "</body>"); got != 1 {
		t.Fatalf("closing body tag appears %d times: %q", got, body)
	}
	if got := strings.Count(body, "module-settings.js"); got != 1 {
		t.Fatalf("script injected %d times: %q", got, body)
	}
	if got := strings.Count(body, "module-settings.css"); got != 1 {
		t.Fatalf("stylesheet injected %d times: %q", got, body)
	}
	if !strings.Contains(body, "data-maa-overlay=\"module-settings\"") {
		t.Fatalf("overlay marker missing: %q", body)
	}
	if !strings.Contains(body, "<div id=\"app\"></div>") {
		t.Fatalf("page content damaged: %q", body)
	}
	if !strings.HasSuffix(body, "</body></html>") {
		t.Fatalf("page tail damaged: %q", body)
	}
	if strings.Index(body, "/module-assets/module-settings.js") > strings.LastIndex(body, "</body>") {
		t.Fatalf("assets injected after the body tag: %q", body)
	}
	if got := w.Header().Get("Content-Length"); got != "" {
		t.Fatalf("stale content length header: %q", got)
	}
}

func TestProxyLeavesAnAlreadyInjectedPageIntact(t *testing.T) {
	page := "<html><body><link rel=\"stylesheet\" href=\"/module-assets/module-settings.css\" data-maa-overlay=\"module-settings\"><script src=\"/module-assets/module-settings.js\" defer data-maa-overlay=\"module-settings\"></script></body></html>"
	p := newProxyFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = io.WriteString(w, page)
	})
	c := exchange(t, p.server)
	w := p.do(t, "GET", "/", c, "", nil)
	if w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	body := w.Body.String()
	// A host content filter may add its own tags, so assert the properties that
	// must hold instead of byte equality: the page survives and gains nothing.
	if len(body) < len(page) {
		t.Fatalf("already injected page was truncated to %d bytes: %q", len(body), body)
	}
	for _, asset := range []string{"module-settings.js", "module-settings.css"} {
		if got := strings.Count(body, asset); got != 1 {
			t.Fatalf("%s appears %d times: %q", asset, got, body)
		}
	}
}

func TestProxyPassesThroughHeadRequests(t *testing.T) {
	p := newProxyFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusOK)
	})
	c := exchange(t, p.server)
	h := p.do(t, "HEAD", "/", c, "", nil)
	if h.Code != 200 {
		t.Fatalf("HEAD status %d", h.Code)
	}
	if h.Body.Len() != 0 {
		t.Fatalf("HEAD returned %d body bytes", h.Body.Len())
	}
}

// responseFor builds the upstream response shape ModifyResponse inspects, which
// keeps these regressions testable without depending on a host content filter.
func responseFor(method, path string, header http.Header, body []byte) *http.Response {
	return &http.Response{
		StatusCode:    200,
		Header:        header,
		Body:          io.NopCloser(bytes.NewReader(body)),
		ContentLength: int64(len(body)),
		Request:       httptest.NewRequest(method, "http://127.0.0.1:8088"+path, nil),
	}
}

func TestModifyResponseRestoresAnAlreadyInjectedPage(t *testing.T) {
	p := newProxyFixture(t, func(http.ResponseWriter, *http.Request) {})
	page := []byte("<html><body><p data-maa-overlay=\"module-settings\">kept</p></body></html>")
	resp := responseFor("GET", "/", http.Header{"Content-Type": {"text/html; charset=utf-8"}}, page)
	if err := p.server.proxy.ModifyResponse(resp); err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("response body was left unusable: %v", err)
	}
	if !bytes.Equal(got, page) {
		t.Fatalf("already injected page was rewritten to %d bytes: %q", len(got), got)
	}
	if resp.ContentLength != int64(len(page)) {
		t.Fatalf("content length = %d, want %d", resp.ContentLength, len(page))
	}
}

func TestModifyResponseKeepsASingleClosingBodyTag(t *testing.T) {
	p := newProxyFixture(t, func(http.ResponseWriter, *http.Request) {})
	page := []byte("<!doctype html><html><head><title>t</title></head><body><div id=\"app\"></div></body></html>")
	resp := responseFor("GET", "/", http.Header{"Content-Type": {"text/html; charset=utf-8"}}, page)
	if err := p.server.proxy.ModifyResponse(resp); err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if n := bytes.Count(got, []byte("</body>")); n != 1 {
		t.Fatalf("closing body tag appears %d times: %q", n, got)
	}
	if n := bytes.Count(got, []byte("module-settings.js")); n != 1 {
		t.Fatalf("script injected %d times: %q", n, got)
	}
	if !bytes.Contains(got, []byte("data-maa-overlay=\"module-settings\"")) {
		t.Fatalf("overlay marker missing: %q", got)
	}
	if !bytes.HasSuffix(got, []byte("</body></html>")) {
		t.Fatalf("page tail damaged: %q", got)
	}
	if !bytes.Contains(got, []byte("<div id=\"app\"></div>")) {
		t.Fatalf("page content damaged: %q", got)
	}
}

func TestModifyResponseLeavesEncodedAndNonPageResponsesAlone(t *testing.T) {
	p := newProxyFixture(t, func(http.ResponseWriter, *http.Request) {})
	var compressed bytes.Buffer
	gz := gzip.NewWriter(&compressed)
	if _, err := gz.Write([]byte("<html><body>webui</body></html>")); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	page := []byte("<!doctype html><html><body>webui</body></html>")
	for _, tc := range []struct {
		name   string
		method string
		header http.Header
		body   []byte
	}{
		{"compressed page", "GET", http.Header{"Content-Type": {"text/html"}, "Content-Encoding": {"gzip"}}, compressed.Bytes()},
		{"head request", "HEAD", http.Header{"Content-Type": {"text/html"}}, page},
		{"json response", "GET", http.Header{"Content-Type": {"application/json"}}, []byte("{}")},
		{"other path", "GET", http.Header{"Content-Type": {"text/html"}}, page},
	} {
		path := "/"
		if tc.name == "other path" {
			path = "/other"
		}
		resp := responseFor(tc.method, path, tc.header, tc.body)
		if err := p.server.proxy.ModifyResponse(resp); err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		got, err := io.ReadAll(resp.Body)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if !bytes.Equal(got, tc.body) {
			t.Fatalf("%s was rewritten: %q", tc.name, got)
		}
	}
}

func TestInjectableRejectsNonPagesAndOversizedBodies(t *testing.T) {
	for _, tc := range []struct {
		name string
		resp *http.Response
		want bool
	}{
		{"plain page", responseFor("GET", "/", http.Header{"Content-Type": {"text/html"}}, []byte("x")), true},
		{"gzip page", responseFor("GET", "/", http.Header{"Content-Type": {"text/html"}, "Content-Encoding": {"br"}}, []byte("x")), false},
		{"post", responseFor("POST", "/", http.Header{"Content-Type": {"text/html"}}, []byte("x")), false},
		{"other path", responseFor("GET", "/index.html", http.Header{"Content-Type": {"text/html"}}, []byte("x")), false},
		{"json", responseFor("GET", "/", http.Header{"Content-Type": {"application/json"}}, []byte("{}")), false},
	} {
		if got := injectable(tc.resp); got != tc.want {
			t.Errorf("%s: injectable = %v, want %v", tc.name, got, tc.want)
		}
	}
	oversized := responseFor("GET", "/", http.Header{"Content-Type": {"text/html"}}, []byte("x"))
	oversized.ContentLength = maxInjectBytes + 1
	if injectable(oversized) {
		t.Error("oversized page must not be buffered for injection")
	}
	if injectable(&http.Response{Header: http.Header{"Content-Type": {"text/html"}}}) {
		t.Error("response without a request must not be injected")
	}
}
