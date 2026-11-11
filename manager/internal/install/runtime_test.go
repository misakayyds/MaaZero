package install

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"maa-phone-manager/manager/internal/manifest"
)

// Debian rootfs archives contain real symlinks and hardlinks, so the rootfs
// policy cannot reject every link the way the project tar.gz policy does.
// These tests stage real archives in real temporary directories and check the
// verified-before-extract contract, the link rewriting, the resource bounds,
// cancellation and the no-candidate-on-failure guarantee.

type rootfsEntry struct {
	Name     string
	Type     byte
	Mode     int64
	Linkname string
	Body     string
}

func sha256Hex(b []byte) string { return fmt.Sprintf("%x", sha256.Sum256(b)) }

func buildRootfsArchive(t *testing.T, ents []rootfsEntry) (manifest.Artifact, string) {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, e := range ents {
		h := &tar.Header{Name: e.Name, Typeflag: e.Type, Mode: e.Mode, Linkname: e.Linkname}
		if h.Typeflag == 0 {
			h.Typeflag = tar.TypeReg
		}
		if h.Typeflag == tar.TypeReg || h.Typeflag == tar.TypeRegA {
			h.Size = int64(len(e.Body))
			if h.Mode == 0 {
				h.Mode = 0644
			}
		} else if h.Typeflag == tar.TypeDir && h.Mode == 0 {
			h.Mode = 0755
		}
		if err := tw.WriteHeader(h); err != nil {
			t.Fatalf("write tar header %q: %v", e.Name, err)
		}
		if h.Size > 0 {
			if _, err := tw.Write([]byte(e.Body)); err != nil {
				t.Fatalf("write tar body %q: %v", e.Name, err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(t.TempDir(), "rootfs.tar.gz")
	if err := os.WriteFile(p, buf.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	a := manifest.Artifact{Compression: "tar.gz", Size: int64(buf.Len()), SHA256: sha256Hex(buf.Bytes())}
	return a, p
}

func assertNoStagingLeftovers(t *testing.T, parent string) {
	t.Helper()
	ents, err := os.ReadDir(parent)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range ents {
		if strings.HasPrefix(e.Name(), ".staging-") {
			t.Fatalf("staging directory %q left behind in %s", e.Name(), parent)
		}
	}
}

func assertNoCandidate(t *testing.T, dest string) {
	t.Helper()
	if _, err := os.Lstat(dest); !os.IsNotExist(err) {
		t.Fatalf("release candidate %s exists after an error: %v", dest, err)
	}
}

func stageRootfsExpectError(t *testing.T, ents []rootfsEntry) error {
	t.Helper()
	a, p := buildRootfsArchive(t, ents)
	parent := t.TempDir()
	dest := filepath.Join(parent, "candidate")
	_, err := StageRootfs(context.Background(), a, p, dest)
	if err == nil {
		t.Fatalf("unsafe rootfs archive was staged into %s", dest)
	}
	assertNoCandidate(t, dest)
	assertNoStagingLeftovers(t, parent)
	if errors.Is(err, ErrPublished) {
		t.Fatalf("pre-publication failure reported a published release: %v", err)
	}
	return err
}

func readStaged(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}

func assertLinkTarget(t *testing.T, path, want string) {
	t.Helper()
	got, err := os.Readlink(path)
	if err != nil {
		t.Fatalf("readlink %s: %v", path, err)
	}
	if got != want {
		t.Fatalf("link %s targets %q, want %q", path, got, want)
	}
}

func requireSymlinks(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, []byte("x"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, filepath.Join(dir, "link")); err != nil {
		t.Skipf("symlinks unavailable on this host: %v", err)
	}
}

func requireHardlinks(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, []byte("x"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(target, filepath.Join(dir, "link")); err != nil {
		t.Skipf("hardlinks unavailable on this host: %v", err)
	}
}

func TestStageRootfsExtractsRegularTree(t *testing.T) {
	a, p := buildRootfsArchive(t, []rootfsEntry{
		{Name: "./", Type: tar.TypeDir, Mode: 0755},
		{Name: "./etc", Type: tar.TypeDir, Mode: 0755},
		{Name: "./etc/os-release", Mode: 0644, Body: "ID=debian\n"},
		{Name: "./usr", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr/bin", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr/bin/sh", Mode: 0755, Body: "#!/bin/sh\n"},
		{Name: "./usr/bin/su", Mode: 04755, Body: "su\n"},
		{Name: "./var/lib/dpkg/status", Mode: 0644, Body: "Package: base-files\n"},
	})
	parent := t.TempDir()
	sentinel := filepath.Join(parent, "sentinel")
	if err := os.WriteFile(sentinel, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(parent, "1.0.0")
	stats, err := StageRootfs(context.Background(), a, p, dest)
	if err != nil {
		t.Fatalf("stage rootfs: %v", err)
	}
	if stats.Files != 4 {
		t.Fatalf("files = %d, want 4 (%+v)", stats.Files, stats)
	}
	if stats.Dirs != 6 {
		t.Fatalf("dirs = %d, want 6 including implicit parents (%+v)", stats.Dirs, stats)
	}
	if stats.Symlinks != 0 || stats.Hardlinks != 0 {
		t.Fatalf("unexpected links in a link-free archive: %+v", stats)
	}
	if stats.SpecialBitsStripped != 1 {
		t.Fatalf("setuid entry was not reported as stripped: %+v", stats)
	}
	wantBytes := int64(len("ID=debian\n") + len("#!/bin/sh\n") + len("su\n") + len("Package: base-files\n"))
	if stats.ExpandedBytes != wantBytes {
		t.Fatalf("expanded bytes = %d, want %d", stats.ExpandedBytes, wantBytes)
	}
	if got := readStaged(t, filepath.Join(dest, "etc", "os-release")); got != "ID=debian\n" {
		t.Fatalf("os-release = %q", got)
	}
	if got := readStaged(t, filepath.Join(dest, "var", "lib", "dpkg", "status")); got != "Package: base-files\n" {
		t.Fatalf("dpkg status = %q", got)
	}
	if got := readStaged(t, sentinel); got != "keep" {
		t.Fatalf("sibling file was modified: %q", got)
	}
	ents, err := os.ReadDir(parent)
	if err != nil {
		t.Fatal(err)
	}
	if len(ents) != 2 {
		t.Fatalf("parent gained unexpected entries: %v", ents)
	}
	if runtime.GOOS != "windows" {
		if mode := stagedMode(t, dest); mode != 0755 {
			t.Fatalf("release directory mode = %04o, want the chroot root mode 0755", mode)
		}
		if mode := stagedMode(t, filepath.Join(dest, "var")); mode != 0755 {
			t.Fatalf("implicit directory mode = %04o, want 0755", mode)
		}
		if mode := stagedMode(t, filepath.Join(dest, "usr", "bin", "sh")); mode != 0755 {
			t.Fatalf("executable mode = %04o, want 0755", mode)
		}
		if mode := stagedMode(t, filepath.Join(dest, "etc", "os-release")); mode != 0644 {
			t.Fatalf("regular file mode = %04o, want 0644", mode)
		}
		if mode := stagedMode(t, filepath.Join(dest, "usr", "bin", "su")); mode&07000 != 0 {
			t.Fatalf("setuid bit survived extraction: %04o", mode)
		}
	}
}

func stagedMode(t *testing.T, path string) os.FileMode {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatalf("lstat %s: %v", path, err)
	}
	return info.Mode().Perm()
}

func TestStageRootfsRewritesLinksAndHardlinks(t *testing.T) {
	requireSymlinks(t)
	requireHardlinks(t)
	a, p := buildRootfsArchive(t, []rootfsEntry{
		{Name: "./", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr/bin", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr/bin/sh", Mode: 0755, Body: "shell\n"},
		{Name: "./usr/bin/gawk", Mode: 0755, Body: "gawk\n"},
		{Name: "./usr/lib", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr/lib/aarch64-linux-gnu", Type: tar.TypeDir, Mode: 0755},
		{Name: "./usr/lib/aarch64-linux-gnu/libc.so.6", Mode: 0755, Body: "libc\n"},
		{Name: "./bin", Type: tar.TypeDir, Mode: 0755},
		{Name: "./etc", Type: tar.TypeDir, Mode: 0755},
		{Name: "./bin/sh", Type: tar.TypeSymlink, Linkname: "/usr/bin/sh"},
		{Name: "./bin/gawk", Type: tar.TypeSymlink, Linkname: "/usr/bin/gawk"},
		{Name: "./usr/bin/awk", Type: tar.TypeSymlink, Linkname: "gawk"},
		{Name: "./lib64", Type: tar.TypeSymlink, Linkname: "/usr/lib"},
		{Name: "./etc/mtab", Type: tar.TypeSymlink, Linkname: "/proc/mounts"},
		{Name: "./usr/bin/sh2", Type: tar.TypeLink, Linkname: "./usr/bin/sh"},
		{Name: "./late-target", Mode: 0644, Body: "late\n"},
		{Name: "./late-link", Type: tar.TypeLink, Linkname: "late-target"},
	})
	dest := filepath.Join(t.TempDir(), "rootfs")
	stats, err := StageRootfs(context.Background(), a, p, dest)
	if err != nil {
		t.Fatalf("stage rootfs: %v", err)
	}
	if stats.Symlinks != 5 {
		t.Fatalf("symlinks = %d, want 5 (%+v)", stats.Symlinks, stats)
	}
	if stats.Hardlinks != 2 {
		t.Fatalf("hardlinks = %d, want 2 (%+v)", stats.Hardlinks, stats)
	}
	// In-root absolute targets are rewritten to the equivalent relative target
	// so a root-relative extraction keeps the same meaning without absolute
	// symlinks.
	assertLinkTarget(t, filepath.Join(dest, "bin", "sh"), "../usr/bin/sh")
	assertLinkTarget(t, filepath.Join(dest, "bin", "gawk"), "../usr/bin/gawk")
	assertLinkTarget(t, filepath.Join(dest, "usr", "bin", "awk"), "gawk")
	assertLinkTarget(t, filepath.Join(dest, "lib64"), "usr/lib")
	assertLinkTarget(t, filepath.Join(dest, "etc", "mtab"), "../proc/mounts")
	// The rewritten text must stay inside the release directory; a link that
	// resolves above it would drag path-based consumers out of the version.
	assertLinksStayInside(t, dest)
	// Reading back through a rewritten link is asserted where the OS keeps
	// forward-slash link targets verbatim (Linux/Android, the production target).
	// Windows stores them as written and then cannot resolve them, and rewriting
	// to backslashes would corrupt a Linux rootfs, so on Windows only the
	// rewritten text above is asserted.
	if runtime.GOOS != "windows" {
		if got := readStaged(t, filepath.Join(dest, "bin", "sh")); got != "shell\n" {
			t.Fatalf("rewritten symlink did not resolve in-root: %q", got)
		}
		if got := readStaged(t, filepath.Join(dest, "lib64", "aarch64-linux-gnu", "libc.so.6")); got != "libc\n" {
			t.Fatalf("rewritten directory symlink did not resolve: %q", got)
		}
	}
	if got := readStaged(t, filepath.Join(dest, "late-link")); got != "late\n" {
		t.Fatalf("forward hardlink was not created: %q", got)
	}
	sh, err := os.Stat(filepath.Join(dest, "usr", "bin", "sh"))
	if err != nil {
		t.Fatal(err)
	}
	sh2, err := os.Stat(filepath.Join(dest, "usr", "bin", "sh2"))
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(sh, sh2) {
		t.Fatal("hardlink usr/bin/sh2 is not the same file as usr/bin/sh")
	}
	lateTarget, err := os.Stat(filepath.Join(dest, "late-target"))
	if err != nil {
		t.Fatal(err)
	}
	lateLink, err := os.Stat(filepath.Join(dest, "late-link"))
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(lateTarget, lateLink) {
		t.Fatal("forward hardlink is not the same file as its target")
	}
}

func TestStageRootfsRejectsUnsafeArchives(t *testing.T) {
	cases := map[string][]rootfsEntry{
		"absolute member":        {{Name: "/etc/passwd", Body: "x"}},
		"parent traversal":       {{Name: "../escape", Body: "x"}},
		"backslash ambiguity":    {{Name: `..\escape`, Body: "x"}},
		"drive letter":           {{Name: "C:/windows", Body: "x"}},
		"alternate data stream":  {{Name: "usr/bin/sh:stream", Body: "x"}},
		"non canonical":          {{Name: "usr//bin/sh", Body: "x"}},
		"dot component":          {{Name: "usr/./bin/sh", Body: "x"}},
		"trailing dot":           {{Name: "usr/bin./sh", Body: "x"}},
		"reserved device name":   {{Name: "usr/bin/NUL", Body: "x"}},
		"duplicate entry":        {{Name: "./usr/x", Body: "a"}, {Name: "usr/x", Body: "b"}},
		"character device":       {{Name: "dev/null", Type: tar.TypeChar}},
		"block device":           {{Name: "dev/sda", Type: tar.TypeBlock}},
		"fifo":                   {{Name: "run/fifo", Type: tar.TypeFifo}},
		"root marker as file":    {{Name: ".", Body: "x"}},
		"empty archive":          {},
		"symlink escape":         {{Name: "lib/x", Type: tar.TypeSymlink, Linkname: "../../etc/passwd"}},
		"symlink root pivot":     {{Name: "x", Type: tar.TypeSymlink, Linkname: ".."}},
		"symlink absolute pivot": {{Name: "lib/x", Type: tar.TypeSymlink, Linkname: "/../etc/passwd"}},
		"symlink backslash":      {{Name: "lib/x", Type: tar.TypeSymlink, Linkname: `..\..\etc`}},
		"symlink drive":          {{Name: "lib/x", Type: tar.TypeSymlink, Linkname: "C:/windows"}},
		"symlink empty target":   {{Name: "lib/x", Type: tar.TypeSymlink, Linkname: ""}},
		"hardlink missing":       {{Name: "x", Type: tar.TypeLink, Linkname: "missing"}},
		"hardlink to directory":  {{Name: "d", Type: tar.TypeDir, Mode: 0755}, {Name: "x", Type: tar.TypeLink, Linkname: "d"}},
		"hardlink to symlink":    {{Name: "s", Type: tar.TypeSymlink, Linkname: "/etc"}, {Name: "x", Type: tar.TypeLink, Linkname: "s"}},
		"hardlink chain": {
			{Name: "a", Body: "a"},
			{Name: "b", Type: tar.TypeLink, Linkname: "a"},
			{Name: "c", Type: tar.TypeLink, Linkname: "b"},
		},
		"hardlink escape": {{Name: "a", Body: "a"}, {Name: "x", Type: tar.TypeLink, Linkname: "../a"}},
	}
	for name, ents := range cases {
		t.Run(name, func(t *testing.T) {
			stageRootfsExpectError(t, ents)
		})
	}
}

func TestStageRootfsRejectsUnsupportedCompressionAndBadArtifacts(t *testing.T) {
	base, p := buildRootfsArchive(t, []rootfsEntry{{Name: "usr/bin/sh", Body: "x"}})
	for _, kind := range []string{"compression-none", "compression-zip", "hash", "size", "zero-size", "missing-file", "directory-argument"} {
		t.Run(kind, func(t *testing.T) {
			a := base
			path := p
			switch kind {
			case "compression-none":
				a.Compression = "none"
			case "compression-zip":
				a.Compression = "zip"
			case "hash":
				a.SHA256 = sha256Hex([]byte("other"))
			case "size":
				a.Size++
			case "zero-size":
				a.Size = 0
			case "missing-file":
				path = filepath.Join(t.TempDir(), "absent.tar.gz")
			case "directory-argument":
				path = t.TempDir()
			}
			parent := t.TempDir()
			dest := filepath.Join(parent, "candidate")
			if _, err := StageRootfs(context.Background(), a, path, dest); err == nil {
				t.Fatalf("%s artifact was staged", kind)
			}
			assertNoCandidate(t, dest)
			assertNoStagingLeftovers(t, parent)
		})
	}
}

func TestStageRootfsEnforcesBounds(t *testing.T) {
	orig := rootfsLimits
	defer func() { rootfsLimits = orig }()

	t.Run("expanded bytes", func(t *testing.T) {
		rootfsLimits = orig
		rootfsLimits.MaxExpanded = 16
		err := stageRootfsExpectError(t, []rootfsEntry{{Name: "usr/bin/big", Body: strings.Repeat("A", 64)}})
		if !strings.Contains(err.Error(), "limit") {
			t.Fatalf("unexpected error: %v", err)
		}
	})
	t.Run("entry count", func(t *testing.T) {
		rootfsLimits = orig
		rootfsLimits.MaxEntries = 2
		err := stageRootfsExpectError(t, []rootfsEntry{{Name: "a", Body: "a"}, {Name: "b", Body: "b"}, {Name: "c", Body: "c"}})
		if !strings.Contains(err.Error(), "too many") {
			t.Fatalf("unexpected error: %v", err)
		}
	})
	t.Run("archive size", func(t *testing.T) {
		rootfsLimits = orig
		rootfsLimits.MaxArchive = 8
		a, p := buildRootfsArchive(t, []rootfsEntry{{Name: "a", Body: "aaa"}})
		if _, err := StageRootfs(context.Background(), a, p, filepath.Join(t.TempDir(), "candidate")); err == nil {
			t.Fatal("oversized archive accepted")
		}
	})
}

type countingContext struct {
	context.Context
	limit int
	calls int
}

func (c *countingContext) Err() error {
	c.calls++
	if c.calls > c.limit {
		return context.Canceled
	}
	return nil
}

func TestRootfsLimitsStayBoundedForPhoneMemory(t *testing.T) {
	// Pinned policy: one extraction must not hold an unbounded name index on a
	// phone that is also running MAA. A minimal Debian rootfs is roughly 20-30k
	// entries, so 100k is head-room. The size ceilings stay at 8 GiB, and this
	// package deliberately has no statfs precheck, no stale .staging-* reclaim
	// and no full-disk recovery: those belong to the caller that owns the lock.
	if rootfsLimits.MaxEntries != 100_000 {
		t.Fatalf("rootfs entry bound = %d, want 100000", rootfsLimits.MaxEntries)
	}
	if rootfsLimits.MaxArchive != 8<<30 || rootfsLimits.MaxExpanded != 8<<30 {
		t.Fatalf("rootfs size ceilings changed: %+v", rootfsLimits)
	}
	if binaryLimits.MaxBytes != 256<<20 {
		t.Fatalf("manager binary ceiling = %d, want 256 MiB", binaryLimits.MaxBytes)
	}
}

func TestStageRootfsCancellation(t *testing.T) {
	t.Run("before start", func(t *testing.T) {
		a, p := buildRootfsArchive(t, []rootfsEntry{{Name: "usr/bin/sh", Body: "x"}})
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		parent := t.TempDir()
		dest := filepath.Join(parent, "candidate")
		if _, err := StageRootfs(ctx, a, p, dest); !errors.Is(err, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v", err)
		}
		assertNoCandidate(t, dest)
		assertNoStagingLeftovers(t, parent)
	})
	t.Run("mid stream", func(t *testing.T) {
		var ents []rootfsEntry
		for i := 0; i < 60; i++ {
			ents = append(ents, rootfsEntry{Name: fmt.Sprintf("usr/share/doc/pkg/file%02d", i), Body: strings.Repeat("x", 128)})
		}
		a, p := buildRootfsArchive(t, ents)
		parent := t.TempDir()
		dest := filepath.Join(parent, "candidate")
		// Enough Err calls for verification to finish, then cancellation lands
		// while entries are already being written.
		ctx := &countingContext{Context: context.Background(), limit: 8}
		if _, err := StageRootfs(ctx, a, p, dest); !errors.Is(err, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v (Err calls=%d)", err, ctx.calls)
		}
		assertNoCandidate(t, dest)
		assertNoStagingLeftovers(t, parent)
	})
}

func TestStageRootfsKeepsReleasesImmutable(t *testing.T) {
	a, p := buildRootfsArchive(t, []rootfsEntry{{Name: "usr/bin/sh", Body: "first\n"}})
	parent := t.TempDir()
	dest := filepath.Join(parent, "1.0.0")
	if _, err := StageRootfs(context.Background(), a, p, dest); err != nil {
		t.Fatal(err)
	}
	other, otherPath := buildRootfsArchive(t, []rootfsEntry{{Name: "usr/bin/sh", Body: "second\n"}})
	if _, err := StageRootfs(context.Background(), other, otherPath, dest); err == nil {
		t.Fatal("existing release was overwritten")
	}
	if got := readStaged(t, filepath.Join(dest, "usr", "bin", "sh")); got != "first\n" {
		t.Fatalf("existing release content changed: %q", got)
	}
	fileParent := t.TempDir()
	fileDest := filepath.Join(fileParent, "2.0.0")
	if err := os.WriteFile(fileDest, []byte("occupied"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := StageRootfs(context.Background(), a, p, fileDest); err == nil {
		t.Fatal("staging over an occupied path succeeded")
	}
	if got := readStaged(t, fileDest); got != "occupied" {
		t.Fatalf("occupied path changed: %q", got)
	}
	assertNoStagingLeftovers(t, fileParent)
}

func TestOpenVerifiedArtifactKeepsVerifiedHandle(t *testing.T) {
	payload := bytes.Repeat([]byte("verified-bytes"), 64)
	a, p := buildManagerArtifact(t, payload)
	f, err := openVerifiedArtifact(context.Background(), a, p)
	if err != nil {
		t.Fatalf("verify artifact: %v", err)
	}
	defer f.Close()
	// Portable part: the returned handle is the verified artifact, rewound to the
	// start and still serving the reviewed bytes.
	handleInfo, err := f.Stat()
	if err != nil {
		t.Fatal(err)
	}
	pathInfo, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(handleInfo, pathInfo) {
		t.Fatal("verified handle is not the artifact file")
	}
	if got, err := io.ReadAll(f); err != nil || !bytes.Equal(got, payload) {
		t.Fatalf("verified handle served %d bytes (err=%v)", len(got), err)
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	// Swapping the path must not change what extraction reads: the verified
	// handle is the only source of bytes.
	if err := os.Remove(p); err != nil {
		t.Skipf("cannot replace an open artifact on this host: %v", err)
	}
	if err := os.WriteFile(p, []byte("swapped"), 0600); err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(f)
	if err != nil {
		t.Fatalf("read verified handle: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("verified handle served %d bytes of unreviewed content", len(got))
	}
	if _, err := openVerifiedArtifact(context.Background(), a, p); err == nil {
		t.Fatal("swapped artifact passed verification")
	}
}

func TestStageRootfsRejectsLinkDescendants(t *testing.T) {
	// Physical creation beneath an archive link member is what lets a link text
	// escape the release directory, so every entry whose ancestor is a link
	// (symlink or hardlink, including one declared later in the archive) is
	// refused before anything is written. The rule is lexical, so this test runs
	// on every host, not only where symlinks can be created.
	cases := map[string][]rootfsEntry{
		"symlink parent":          {{Name: "a", Type: tar.TypeSymlink, Linkname: "/"}, {Name: "a/b", Type: tar.TypeSymlink, Linkname: "/etc/passwd"}},
		"nested symlink parent":   {{Name: "a", Type: tar.TypeSymlink, Linkname: "/"}, {Name: "a/a/a/b", Type: tar.TypeSymlink, Linkname: "/etc/passwd"}},
		"forward symlink parent":  {{Name: "a/b", Type: tar.TypeSymlink, Linkname: "/etc/passwd"}, {Name: "a", Type: tar.TypeSymlink, Linkname: "/"}},
		"forward nested parent":   {{Name: "a/b/c/d", Type: tar.TypeReg, Body: "x"}, {Name: "a/b", Type: tar.TypeSymlink, Linkname: "/"}},
		"file under symlink":      {{Name: "a", Type: tar.TypeSymlink, Linkname: "/usr"}, {Name: "a/b", Type: tar.TypeReg, Body: "x"}},
		"directory under symlink": {{Name: "a", Type: tar.TypeSymlink, Linkname: "/usr"}, {Name: "a/b", Type: tar.TypeDir}},
		"hardlink parent":         {{Name: "a", Type: tar.TypeReg, Body: "a"}, {Name: "h", Type: tar.TypeLink, Linkname: "a"}, {Name: "h/b", Type: tar.TypeReg, Body: "x"}},
		"merged usr alias":        {{Name: "bin", Type: tar.TypeSymlink, Linkname: "/usr/bin"}, {Name: "bin/sh", Type: tar.TypeSymlink, Linkname: "/usr/bin/sh"}},
	}
	for name, ents := range cases {
		t.Run(name, func(t *testing.T) {
			err := stageRootfsExpectError(t, ents)
			if !strings.Contains(err.Error(), "beneath") {
				t.Fatalf("want a link-descendant rejection, got %v", err)
			}
		})
	}
}

func TestStageRootfsHonorsArchiveRootMode(t *testing.T) {
	// The release directory is the chroot "/": a private 0700 root would lock
	// non-root chroot processes (for example apt's _apt user) out of the tree, so
	// an explicit archive root mode is honored and an absent one defaults to 0755.
	t.Run("explicit root mode", func(t *testing.T) {
		a, p := buildRootfsArchive(t, []rootfsEntry{
			{Name: ".", Type: tar.TypeDir, Mode: 0700},
			{Name: "var/cache/apt/archives/keep", Mode: 0644, Body: "cache\n"},
		})
		dest := filepath.Join(t.TempDir(), "rootfs")
		stats, err := StageRootfs(context.Background(), a, p, dest)
		if err != nil {
			t.Fatalf("stage rootfs: %v", err)
		}
		if stats.Files != 1 || stats.Dirs != 4 {
			t.Fatalf("unexpected stats: %+v", stats)
		}
		if runtime.GOOS == "windows" {
			return
		}
		if got := stagedMode(t, dest); got != 0700 {
			t.Fatalf("archive root mode ignored: got %04o want 0700", got)
		}
		for _, dir := range []string{"var", "var/cache", "var/cache/apt", "var/cache/apt/archives"} {
			if got := stagedMode(t, filepath.Join(dest, filepath.FromSlash(dir))); got != 0755 {
				t.Fatalf("implicit directory %s mode = %04o, want 0755", dir, got)
			}
		}
	})
	t.Run("default root mode", func(t *testing.T) {
		a, p := buildRootfsArchive(t, []rootfsEntry{{Name: "usr/bin/sh", Mode: 0755, Body: "shell\n"}})
		dest := filepath.Join(t.TempDir(), "rootfs")
		if _, err := StageRootfs(context.Background(), a, p, dest); err != nil {
			t.Fatalf("stage rootfs: %v", err)
		}
		if runtime.GOOS == "windows" {
			return
		}
		if got := stagedMode(t, dest); got != 0755 {
			t.Fatalf("default release root mode = %04o, want 0755", got)
		}
	})
}

func TestStageRootfsRejectionsNeverClaimPublished(t *testing.T) {
	// Every rejection path in this file runs through stageRootfsExpectError, which
	// asserts both "no release candidate" and "no published claim": a pre-
	// publication failure must never look like a release that exists on disk.
	err := stageRootfsExpectError(t, []rootfsEntry{
		{Name: "bin", Type: tar.TypeSymlink, Linkname: "/usr/bin"},
		{Name: "bin/sh", Type: tar.TypeSymlink, Linkname: "/usr/bin/sh"},
	})
	if errors.Is(err, ErrPublished) {
		t.Fatalf("rejection reported a published release: %v", err)
	}
}

func TestReleaseStagingRefusesLateRelease(t *testing.T) {
	// A competing writer can publish the same version after the exclusive check.
	// commit must then refuse instead of overwriting, and cleanup must remove only
	// its own staging directory.
	parent := t.TempDir()
	stg, err := newReleaseStaging(parent, "1.0.0")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(parent, "1.0.0"), 0700); err != nil {
		t.Fatal(err)
	}
	published, commitErr := stg.commit(context.Background(), nil)
	if commitErr == nil || published {
		t.Fatalf("commit = (%v, %v), want a pre-publication failure", published, commitErr)
	}
	if errors.Is(commitErr, ErrPublished) {
		t.Fatalf("pre-publication failure reported a published release: %v", commitErr)
	}
	if err := stg.finish(); err != nil {
		t.Fatalf("finish: %v", err)
	}
	assertNoStagingLeftovers(t, parent)
	if _, err := os.Lstat(filepath.Join(parent, "1.0.0")); err != nil {
		t.Fatalf("the competing release was removed: %v", err)
	}
}

func TestReleaseStagingPublishesThenCleansUpOnlyItself(t *testing.T) {
	parent := t.TempDir()
	stg, err := newReleaseStaging(parent, "2.0.0")
	if err != nil {
		t.Fatal(err)
	}
	if err := stg.dir.WriteFile("payload", []byte("data"), 0600); err != nil {
		t.Fatal(err)
	}
	published, err := stg.commit(context.Background(), nil)
	if err != nil || !published {
		t.Fatalf("commit = (%v, %v), want a published release", published, err)
	}
	if err := stg.finish(); err != nil {
		t.Fatalf("finish after publish: %v", err)
	}
	if err := stg.finish(); err != nil {
		t.Fatalf("finish is not idempotent: %v", err)
	}
	if got := readStaged(t, filepath.Join(parent, "2.0.0", "payload")); got != "data" {
		t.Fatalf("published release changed after cleanup: %q", got)
	}
	assertNoStagingLeftovers(t, parent)
}

// assertLinksStayInside walks a staged release and proves no link text resolves
// above the release directory: a link that escapes its own version directory
// would drag every path-based consumer (walker, packer, verifier) outside it.
func assertLinksStayInside(t *testing.T, dest string) {
	t.Helper()
	err := filepath.WalkDir(dest, func(p string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.Type()&os.ModeSymlink == 0 {
			return nil
		}
		target, err := os.Readlink(p)
		if err != nil {
			return err
		}
		const backslashByte = 92 // ASCII backslash, never valid in a Linux rootfs link text
		if path.IsAbs(target) || strings.IndexByte(target, backslashByte) >= 0 {
			return fmt.Errorf("link %s has a non-relative target %q", p, target)
		}
		rel, err := filepath.Rel(dest, p)
		if err != nil {
			return err
		}
		resolved := path.Join(path.Dir(filepath.ToSlash(rel)), target)
		if resolved == ".." || strings.HasPrefix(resolved, "../") {
			return fmt.Errorf("link %s targets outside the release directory: %q", rel, target)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestExtractRootfsRefusesLinkSetDrift(t *testing.T) {
	// The indexed link set is what made "no entry beneath a link" complete,
	// including forward references. The extraction pass must see exactly the same
	// link members before it creates any link, otherwise the analysis describes a
	// different archive than the bytes being extracted.
	_, indexPath := buildRootfsArchive(t, []rootfsEntry{{Name: "a", Type: tar.TypeSymlink, Linkname: "/"}})
	_, indexTar := openRootfsTar(t, indexPath)
	links, err := scanRootfs(context.Background(), indexTar)
	if err != nil {
		t.Fatalf("index archive: %v", err)
	}
	// Simulate a source rewritten between passes: the member names still exist, the
	// link members differ, and no entry sits beneath a link, so only the set
	// comparison can catch it.
	_, driftPath := buildRootfsArchive(t, []rootfsEntry{
		{Name: "a", Mode: 0644, Body: "x"},
		{Name: "c", Type: tar.TypeSymlink, Linkname: "/etc/passwd"},
	})
	_, driftTar := openRootfsTar(t, driftPath)
	root, err := os.OpenRoot(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if _, err := extractRootfs(context.Background(), driftTar, root, links, newRootfsPlan()); err == nil {
		t.Fatal("an archive whose link set drifted between passes was extracted")
	} else if !strings.Contains(err.Error(), "changed between indexing and extraction") {
		t.Fatalf("unexpected drift error: %v", err)
	}
	if _, err := root.Lstat("c"); err == nil {
		t.Fatal("a link was created from an archive whose link set drifted")
	}
}

func TestStageRootfsRejectionReturnsZeroStats(t *testing.T) {
	// A failure before publication must not hand back statistics that look like a
	// produced release.
	a, p := buildRootfsArchive(t, []rootfsEntry{{Name: "usr/bin/sh", Mode: 0755, Body: "shell\n"}})
	a.SHA256 = sha256Hex([]byte("other"))
	stats, err := StageRootfs(context.Background(), a, p, filepath.Join(t.TempDir(), "candidate"))
	if err == nil {
		t.Fatal("a bad artifact hash was accepted")
	}
	if stats != (RootfsStats{}) {
		t.Fatalf("failed stage reported statistics %+v", stats)
	}
}

// openRootfsTar opens one fixture archive for an in-package test that drives a
// single pass directly.
func openRootfsTar(t *testing.T, path string) (*gzip.Reader, *tar.Reader) {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { f.Close() })
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { gz.Close() })
	return gz, tar.NewReader(gz)
}

func TestVerifyConsumedArtifactRejectsDrift(t *testing.T) {
	// The extraction pass hashes the compressed bytes it actually consumed, so a
	// source rewritten in place after the verified read cannot be published.
	payload := []byte("compressed-bytes")
	a := manifest.Artifact{Compression: "tar.gz", Size: int64(len(payload)), SHA256: sha256Hex(payload)}
	sum := sha256.Sum256(payload)
	if err := verifyConsumedArtifact(int64(len(payload)), sum[:], a); err != nil {
		t.Fatalf("matching consumed bytes rejected: %v", err)
	}
	if err := verifyConsumedArtifact(int64(len(payload))-1, sum[:], a); err == nil {
		t.Fatal("truncated consumption accepted")
	}
	other := sha256.Sum256([]byte("other"))
	if err := verifyConsumedArtifact(int64(len(payload)), other[:], a); err == nil {
		t.Fatal("hash drift accepted")
	}
}

func TestStageRootfsAcceptsMixedRootMarkers(t *testing.T) {
	a, p := buildRootfsArchive(t, []rootfsEntry{
		{Name: ".", Type: tar.TypeDir, Mode: 0755},
		{Name: "etc", Type: tar.TypeDir, Mode: 0755},
		{Name: "etc/hosts", Mode: 0644, Body: "127.0.0.1 localhost\n"},
		{Name: "./usr/bin/env", Mode: 0755, Body: "env\n"},
	})
	dest := filepath.Join(t.TempDir(), "rootfs")
	stats, err := StageRootfs(context.Background(), a, p, dest)
	if err != nil {
		t.Fatalf("stage rootfs: %v", err)
	}
	if stats.Files != 2 {
		t.Fatalf("files = %d, want 2 (%+v)", stats.Files, stats)
	}
	if got := readStaged(t, filepath.Join(dest, "usr", "bin", "env")); got != "env\n" {
		t.Fatalf("env = %q", got)
	}
}
