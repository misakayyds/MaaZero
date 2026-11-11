package store

import (
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// healthCheckTransaction moves a staged candidate to the point where the runtime
// health callback runs.
func healthCheckTransaction(t *testing.T, s *Store, from, to string) *Transaction {
	t.Helper()
	tx := staged(t, s, from, to)
	if err := tx.Advance(Switching); err != nil {
		t.Fatal(err)
	}
	if err := tx.Advance(HealthCheck); err != nil {
		t.Fatal(err)
	}
	return tx
}

// blockedCommit runs a Commit whose health callback blocks until the test
// releases it, which is the window a runtime activation occupies.
type blockedCommit struct {
	entered chan struct{}
	release chan struct{}
	done    chan error
}

func startBlockedCommit(tx *Transaction) *blockedCommit {
	b := &blockedCommit{entered: make(chan struct{}), release: make(chan struct{}), done: make(chan error, 1)}
	go func() {
		b.done <- tx.Commit(func() error {
			close(b.entered)
			<-b.release
			return nil
		})
	}()
	<-b.entered
	return b
}

func TestCommitDoesNotHoldTheStoreLockDuringHealthCheck(t *testing.T) {
	s := openStore(t, t.TempDir())
	commitVersion(t, s, "", "1.0.0")
	tx := healthCheckTransaction(t, s, "1.0.0", "2.0.0")
	b := startBlockedCommit(tx)
	read := make(chan string, 1)
	go func() {
		versions := s.Versions()["project"]
		stage := "none"
		if pending := s.Pending(); pending != nil {
			stage = string(pending.Stage)
		}
		read <- versions.Current + "/" + versions.Previous + "/" + stage
	}()
	select {
	case got := <-read:
		if got != "2.0.0/1.0.0/health_check" {
			t.Fatalf("readers saw %q during the health check", got)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("store readers were blocked by the runtime health check")
	}
	close(b.release)
	if err := <-b.done; err != nil {
		t.Fatal(err)
	}
	if s.Pending() != nil || s.Versions()["project"].Current != "2.0.0" {
		t.Fatal("commit did not persist after the health check")
	}
}

func TestConcurrentCommitRunsOneHealthCheckAndOneCommit(t *testing.T) {
	s := openStore(t, t.TempDir())
	commitVersion(t, s, "", "1.0.0")
	tx := healthCheckTransaction(t, s, "1.0.0", "2.0.0")
	var calls int32
	errs := make([]error, 2)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := range errs {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			errs[i] = tx.Commit(func() error {
				atomic.AddInt32(&calls, 1)
				time.Sleep(50 * time.Millisecond)
				return nil
			})
		}(i)
	}
	close(start)
	wg.Wait()
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("health check ran %d times, want exactly one activation", got)
	}
	committed, rejected := 0, 0
	for _, err := range errs {
		if err == nil {
			committed++
		} else {
			rejected++
		}
	}
	if committed != 1 || rejected != 1 {
		t.Fatalf("commit results = %v, want exactly one winner", errs)
	}
	if got := s.Versions()["project"].Current; got != "2.0.0" {
		t.Fatalf("current = %q", got)
	}
	if s.Pending() != nil {
		t.Fatal("pending record survived the commit")
	}
}

func TestRollbackDuringHealthCheckWinsAndBlocksNewTransactions(t *testing.T) {
	s := openStore(t, t.TempDir())
	commitVersion(t, s, "", "1.0.0")
	tx := healthCheckTransaction(t, s, "1.0.0", "2.0.0")
	b := startBlockedCommit(tx)
	if _, err := s.BeginUpdate("project", "1.0.0", "3.0.0", digest); err == nil {
		t.Fatal("new transaction started while a runtime health check was running")
	}
	if err := tx.Rollback("cancelled"); err != nil {
		t.Fatal(err)
	}
	close(b.release)
	if err := <-b.done; err == nil {
		t.Fatal("commit accepted a transaction that was rolled back underneath it")
	}
	if s.Pending() != nil {
		t.Fatal("rollback left a pending record")
	}
	if got := s.Versions()["project"].Current; got != "1.0.0" {
		t.Fatalf("current = %q, want the restored version", got)
	}
	if _, err := s.BeginUpdate("project", "1.0.0", "3.0.0", digest); err != nil {
		t.Fatalf("store stayed locked after the health check: %v", err)
	}
}

func TestCloseDuringHealthCheckIsSafe(t *testing.T) {
	root := t.TempDir()
	s := openStore(t, root)
	commitVersion(t, s, "", "1.0.0")
	tx := healthCheckTransaction(t, s, "1.0.0", "2.0.0")
	b := startBlockedCommit(tx)
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	close(b.release)
	if err := <-b.done; err == nil {
		t.Fatal("commit succeeded on a closed store")
	}
	reopened := openStore(t, root)
	if reopened.Pending() != nil || reopened.Versions()["project"].Current != "1.0.0" {
		t.Fatal("interrupted health check was not recovered on reopen")
	}
}

func TestLastComponentTracksTerminalTransactions(t *testing.T) {
	s := openStore(t, t.TempDir())
	if got := s.LastComponent(); got != "" {
		t.Fatalf("fresh store reported component %q", got)
	}
	commitVersion(t, s, "", "1.0.0")
	if got := s.LastComponent(); got != "project" {
		t.Fatalf("committed store reported component %q", got)
	}
	tx := healthCheckTransaction(t, s, "1.0.0", "2.0.0")
	if err := tx.Commit(func() error { return errors.New("unhealthy") }); err == nil {
		t.Fatal("unhealthy candidate accepted")
	}
	if got := s.LastComponent(); got != "project" {
		t.Fatalf("failed transaction reported component %q", got)
	}
	if got := s.Versions()["project"].Current; got != "1.0.0" {
		t.Fatalf("current = %q after a failed switch", got)
	}
}
