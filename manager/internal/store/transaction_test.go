package store

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func openStore(t *testing.T, root string) *Store {
	t.Helper()
	s, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = s.Close()
		// Windows AV filter drivers may briefly hold DELETE_PENDING on renamed state.json.
		for i := 0; i < 10; i++ {
			if os.RemoveAll(root) == nil {
				break
			}
			time.Sleep(5 * time.Millisecond)
		}
	})
	return s
}

func staged(t *testing.T, s *Store, from, to string) *Transaction {
	t.Helper()
	tx, err := s.BeginUpdate("project", from, to, digest)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []Stage{Downloading, Verifying, Staging} {
		if err := tx.Advance(p); err != nil {
			t.Fatal(err)
		}
	}
	path, err := s.ReleasePath("project", to)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(path, 0700); err != nil {
		t.Fatal(err)
	}
	return tx
}

func commitVersion(t *testing.T, s *Store, from, to string) {
	t.Helper()
	tx := staged(t, s, from, to)
	if err := tx.Advance(Switching); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(HealthCheck); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(func() error { return nil }); err != nil {
		t.Fatal(err)
	}
}

func TestInstallUpdateAndRollbackPreserveData(t *testing.T) {
	root := t.TempDir()
	s := openStore(t, root)
	data := filepath.Join(root, "data", "cultivation.json")
	if err := os.WriteFile(data, []byte(`{"revision":9}`), 0600); err != nil {
		t.Fatal(err)
	}
	commitVersion(t, s, "", "1.0.0")
	commitVersion(t, s, "1.0.0", "1.1.0")
	got := s.Versions()["project"]
	if got.Current != "1.1.0" || got.Previous != "1.0.0" {
		t.Fatalf("versions: %+v", got)
	}
	tx := staged(t, s, "1.1.0", "1.2.0")
	if err := tx.Advance(Switching); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(HealthCheck); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(func() error { return errors.New("unhealthy") }); err == nil {
		t.Fatal("accepted unhealthy candidate")
	}
	if got := s.Versions()["project"]; got.Current != "1.1.0" || got.Previous != "1.0.0" {
		t.Fatalf("rollback: %+v", got)
	}
	b, err := os.ReadFile(data)
	if err != nil || string(b) != `{"revision":9}` {
		t.Fatalf("data changed: %q %v", b, err)
	}
}

func TestRestartRecoversSwitchButResumesDownload(t *testing.T) {
	for _, phase := range []Stage{Downloading, Switching, HealthCheck} {
		t.Run(string(phase), func(t *testing.T) {
			root := t.TempDir()
			s := openStore(t, root)
			commitVersion(t, s, "", "1.0.0")
			var tx *Transaction
			if phase == Downloading {
				var err error
				tx, err = s.BeginUpdate("project", "1.0.0", "2.0.0", digest)
				if err != nil {
					t.Fatal(err)
				}
				if err := tx.Advance(Downloading); err != nil {
					t.Fatal(err)
				}
			} else {
				tx = staged(t, s, "1.0.0", "2.0.0")
				if err := tx.Advance(Switching); err != nil {
					t.Fatal(err)
				}
				if phase == HealthCheck {
					if err := tx.Advance(HealthCheck); err != nil {
						t.Fatal(err)
					}
				}
			}
			if err := s.Close(); err != nil {
				t.Fatal(err)
			}
			s = openStore(t, root)
			if s.Versions()["project"].Current != "1.0.0" {
				t.Fatal("restart ran unchecked candidate")
			}
			pending := s.Pending()
			if phase == Downloading {
				if pending == nil || pending.Stage != Downloading {
					t.Fatal("download not resumable")
				}
				resumed, err := s.Resume()
				if err != nil {
					t.Fatal(err)
				}
				if err := resumed.Rollback("cancelled"); err != nil {
					t.Fatal(err)
				}
			} else if pending != nil {
				t.Fatal("interrupted switch not recovered")
			}
		})
	}
}

