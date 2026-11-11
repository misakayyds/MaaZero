package download

import (
	"context"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"maa-phone-manager/manager/internal/manifest"
)

func artifact(url, body string) manifest.Artifact {
	return manifest.Artifact{URL: url, Size: int64(len(body)), SHA256: fmt.Sprintf("%x", sha256.Sum256([]byte(body)))}
}

func TestResumeAndMirror(t *testing.T) {
	const body = "complete artifact"
	var ranges []string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/bad" {
			w.WriteHeader(503)
			return
		}
		ranges = append(ranges, r.Header.Get("Range"))
		if r.Header.Get("Range") == "bytes=4-" {
			w.Header().Set("Content-Range", "bytes 4-16/17")
			w.WriteHeader(206)
			_, _ = w.Write([]byte(body[4:]))
		} else {
			_, _ = w.Write([]byte(body))
		}
	}))
	defer srv.Close()
	a := artifact(srv.URL+"/bad", body)
	a.Mirrors = []string{srv.URL + "/ok"}
	path := filepath.Join(t.TempDir(), "asset")
	if err := os.WriteFile(path+".part", []byte(body[:4]), 0600); err != nil {
		t.Fatal(err)
	}
	var last Progress
	c := Client{HTTP: srv.Client()}
	if err := c.Fetch(context.Background(), a, path, func(p Progress) { last = p }); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(path)
	if err != nil || string(b) != body {
		t.Fatalf("download: %q %v", b, err)
	}
	if len(ranges) != 1 || ranges[0] != "bytes=4-" {
		t.Fatalf("not resumed: %v", ranges)
	}
	if last.Bytes != 17 || last.Total != 17 {
		t.Fatalf("progress: %+v", last)
	}
}

func TestRejectCorruptOversizeTruncatedAndHTTP(t *testing.T) {
	for _, body := range []string{"bad", "toolong", "ok"} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(body)) }))
			defer srv.Close()
			path := filepath.Join(t.TempDir(), "asset")
			c := Client{HTTP: srv.Client()}
			if err := c.Fetch(context.Background(), artifact(srv.URL, "yes"), path, nil); err == nil {
				t.Fatal("invalid bytes accepted")
			}
			if _, err := os.Stat(path); !os.IsNotExist(err) {
				t.Fatal("invalid download promoted")
			}
		})
	}
	c := Client{}
	if err := c.Fetch(context.Background(), artifact("http://example.invalid/file", "yes"), filepath.Join(t.TempDir(), "asset"), nil); err == nil {
		t.Fatal("plaintext accepted")
	}
}

func TestCancelledAndExistingDestinationPreserved(t *testing.T) {
	path := filepath.Join(t.TempDir(), "asset")
	if err := os.WriteFile(path, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	c := Client{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := c.Fetch(ctx, artifact("https://example.invalid/file", "yes"), path, nil); err == nil {
		t.Fatal("cancel ignored")
	}
	b, _ := os.ReadFile(path)
	if string(b) != "keep" {
		t.Fatal("existing destination changed")
	}
}

func TestIgnoredRangeRestartsAndBadRangeRejected(t *testing.T) {
	for _, invalid := range []bool{false, true} {
		t.Run(fmt.Sprint(invalid), func(t *testing.T) {
			srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if invalid {
					w.Header().Set("Content-Range", "bytes 0-2/3")
					w.WriteHeader(206)
				}
				_, _ = w.Write([]byte("yes"))
			}))
			defer srv.Close()
			path := filepath.Join(t.TempDir(), "asset")
			if err := os.WriteFile(path+".part", []byte("y"), 0600); err != nil {
				t.Fatal(err)
			}
			c := Client{HTTP: srv.Client()}
			err := c.Fetch(context.Background(), artifact(srv.URL, "yes"), path, nil)
			if invalid && err == nil {
				t.Fatal("bad range accepted")
			}
			if !invalid && err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestCompletePartialVerifiedWithoutNetwork(t *testing.T) {
	path := filepath.Join(t.TempDir(), "asset")
	if err := os.WriteFile(path+".part", []byte("yes"), 0600); err != nil {
		t.Fatal(err)
	}
	c := Client{}
	if err := c.Fetch(context.Background(), artifact("https://example.invalid/file", "yes"), path, nil); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(path)
	if string(b) != "yes" {
		t.Fatal("wrong cached bytes")
	}
}

func TestCorruptPartialRetriesMirrorFromZero(t *testing.T) {
	var ranges []string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ranges = append(ranges, r.Header.Get("Range"))
		if r.Header.Get("Range") == "bytes=1-" {
			w.Header().Set("Content-Range", "bytes 1-2/3")
			w.WriteHeader(206)
			_, _ = w.Write([]byte("es"))
		} else {
			_, _ = w.Write([]byte("yes"))
		}
	}))
	defer srv.Close()
	path := filepath.Join(t.TempDir(), "asset")
	if err := os.WriteFile(path+".part", []byte("!"), 0600); err != nil {
		t.Fatal(err)
	}
	a := artifact(srv.URL+"/one", "yes")
	a.Mirrors = []string{srv.URL + "/two"}
	c := Client{HTTP: srv.Client()}
	if err := c.Fetch(context.Background(), a, path, nil); err != nil {
		t.Fatal(err)
	}
	if len(ranges) != 2 || ranges[0] != "bytes=1-" || ranges[1] != "" {
		t.Fatalf("bad restart: %v", ranges)
	}
}

func TestHTTPSDowngradeRedirectRejected(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://example.invalid/file", http.StatusFound)
	}))
	defer srv.Close()
	c := Client{HTTP: srv.Client()}
	if err := c.Fetch(context.Background(), artifact(srv.URL, "yes"), filepath.Join(t.TempDir(), "asset"), nil); err == nil {
		t.Fatal("downgrade redirect accepted")
	}
}
