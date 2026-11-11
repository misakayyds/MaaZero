package store

import (
	"os"
	"path/filepath"
	"testing"
)

// TestCloseReleasesEveryStoreHandle guards the invariant that a closed store
// leaves nothing behind: the writer lock and the state file must be removable
// immediately, otherwise a restart on the same data root can wedge.
func TestCloseReleasesEveryStoreHandle(t *testing.T) {
	root := t.TempDir()
	s, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	commitVersion(t, s, "", "1.0.0")
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"transactions/writer.lock", "transactions/state.json"} {
		if err := os.Remove(filepath.Join(root, filepath.FromSlash(name))); err != nil {
			t.Fatalf("handle to %s survived Close: %v", name, err)
		}
	}
	if err := os.RemoveAll(root); err != nil {
		t.Fatalf("data root survived Close: %v", err)
	}
}

