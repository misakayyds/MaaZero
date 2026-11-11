package install

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"maa-phone-manager/manager/internal/manifest"
)

// The manager daemon is distributed as an uncompressed executable image, so the
// binary stage must prove the signed hash and byte size on the same handle it
// copies, publish exactly releaseDir/maa-managerd, and leave nothing behind when
// verification fails.

func buildManagerArtifact(t *testing.T, payload []byte) (manifest.Artifact, string) {
	t.Helper()
	p := filepath.Join(t.TempDir(), "maa-managerd")
	if err := os.WriteFile(p, payload, 0600); err != nil {
		t.Fatal(err)
	}
	a := manifest.Artifact{Compression: "none", Size: int64(len(payload)), SHA256: sha256Hex(payload)}
	return a, p
}

func TestStageBinaryStagesExecutable(t *testing.T) {
	payload := append([]byte("\x7fELF"), bytes.Repeat([]byte("manager"), 512)...)
	a, p := buildManagerArtifact(t, payload)
	parent := t.TempDir()
	sentinel := filepath.Join(parent, "sentinel")
	if err := os.WriteFile(sentinel, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	dest := filepath.Join(parent, "1.0.0")
	if err := StageBinary(context.Background(), a, p, dest); err != nil {
		t.Fatalf("stage binary: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dest, "maa-managerd"))
	if err != nil {
		t.Fatalf("read staged binary: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("staged binary differs from the verified artifact (%d vs %d bytes)", len(got), len(payload))
	}
	// The published bytes must hash to the signed artifact, which is what the
	// staged-bytes check enforces before publication.
	if sha256Hex(got) != a.SHA256 {
		t.Fatal("published binary does not match the signed artifact hash")
	}
	info, err := os.Lstat(filepath.Join(dest, "maa-managerd"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		t.Fatalf("staged binary is not a regular file: %v", info.Mode())
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0111 == 0 {
		t.Fatalf("staged binary is not executable: %04o", info.Mode().Perm())
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
	assertNoStagingLeftovers(t, parent)
	// The published release is immutable: staging the same version twice must not
	// replace the verified bytes already installed.
	other, otherPath := buildManagerArtifact(t, []byte("different"))
	if err := StageBinary(context.Background(), other, otherPath, dest); err == nil {
		t.Fatal("existing release was overwritten")
	}
	if got := readStaged(t, filepath.Join(dest, "maa-managerd")); got != string(payload) {
		t.Fatalf("existing release content changed")
	}
}

func TestStageBinaryRejectsInvalidInput(t *testing.T) {
	payload := []byte("manager-binary")
	base, p := buildManagerArtifact(t, payload)
	orig := binaryLimits
	defer func() { binaryLimits = orig }()
	for _, kind := range []string{"compressed", "hash", "size", "zero-size", "oversized", "missing-file", "directory-argument"} {
		t.Run(kind, func(t *testing.T) {
			binaryLimits = orig
			a := base
			path := p
			switch kind {
			case "compressed":
				a.Compression = "tar.gz"
			case "hash":
				a.SHA256 = sha256Hex([]byte("other"))
			case "size":
				a.Size--
			case "zero-size":
				a.Size = 0
			case "oversized":
				binaryLimits.MaxBytes = 4
			case "missing-file":
				path = filepath.Join(t.TempDir(), "absent")
			case "directory-argument":
				path = t.TempDir()
			}
			parent := t.TempDir()
			dest := filepath.Join(parent, "candidate")
			stageErr := StageBinary(context.Background(), a, path, dest)
			if stageErr == nil {
				t.Fatalf("%s artifact was staged", kind)
			}
			if errors.Is(stageErr, ErrPublished) {
				t.Fatalf("%s failure reported a published release: %v", kind, stageErr)
			}
			assertNoCandidate(t, dest)
			assertNoStagingLeftovers(t, parent)
		})
	}
}

func TestStageBinaryCancellation(t *testing.T) {
	t.Run("before start", func(t *testing.T) {
		a, p := buildManagerArtifact(t, []byte("manager-binary"))
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		parent := t.TempDir()
		dest := filepath.Join(parent, "candidate")
		stageErr := StageBinary(ctx, a, p, dest)
		if !errors.Is(stageErr, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v", stageErr)
		}
		if errors.Is(stageErr, ErrPublished) {
			t.Fatalf("cancellation reported a published release: %v", stageErr)
		}
		assertNoCandidate(t, dest)
		assertNoStagingLeftovers(t, parent)
	})
	t.Run("mid stream", func(t *testing.T) {
		a, p := buildManagerArtifact(t, bytes.Repeat([]byte("manager-binary"), 4096))
		parent := t.TempDir()
		dest := filepath.Join(parent, "candidate")
		ctx := &countingContext{Context: context.Background(), limit: 2}
		stageErr := StageBinary(ctx, a, p, dest)
		if !errors.Is(stageErr, context.Canceled) {
			t.Fatalf("want context.Canceled, got %v (Err calls=%d)", stageErr, ctx.calls)
		}
		if errors.Is(stageErr, ErrPublished) {
			t.Fatalf("cancellation reported a published release: %v", stageErr)
		}
		assertNoCandidate(t, dest)
		assertNoStagingLeftovers(t, parent)
	})
}

func TestVerifyStagedFileDetectsMismatch(t *testing.T) {
	// The published binary must be the verified bytes, so the staged copy is
	// hashed again before promotion.
	dir := t.TempDir()
	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	payload := []byte("manager-bytes")
	if err := root.WriteFile("maa-managerd", payload, 0600); err != nil {
		t.Fatal(err)
	}
	if err := verifyStagedFile(root, "maa-managerd", int64(len(payload)), sha256Hex(payload)); err != nil {
		t.Fatalf("verified staged bytes rejected: %v", err)
	}
	if err := verifyStagedFile(root, "maa-managerd", int64(len(payload)), sha256Hex([]byte("other"))); err == nil {
		t.Fatal("staged bytes with the wrong hash were accepted")
	}
	if err := verifyStagedFile(root, "maa-managerd", int64(len(payload))+1, sha256Hex(payload)); err == nil {
		t.Fatal("staged bytes with the wrong size were accepted")
	}
	if err := verifyStagedFile(root, "absent", int64(len(payload)), sha256Hex(payload)); err == nil {
		t.Fatal("a missing staged file was accepted")
	}
}

func TestStageBinaryRejectsOccupiedReleasePath(t *testing.T) {
	a, p := buildManagerArtifact(t, []byte("manager-binary"))
	parent := t.TempDir()
	dest := filepath.Join(parent, "1.0.0")
	if err := os.WriteFile(dest, []byte("occupied"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := StageBinary(context.Background(), a, p, dest); err == nil {
		t.Fatal("staging over an occupied path succeeded")
	}
	if got := readStaged(t, dest); got != "occupied" {
		t.Fatalf("occupied path changed: %q", got)
	}
}