func TestSequencePersistsAndRejectsReplay(t *testing.T) {
	root := t.TempDir()
	s := openStore(t, root)
	if err := s.AcceptSequence("stable", 7, digest); err != nil {
		t.Fatal(err)
	}
	_ = s.Close()
	s = openStore(t, root)
	if err := s.AcceptSequence("stable", 6, digest); err == nil {
		t.Fatal("replay accepted")
	}
	if err := s.AcceptSequence("stable", 7, strings.Repeat("b", 64)); err == nil {
		t.Fatal("equivocation accepted")
	}
	if err := s.AcceptSequence("stable", 7, digest); err != nil {
		t.Fatal(err)
	}
	if err := s.AcceptSequence("beta", 1, digest); err != nil {
		t.Fatal(err)
	}
	if err := s.AcceptSequence("stable", 8, digest); err != nil {
		t.Fatal(err)
	}
}

func TestRejectUnsafePathsAndTransitions(t *testing.T) {
	s := openStore(t, t.TempDir())
	for _, bad := range []string{"", "..", "../data", "/root", `C:\data`, "a/b", "a\\b", "a:stream"} {
		if _, err := s.ReleasePath("project", bad); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
	tx, err := s.BeginUpdate("project", "", "1.0.0", digest)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.BeginUpdate("maacore", "", "2.0.0", digest); err == nil {
		t.Fatal("parallel transaction accepted")
	}
	if err := tx.Advance(Switching); err == nil {
		t.Fatal("skipped verification")
	}
	if err := tx.Commit(func() error { return nil }); err == nil {
		t.Fatal("early commit")
	}
	if err := tx.Rollback("cancelled"); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(Downloading); err == nil {
		t.Fatal("stale transaction accepted")
	}
	if _, err := s.BeginUpdate("project", "9.0.0", "2.0.0", digest); err == nil {
		t.Fatal("incorrect source accepted")
	}
}

func TestStoreExclusiveAndCorruptionFailsClosed(t *testing.T) {
	root := t.TempDir()
	s := openStore(t, root)
	if other, err := Open(root); err == nil {
		_ = other.Close()
		t.Fatal("second writer accepted")
	}
	_ = s.Close()
	if err := os.WriteFile(filepath.Join(root, "transactions", "state.json"), []byte("broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if other, err := Open(root); err == nil {
		_ = other.Close()
		t.Fatal("corruption reset to fresh install")
	}
}

func TestMissingCandidateAndNilHealthCheckRejected(t *testing.T) {
	s := openStore(t, t.TempDir())
	tx, err := s.BeginUpdate("project", "", "1.0.0", digest)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []Stage{Downloading, Verifying, Staging} {
		if err := tx.Advance(p); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Advance(Switching); err == nil {
		t.Fatal("missing candidate accepted")
	}
	path, _ := s.ReleasePath("project", "1.0.0")
	if err := os.MkdirAll(path, 0700); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(WaitingIdle); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(Switching); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(HealthCheck); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(nil); err == nil {
		t.Fatal("missing health check accepted")
	}
}

func TestProcessExitReleasesLockAndRecovers(t *testing.T) {
	if root := os.Getenv("MAA_STORE_CRASH_FIXTURE"); root != "" {
		s, err := Open(root)
		if err != nil {
			os.Exit(2)
		}
		commitVersion(t, s, "", "1.0.0")
		tx := staged(t, s, "1.0.0", "2.0.0")
		if err := tx.Advance(Switching); err != nil {
			os.Exit(3)
		}
		os.Exit(0) // Intentionally bypass Close and all test cleanup.
	}
	root := t.TempDir()
	cmd := exec.Command(os.Args[0], "-test.run=^TestProcessExitReleasesLockAndRecovers$")
	cmd.Env = append(os.Environ(), "MAA_STORE_CRASH_FIXTURE="+root)
	if output, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("crash fixture: %s %v", output, err)
	}
	s := openStore(t, root)
	if s.Pending() != nil || s.Versions()["project"].Current != "1.0.0" {
		t.Fatal("process exit recovery failed")
	}
}

func TestFirstInstallFailureAndClosedStore(t *testing.T) {
	s := openStore(t, t.TempDir())
	tx := staged(t, s, "", "1.0.0")
	if err := tx.Advance(Switching); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback("cancelled"); err != nil {
		t.Fatal(err)
	}
	if s.Versions()["project"].Current != "" {
		t.Fatal("failed initial install became current")
	}
	if _, err := s.Resume(); err == nil {
		t.Fatal("resumed terminal transaction")
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := s.BeginUpdate("project", "", "1.0.0", digest); err == nil {
		t.Fatal("closed store wrote data")
	}
	if err := s.AcceptSequence("stable", 1, digest); err == nil {
		t.Fatal("closed store accepted sequence")
	}
}
