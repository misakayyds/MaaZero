package runtime

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	testRootfsPath  = "/data/local/maa-platform/runtime/rootfs/12.0.1"
	testProjectPath = "/data/local/maa-platform/runtime/project/0.1.0"
)

func bundleJSON(body string) string { return "{\"schema\":1,\"components\":[" + body + "]}" }

func validComponentsJSON() string {
	return `{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime"},` +
		`{"id":"maa-phone-project","version":"0.1.0","path":"` + testProjectPath + `","kind":"project"}`
}

func TestLoadBundleAcceptance(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want error
	}{
		{
			name: "valid",
			raw:  bundleJSON(validComponentsJSON()),
		},
		{
			name: "schema omitted",
			raw:  `{"components":[{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime"},{"id":"maa-phone-project","version":"0.1.0","path":"` + testProjectPath + `","kind":"project"}]}`,
		},
		{
			name: "unknown field",
			raw:  `{"components":[{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime","extra":1},{"id":"maa-phone-project","version":"0.1.0","path":"` + testProjectPath + `","kind":"project"}]}`,
			want: ErrInvalidBundle,
		},
		{
			name: "trailing content",
			raw:  bundleJSON(validComponentsJSON()) + `{"id":"x"}`,
			want: ErrInvalidBundle,
		},
		{name: "no components", raw: `{"components":[]}`, want: ErrInvalidBundle},
		{
			name: "duplicate id",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"1","path":"` + testRootfsPath + `","kind":"runtime"},{"id":"debian-rootfs","version":"1","path":"` + testProjectPath + `","kind":"project"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "legacy rootfs kind",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"rootfs"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "upstream kind",
			raw:  bundleJSON(`{"id":"maa-cli","version":"0.7.5","path":"` + testProjectPath + `","kind":"upstream"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "missing project",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "two rootfs",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime"},{"id":"other-rootfs","version":"1","path":"` + testProjectPath + `","kind":"runtime"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "rootfs id mismatch",
			raw:  bundleJSON(`{"id":"runtime","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "bad version",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"-bad","path":"` + testRootfsPath + `","kind":"runtime"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "relative path",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"12.0.1","path":"runtime/rootfs","kind":"runtime"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "duplicate path",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `","kind":"runtime"},{"id":"maa-phone-project","version":"0.1.0","path":"` + testRootfsPath + `","kind":"project"}`),
			want: ErrInvalidBundle,
		},
		{
			name: "escaping path",
			raw:  bundleJSON(`{"id":"debian-rootfs","version":"12.0.1","path":"` + testRootfsPath + `/../etc","kind":"runtime"}`),
			want: ErrInvalidBundle,
		},
		{name: "not an object", raw: `[]`, want: ErrInvalidBundle},
		{name: "empty file", raw: ``, want: ErrInvalidBundle},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			writeTestFile(t, filepath.Join(dir, bundleFileName), tc.raw, 0o644)
			bundle, err := loadBundle(dir)
			if tc.want == nil {
				if err != nil {
					t.Fatalf("loadBundle: %v", err)
				}
				if (bundle.Schema != 0 && bundle.Schema != 1) || len(bundle.Components) != 2 {
					t.Fatalf("unexpected bundle %#v", bundle)
				}
				if c, ok := bundle.componentByKind(kindRuntime); !ok || c.ID != rootfsComponentID {
					t.Fatalf("rootfs component = %#v, %v", c, ok)
				}
				return
			}
			if !errors.Is(err, tc.want) {
				t.Fatalf("loadBundle = %v, want %v", err, tc.want)
			}
		})
	}
}

func TestLoadBundleRejectsOversizedFile(t *testing.T) {
	dir := t.TempDir()
	big := strings.Repeat("x", maxBundleBytes+1)
	writeTestFile(t, filepath.Join(dir, bundleFileName), big, 0o644)
	if _, err := loadBundle(dir); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("loadBundle = %v, want ErrInvalidBundle", err)
	}
}

func TestLoadBundleRejectsDirectoryWithoutManifest(t *testing.T) {
	if _, err := loadBundle(t.TempDir()); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("loadBundle = %v, want ErrInvalidBundle", err)
	}
	if _, err := loadBundle("relative"); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("loadBundle(relative) = %v, want ErrInvalidBundle", err)
	}
	file := filepath.Join(t.TempDir(), "file")
	writeTestFile(t, file, "x", 0o644)
	if _, err := loadBundle(file); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("loadBundle(file) = %v, want ErrInvalidBundle", err)
	}
}

func TestLoadBundleIgnoresDirectorySymlink(t *testing.T) {
	real := t.TempDir()
	writeTestFile(t, filepath.Join(real, bundleFileName), bundleJSON(
		`{"id":"debian-rootfs","version":"12.0.1","path":"`+testRootfsPath+`","kind":"runtime"},{"id":"maa-phone-project","version":"0.1.0","path":"`+testProjectPath+`","kind":"project"}`), 0o644)
	link := filepath.Join(t.TempDir(), "bundle")
	if err := os.Symlink(real, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if _, err := loadBundle(link); err != nil {
		t.Fatalf("loadBundle(link) = %v", err)
	}
}
