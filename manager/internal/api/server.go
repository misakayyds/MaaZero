// Package api exposes fixed local operations behind a one-use bootstrap ticket.
package api

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"mime"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"time"
)

type Backend interface {
	Status() any
	Check(context.Context) error
	Apply(context.Context) error
	Rollback(context.Context) error
}

type Config struct {
	Host     string
	Backend  Backend
	Assets   fs.FS
	Upstream string
}
type Server struct {
	cfg               Config
	mu                sync.Mutex
	tickets, sessions map[string]time.Time
	proxy             *httputil.ReverseProxy
}

// maxInjectBytes bounds the proxied page the module assets are injected into.
const maxInjectBytes = 8 << 20

// closeBodyTag is the anchor the module assets are injected at.
const closeBodyTag = "</body>"

// moduleAssetOverlay mirrors packaging/webui_overlay.py: the injected tags carry
// this marker, so a page that already has them is never injected twice.
const moduleAssetOverlay = `data-maa-overlay="module-settings"`

// injectable reports whether the extension assets may be injected into an
// upstream response. Compressed bodies and non-page responses pass through
// untouched: replacing them would hide the real WebUI behind a recovery page.
func injectable(r *http.Response) bool {
	if r.Request == nil || r.Request.Method != "GET" || r.Request.URL.Path != "/" {
		return false
	}
	if encoding := strings.TrimSpace(r.Header.Get("Content-Encoding")); encoding != "" && !strings.EqualFold(encoding, "identity") {
		return false
	}
	if !strings.Contains(strings.ToLower(r.Header.Get("Content-Type")), "text/html") {
		return false
	}
	return r.ContentLength <= maxInjectBytes
}

// restoreBody puts the rewritten (or merely inspected) page back on the
// response. The upstream body is fully consumed to inspect it, so leaving the
// closed reader in place would hand the client an empty page.
func restoreBody(r *http.Response, b []byte) {
	r.Body = io.NopCloser(bytes.NewReader(b))
	r.ContentLength = int64(len(b))
	r.Header.Del("Content-Length")
	r.Header.Del("ETag")
}

func New(cfg Config) (*Server, error) {
	host, _, err := net.SplitHostPort(cfg.Host)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() || cfg.Backend == nil {
		return nil, errors.New("loopback host and backend required")
	}
	s := &Server{cfg: cfg, tickets: map[string]time.Time{}, sessions: map[string]time.Time{}}
	if cfg.Upstream != "" {
		u, err := url.Parse(cfg.Upstream)
		if err != nil || u.Scheme != "http" || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || u.Host == cfg.Host {
			return nil, errors.New("invalid loopback upstream")
		}
		s.proxy = httputil.NewSingleHostReverseProxy(u)
		director := s.proxy.Director
		s.proxy.Director = func(r *http.Request) {
			director(r)
			r.Header.Del("Cookie")
			r.Header.Del("Authorization")
			r.Header.Set("Accept-Encoding", "identity")
		}
		s.proxy.ModifyResponse = func(r *http.Response) error {
			r.Header.Del("Access-Control-Allow-Origin")
			r.Header.Del("Access-Control-Allow-Credentials")
			r.Header.Del("Set-Cookie")
			if !injectable(r) {
				return nil
			}
			b, err := io.ReadAll(io.LimitReader(r.Body, (maxInjectBytes)+1))
			_ = r.Body.Close()
			if err != nil || len(b) > maxInjectBytes {
				return errors.New("upstream page unavailable")
			}
			if bytes.Contains(b, []byte(moduleAssetOverlay)) {
				// The page already carries the extension assets (for example from
				// packaging/webui_overlay.py); injecting twice would double render.
				// The body was consumed to inspect it, so it must be restored.
				restoreBody(r, b)
				return nil
			}
			extra := `<link rel="stylesheet" href="/module-assets/module-settings.css" data-maa-overlay="module-settings"><script src="/module-assets/module-settings.js" defer data-maa-overlay="module-settings"></script></body>`
			// The fragment carries its own closing body tag, so it replaces the
			// last one instead of duplicating it. A page without the tag keeps its
			// content and simply gains the assets at the end.
			if i := bytes.LastIndex(b, []byte(closeBodyTag)); i >= 0 {
				injected := make([]byte, 0, len(b)+len(extra))
				injected = append(injected, b[:i]...)
				injected = append(injected, extra...)
				injected = append(injected, b[i+len(closeBodyTag):]...)
				b = injected
			} else {
				b = append(b, extra...)
			}
			restoreBody(r, b)
			return nil
		}
		s.proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, _ error) { s.recovery(w, r) }
	}
	return s, nil
}

