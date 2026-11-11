package install

import (
	"archive/tar"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStageSealedPublishesTheDescriptorWithTheRelease(t *testing.T) {
	a, p := archive(t, []*tar.Header{{Name: "bin/run.sh", Mode: 0755, Size: 3, Typeflag: tar.TypeReg}})
	dest := filepath.Join(t.TempDir(), "1.0.0")
	descriptor := []byte("{\"schema\":1}\n")
	if err := StageSealed(context.Background(), a, p, dest, map[string][]byte{".maa-artifact.json": descriptor}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dest, "bin", "run.sh")); err != nil {
		t.Fatalf("archive content missing: %v", err)
	}
	b, err := os.ReadFile(filepath.Join(dest, ".maa-artifact.json"))
	if err != nil || string(b) != string(descriptor) {
		t.Fatalf("sealed descriptor = %q (%v)", b, err)
	}
}

func TestStageStillPublishesWithoutSealFiles(t *testing.T) {
	a, p := archive(t, []*tar.Header{{Name: "version.txt", Mode: 0600, Size: 3, Typeflag: tar.TypeReg}})
	dest := filepath.Join(t.TempDir(), "1.0.0")
	if err := Stage(context.Background(), a, p, dest); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dest); err != nil {
		t.Fatalf("release missing: %v", err)
	}
}

func TestStageSealedRejectsBadSealsWithoutPublishing(t *testing.T) {
	for _, tc := range []struct {
		name  string
		seal  map[string][]byte
		entry string
	}{
		{"escape", map[string][]byte{"../escape.json": []byte("x")}, ".maa-artifact.json"},
		{"nested", map[string][]byte{"nested/descriptor.json": []byte("x")}, ".maa-artifact.json"},
		{"collision", map[string][]byte{".maa-artifact.json": []byte("x")}, ".maa-artifact.json"},
		{"empty name", map[string][]byte{"": []byte("x")}, ".maa-artifact.json"},
		{"oversized", map[string][]byte{"descriptor.json": make([]byte, maxSealBytes+1)}, "descriptor.json"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a, p := archive(t, []*tar.Header{{Name: tc.entry, Mode: 0644, Size: int64(len(tc.entry)), Typeflag: tar.TypeReg}})
			parent := t.TempDir()
			dest := filepath.Join(parent, "1.0.0")
			if err := StageSealed(context.Background(), a, p, dest, tc.seal); err == nil {
				t.Fatal("unsafe seal accepted")
			}
			if _, err := os.Stat(dest); !os.IsNotExist(err) {
				t.Fatalf("release directory published after a rejected seal: %v", err)
			}
			entries, err := os.ReadDir(parent)
			if err != nil {
				t.Fatal(err)
			}
			for _, entry := range entries {
				if strings.HasPrefix(entry.Name(), ".staging-") {
					t.Fatalf("staging directory left behind: %s", entry.Name())
				}
			}
		})
	}
}
