package install

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"maa-phone-manager/manager/internal/manifest"
)

func archive(t *testing.T, headers []*tar.Header) (manifest.Artifact, string) {
	t.Helper()
	var b bytes.Buffer
	gz := gzip.NewWriter(&b)
	tw := tar.NewWriter(gz)
	for _, h := range headers {
		if err := tw.WriteHeader(h); err != nil {
			t.Fatal(err)
		}
		if h.Size > 0 {
			if _, err := tw.Write(bytes.Repeat([]byte("x"), int(h.Size))); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(t.TempDir(), "project.tar.gz")
	if err := os.WriteFile(p, b.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	return manifest.Artifact{Size: int64(b.Len()), SHA256: fmt.Sprintf("%x", sha256.Sum256(b.Bytes())), Compression: "tar.gz"}, p
}

func TestStageVerifiedProject(t *testing.T) {
	a, p := archive(t, []*tar.Header{{Name: "bin/run.sh", Mode: 0755, Size: 3, Typeflag: tar.TypeReg}})
	dest := filepath.Join(t.TempDir(), "1.0.0")
	if err := Stage(context.Background(), a, p, dest); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(dest, "bin", "run.sh"))
	if err != nil || string(b) != "xxx" {
		t.Fatalf("staged: %q %v", b, err)
	}
	if err := Stage(context.Background(), a, p, dest); err == nil {
		t.Fatal("immutable release overwritten")
	}
}

func TestRejectUnsafeArchives(t *testing.T) {
	cases := map[string][]*tar.Header{
		"absolute":  {{Name: "/etc/file", Mode: 0600, Typeflag: tar.TypeReg}},
		"parent":    {{Name: "../file", Mode: 0600, Typeflag: tar.TypeReg}},
		"backslash": {{Name: `..\file`, Mode: 0600, Typeflag: tar.TypeReg}},
		"drive":     {{Name: "C:/file", Mode: 0600, Typeflag: tar.TypeReg}},
		"symlink":   {{Name: "link", Linkname: "../../data", Typeflag: tar.TypeSymlink}},
		"hardlink":  {{Name: "link", Linkname: "data", Typeflag: tar.TypeLink}},
		"device":    {{Name: "device", Typeflag: tar.TypeChar}},
		"duplicate": {{Name: "same", Typeflag: tar.TypeReg}, {Name: "same", Typeflag: tar.TypeReg}},
		"setuid":    {{Name: "run", Typeflag: tar.TypeReg, Mode: 04755}},
	}
	for name, headers := range cases {
		t.Run(name, func(t *testing.T) {
			a, p := archive(t, headers)
			dest := filepath.Join(t.TempDir(), "candidate")
			if err := Stage(context.Background(), a, p, dest); err == nil {
				t.Fatal("unsafe archive accepted")
			}
			if _, err := os.Stat(dest); !os.IsNotExist(err) {
				t.Fatal("failed candidate visible")
			}
		})
	}
}

func TestRejectWrongHashUnsupportedCompressionAndCancellation(t *testing.T) {
	a, p := archive(t, []*tar.Header{{Name: "ok", Typeflag: tar.TypeReg}})
	for _, kind := range []string{"hash", "compression", "cancel"} {
		t.Run(kind, func(t *testing.T) {
			b := a
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if kind == "hash" {
				b.SHA256 = "bad"
			}
			if kind == "compression" {
				b.Compression = "xz"
			}
			if kind == "cancel" {
				cancel()
			}
			if err := Stage(ctx, b, p, filepath.Join(t.TempDir(), "candidate")); err == nil {
				t.Fatal("invalid stage accepted")
			}
		})
	}
}