func randomString() string { var b [32]byte; _, _ = rand.Read(b[:]); return hex.EncodeToString(b[:]) }

// Ticket is called by the root-only local IPC server, never an HTTP route.
func (s *Server) Ticket() (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.prune()
	if len(s.tickets) >= 64 {
		return "", errors.New("too many pending entry tickets")
	}
	t := randomString()
	s.tickets[t] = time.Now().Add(time.Minute)
	return t, nil
}

func (s *Server) prune() {
	now := time.Now()
	for k, v := range s.tickets {
		if !now.Before(v) {
			delete(s.tickets, k)
		}
	}
	for k, v := range s.sessions {
		if !now.Before(v) {
			delete(s.sessions, k)
		}
	}
}

func (s *Server) authorized(r *http.Request) bool {
	c, err := r.Cookie("MAA_SESSION")
	if err != nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.prune()
	_, ok := s.sessions[c.Value]
	return ok
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// formContentType reports whether a request body could have come from a
// cross-origin HTML form or a form-encoded request, the only write shapes a
// browser sends without a preflight. An absent Content-Type (a bodyless POST) is
// deliberately not treated as cross-site evidence.
func formContentType(raw string) bool {
	t, _, err := mime.ParseMediaType(raw)
	if err != nil {
		t = strings.ToLower(strings.TrimSpace(raw))
	}
	switch t {
	case "application/x-www-form-urlencoded", "multipart/form-data", "text/plain":
		return true
	}
	return false
}

func failure(w http.ResponseWriter, code int, message string) {
	writeJSON(w, code, map[string]any{"ok": false, "message": message})
}

func decode(w http.ResponseWriter, r *http.Request, v any) error {
	t, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || t != "application/json" {
		return errors.New("JSON required")
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(v); err != nil {
		return err
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("X-Frame-Options", "DENY")
	if r.Host != s.cfg.Host {
		failure(w, 403, "请通过模块入口打开")
		return
	}
	if origin := r.Header.Get("Origin"); origin != "" && origin != "http://"+s.cfg.Host {
		failure(w, 403, "来源不受信任")
		return
	}
	if r.URL.Path == "/api/v1/session" {
		if r.Method != "POST" {
			failure(w, 405, "请求方法不支持")
			return
		}
		var input struct {
			Ticket string `json:"ticket"`
		}
		if decode(w, r, &input) != nil {
			failure(w, 400, "请求格式错误")
			return
		}
		s.mu.Lock()
		s.prune()
		_, ok := s.tickets[input.Ticket]
		delete(s.tickets, input.Ticket)
		if !ok || len(s.sessions) >= 64 {
			s.mu.Unlock()
			failure(w, 401, "入口已过期，请从模块重新打开")
			return
		}
		value := randomString()
		s.sessions[value] = time.Now().Add(12 * time.Hour)
		s.mu.Unlock()
		http.SetCookie(w, &http.Cookie{Name: "MAA_SESSION", Value: value, Path: "/", HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: 43200})
		writeJSON(w, 200, map[string]bool{"ok": true})
		return
	}
	if !s.authorized(r) {
		if r.URL.Path == "/" && r.Method == "GET" {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = io.WriteString(w, entryHTML)
			return
		}
		failure(w, 401, "请从模块入口重新打开管理页面")
		return
	}
	if r.Method != "GET" && r.Method != "HEAD" {
		if strings.HasPrefix(r.URL.Path, "/api/v1/") {
			// Module API writes need a marker that no HTML form and no
			// cross-origin fetch can set without a preflight we never answer.
			if r.Header.Get("X-MAA-Request") != "1" {
				failure(w, 403, "请求校验失败")
				return
			}
		} else if formContentType(r.Header.Get("Content-Type")) {
			// A browser reaches this proxy cross-origin without a preflight only
			// when the body is a CORS-safelisted form type, so exactly those are
			// refused. The existing WebUI also POSTs some endpoints without a body
			// and without a Content-Type, which must keep working. No permissive
			// CORS or OPTIONS response is exposed here.
			failure(w, 403, "请求校验失败")
			return
		}
	}
	switch r.URL.Path {
	case "/api/v1/status":
		if r.Method != "GET" {
			failure(w, 405, "请求方法不支持")
			return
		}
		writeJSON(w, 200, s.cfg.Backend.Status())
		return
	case "/api/v1/help":
		if r.Method != "GET" {
			failure(w, 405, "请求方法不支持")
			return
		}
		if s.cfg.Assets != nil {
			if b, err := fs.ReadFile(s.cfg.Assets, "user-guide.md"); err == nil {
				w.Header().Set("Content-Type", "text/markdown; charset=utf-8")
				_, _ = w.Write(b)
				return
			}
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(w, "当前是本地预览。请查看项目 docs/user-guide.md；尚未验收的设备不能作为正式支持设备。")
		return
	case "/api/v1/update/check", "/api/v1/update/apply", "/api/v1/rollback":
		if r.Method != "POST" {
			failure(w, 405, "请求方法不支持")
			return
		}
		var err error
		if r.URL.Path == "/api/v1/update/check" {
			var input struct{}
			if decode(w, r, &input) != nil {
				failure(w, 400, "请求格式错误")
				return
			}
			err = s.cfg.Backend.Check(r.Context())
		} else {
			var input struct {
				Confirm bool `json:"confirm"`
			}
			if decode(w, r, &input) != nil || !input.Confirm {
				failure(w, 400, "请确认操作")
				return
			}
			if r.URL.Path == "/api/v1/rollback" {
				err = s.cfg.Backend.Rollback(r.Context())
			} else {
				err = s.cfg.Backend.Apply(r.Context())
			}
		}
		if err != nil {
			if r.URL.Path == "/api/v1/update/check" {
				// A check is read-only, so it must say that the check failed instead
				// of claiming a rejected operation.
				failure(w, 502, "检查发布配置未完成；当前版本保持不变")
				return
			}
			failure(w, 409, "操作未执行或未完成，请查看模块状态说明")
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true, "message": "操作已接受，请查看状态"})
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/v1/") {
		failure(w, 404, "未知操作")
		return
	}
	if strings.HasPrefix(r.URL.Path, "/module-assets/") {
		if r.Method != "GET" || s.cfg.Assets == nil {
			failure(w, 404, "资源不可用")
			return
		}
		name := strings.TrimPrefix(r.URL.Path, "/module-assets/")
		if name != "module-settings.js" && name != "module-settings.css" {
			failure(w, 404, "资源不存在")
			return
		}
		b, err := fs.ReadFile(s.cfg.Assets, name)
		if err != nil {
			failure(w, 404, "资源不可用")
			return
		}
		if strings.HasSuffix(name, ".js") {
			w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
		} else {
			w.Header().Set("Content-Type", "text/css; charset=utf-8")
		}
		_, _ = w.Write(b)
		return
	}
	if s.proxy != nil {
		s.proxy.ServeHTTP(w, r)
		return
	}
	s.recovery(w, r)
}

func (s *Server) recovery(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		failure(w, 503, "MAA 运行环境尚未就绪")
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = io.WriteString(w, recoveryHTML)
}

const entryHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MAA 模块入口</title><body><h1>MAA 模块</h1><p id="message">请从 Root 管理器的模块入口打开此页面。</p><script>(async()=>{const t=new URLSearchParams(location.hash.slice(1)).get('ticket');history.replaceState(null,'','/');if(!t)return;try{const r=await fetch('/api/v1/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket:t})});if(r.ok)location.reload();else document.getElementById('message').textContent='入口已过期，请从模块重新打开。';}catch{document.getElementById('message').textContent='后台连接失败，请从模块重新打开。';}})();</script></body></html>`

const recoveryHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>MAA 模块恢复</title><body><h1>MAA 模块恢复</h1><p>后台已启动，MAA 页面尚未就绪。现有配置不会被重置。</p><p><a href="/api/v1/help">打开使用说明</a></p><pre id="status">读取状态…</pre><button id="check">检查发布配置</button><script>async function status(){const r=await fetch('/api/v1/status');document.getElementById('status').textContent=JSON.stringify(await r.json(),null,2)}document.getElementById('check').onclick=async()=>{await fetch('/api/v1/update/check',{method:'POST',headers:{'Content-Type':'application/json','X-MAA-Request':'1'},body:'{}'});await status()};status();</script></body></html>`
