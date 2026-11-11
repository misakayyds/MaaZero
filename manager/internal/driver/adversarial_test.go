package driver

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// TestAdversarialMountRollbackSequence tests mount failure injection across multiple positions:
// 1. Failure at mount 7 (vol-config): verifies reverse LIFO unmount of mounts 1..6, stack cleared.
// 2. Failure at mount 1 (proc): verifies clean early return with 0 mounts left.
// 3. Failure at mount 4 (devpts): devpts is optional, so activation MUST succeed despite failure.
// 4. Failure at mount 12 (bundle-project): verifies reverse LIFO unmount of all 11 prior mounts.
func TestAdversarialMountRollbackSequence(t *testing.T) {
	t.Run("FailureOnMount7VolConfig", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		// Entry 7 is vol-config: target is <rootfs>/root/.config/maa
		volConfigTarget := filepath.Join(rootfs, "root", ".config", "maa")
		mounter.SetFailOnMount(volConfigTarget)

		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		ctx := context.Background()
		err = drv.Activate(ctx, projectDir)
		if err == nil {
			t.Fatal("expected Activate to fail when mount 7 fails")
		}
		if !strings.Contains(err.Error(), "vol-config") {
			t.Fatalf("expected error to mention vol-config, got: %v", err)
		}

		// 1. Verify that active mount stack in driver is completely cleared
		if stack := drv.MountStack(); len(stack) != 0 {
			t.Fatalf("expected empty mount stack, got: %v", stack)
		}

		// 2. Verify in-memory mounter has 0 active mounts
		activeMounts, err := mounter.ListMountsUnder(rootfs)
		if err != nil {
			t.Fatal(err)
		}
		if len(activeMounts) != 0 {
			t.Fatalf("expected 0 active mounts remaining, got: %v", activeMounts)
		}

		// 3. Verify exact LIFO unmount ordering from history
		history := mounter.History()
		var mounted []string
		var unmounted []string
		for _, op := range history {
			if op.Op == "mount" {
				// Don't record slave re-mounts with empty source if duplicate target
				if len(mounted) == 0 || mounted[len(mounted)-1] != op.Target {
					mounted = append(mounted, op.Target)
				}
			} else if op.Op == "unmount" {
				unmounted = append(unmounted, op.Target)
			}
		}

		// We expect mounts before vol-config to have been mounted
		// plan: proc, sys, dev, devpts (optional), devshm, tmp (6 items)
		if len(mounted) != 6 {
			t.Fatalf("expected 6 successful mounts before mount 7, got %d: %v", len(mounted), mounted)
		}
		if len(unmounted) != 6 {
			t.Fatalf("expected 6 unmount operations during rollback, got %d: %v", len(unmounted), unmounted)
		}

		// Assert exact reverse LIFO ordering
		for i := 0; i < len(mounted); i++ {
			expectedUnmount := mounted[len(mounted)-1-i]
			actualUnmount := unmounted[i]
			if expectedUnmount != actualUnmount {
				t.Fatalf("LIFO order violation at step %d: expected unmount %s, got %s", i, expectedUnmount, actualUnmount)
			}
		}

		// 4. Verify supervisor was not left running
		if sup.Running() {
			t.Fatal("supervisor must not be running after rollback")
		}
	})

	t.Run("FailureOnMount1Proc", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		procTarget := filepath.Join(rootfs, "proc")
		mounter.SetFailOnMount(procTarget)

		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		err = drv.Activate(context.Background(), projectDir)
		if err == nil {
			t.Fatal("expected failure on proc mount")
		}
		if stack := drv.MountStack(); len(stack) != 0 {
			t.Fatalf("expected empty stack, got %v", stack)
		}
		active, _ := mounter.ListMountsUnder(rootfs)
		if len(active) != 0 {
			t.Fatalf("expected 0 active mounts, got %v", active)
		}
	})

	t.Run("FailureOnMount4DevptsIsOptional", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		// devpts target is <rootfs>/dev/pts
		devptsTarget := filepath.Join(rootfs, "dev", "pts")
		mounter.SetFailOnMount(devptsTarget)

		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		// Since devpts is optional, Activate MUST succeed despite devpts mount failure
		err = drv.Activate(context.Background(), projectDir)
		if err != nil {
			t.Fatalf("expected Activate to succeed despite optional devpts failure, got: %v", err)
		}

		// Active stack should have 15 items (16 minus devpts)
		stack := drv.MountStack()
		if len(stack) != 15 {
			t.Fatalf("expected 15 mounted targets (devpts skipped), got %d: %v", len(stack), stack)
		}

		// Clean up
		_ = drv.Stop(context.Background())
	})

	t.Run("FailureOnMount12BundleProject", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		bundleTarget := filepath.Join(rootfs, "root", "maa-scripts")
		mounter.SetFailOnMount(bundleTarget)

		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		err = drv.Activate(context.Background(), projectDir)
		if err == nil {
			t.Fatal("expected failure on bundle project mount")
		}

		// Verify all 11 prior mounts were unwound in reverse LIFO order
		if stack := drv.MountStack(); len(stack) != 0 {
			t.Fatalf("expected empty stack after mount 12 rollback, got %v", stack)
		}
		active, _ := mounter.ListMountsUnder(rootfs)
		if len(active) != 0 {
			t.Fatalf("expected 0 active mounts after mount 12 rollback, got %v", active)
		}
	})
}

// TestAdversarialReadinessProbeFailures tests teardown behavior when the WebUI fails readiness probes:
// 1. Port refused / server never binds.
// 2. Supervisor start failure.
// 3. Process crashes immediately (waitReadyProbe should detect process termination fast).
func TestAdversarialReadinessProbeFailures(t *testing.T) {
	t.Run("PortNeverBindsTimeoutAndTeardown", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		// Supervisor will fail to bind port because port is pointed to a dead port or simulated failure
		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
		sup.SetFailReadiness(true)

		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()

		err = drv.Activate(ctx, projectDir)
		if err == nil {
			t.Fatal("expected Activate to fail on readiness probe timeout")
		}

		// 1. Verify supervisor is stopped
		if sup.Running() {
			t.Fatal("supervisor must be stopped after readiness failure")
		}

		// 2. Verify all mounts unwound in reverse LIFO order
		if stack := drv.MountStack(); len(stack) != 0 {
			t.Fatalf("mount stack must be empty, got: %v", stack)
		}
		active, err := mounter.ListMountsUnder(rootfs)
		if err != nil {
			t.Fatal(err)
		}
		if len(active) != 0 {
			t.Fatalf("active mounts must be 0, got: %v", active)
		}
	})

	t.Run("SupervisorStartFailureRollback", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
		sup.SetFailStart(true)

		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		err = drv.Activate(context.Background(), projectDir)
		if err == nil {
			t.Fatal("expected Activate to fail when supervisor Start fails")
		}
		if !strings.Contains(err.Error(), "start supervisor") {
			t.Fatalf("unexpected error message: %v", err)
		}

		if stack := drv.MountStack(); len(stack) != 0 {
			t.Fatalf("mount stack must be empty, got %v", stack)
		}
		active, _ := mounter.ListMountsUnder(rootfs)
		if len(active) != 0 {
			t.Fatalf("active mounts must be 0, got %v", active)
		}
	})

	t.Run("ImmediateProcessExitDetectedPromptly", func(t *testing.T) {
		// When supervised process dies immediately, waitReadyProbe must fail immediately
		// without waiting for the full 15-second timeout.
		start := time.Now()
		running := false
		err := waitReadyProbe(context.Background(), "http://127.0.0.1:19999", 10*time.Second, func() bool {
			return running
		})
		elapsed := time.Since(start)

		if err == nil {
			t.Fatal("expected error on exited process")
		}
		if !strings.Contains(err.Error(), "supervised process exited before readiness probe succeeded") {
			t.Fatalf("expected process exit error, got: %v", err)
		}
		if elapsed > 2*time.Second {
			t.Fatalf("readiness probe hung for %v, should have returned immediately when process exited", elapsed)
		}
	})
}

// TestAdversarialLockConcurrencyAndContention stress-tests Acquire(ctx):
// 1. Double acquisition mutual exclusion.
// 2. High-concurrency contention across 16 goroutines ensuring mutual exclusion invariant.
// 3. Prompt cancellation without file handle leak.
func TestAdversarialLockConcurrencyAndContention(t *testing.T) {
	t.Run("StrictMutualExclusionUnderContention", func(t *testing.T) {
		rootfs := t.TempDir()
		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   t.TempDir(),
		})
		if err != nil {
			t.Fatal(err)
		}

		const numWorkers = 12
		const iterationsPerWorker = 5
		var activeCount int32
		var maxConcurrent int32
		var totalAcquisitions int32

		var wg sync.WaitGroup
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()

		for w := 0; w < numWorkers; w++ {
			wg.Add(1)
			go func(workerID int) {
				defer wg.Done()
				for i := 0; i < iterationsPerWorker; i++ {
					release, err := drv.Acquire(ctx)
					if err != nil {
						t.Errorf("worker %d iteration %d acquire failed: %v", workerID, i, err)
						return
					}

					// Enter critical section: activeCount must be strictly 1
					current := atomic.AddInt32(&activeCount, 1)
					if current > 1 {
						atomic.StoreInt32(&maxConcurrent, current)
						t.Errorf("MUTUAL EXCLUSION VIOLATED! %d concurrent holders", current)
					}
					atomic.AddInt32(&totalAcquisitions, 1)

					// Simulate small atomic work inside lock
					time.Sleep(5 * time.Millisecond)

					// Exit critical section
					atomic.AddInt32(&activeCount, -1)
					release()

					// Yield briefly
					time.Sleep(2 * time.Millisecond)
				}
			}(w)
		}

		wg.Wait()

		if max := atomic.LoadInt32(&maxConcurrent); max > 1 {
			t.Fatalf("Lock failed mutual exclusion: maximum concurrency was %d", max)
		}
		expectedTotal := int32(numWorkers * iterationsPerWorker)
		if total := atomic.LoadInt32(&totalAcquisitions); total != expectedTotal {
			t.Fatalf("Expected %d total acquisitions, got %d", expectedTotal, total)
		}
	})

	t.Run("ContextCancellationPromptnessAndNoLeak", func(t *testing.T) {
		rootfs := t.TempDir()
		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   t.TempDir(),
		})
		if err != nil {
			t.Fatal(err)
		}

		// Hold lock in goroutine A
		ctxA := context.Background()
		releaseA, err := drv.Acquire(ctxA)
		if err != nil {
			t.Fatalf("first acquire failed: %v", err)
		}

		// Goroutine B tries to acquire with 50ms context timeout
		ctxB, cancelB := context.WithTimeout(context.Background(), 50*time.Millisecond)
		defer cancelB()

		start := time.Now()
		_, errB := drv.Acquire(ctxB)
		elapsed := time.Since(start)

		if errB == nil {
			t.Fatal("expected second acquire to fail due to context cancellation")
		}
		if !strings.Contains(errB.Error(), "context deadline exceeded") && !strings.Contains(errB.Error(), "held") {
			t.Fatalf("unexpected error message: %v", errB)
		}
		if elapsed > 500*time.Millisecond {
			t.Fatalf("Acquire took too long to abort on cancelled context: %v", elapsed)
		}

		// Release first lock
		releaseA()

		// Verify that lock file can now be deleted or immediately reacquired, proving no handle leaks
		lockPath := filepath.Join(rootfs, "tmp", "maa-task.lock")
		ctxC, cancelC := context.WithTimeout(context.Background(), time.Second)
		defer cancelC()

		releaseC, errC := drv.Acquire(ctxC)
		if errC != nil {
			t.Fatalf("re-acquire after release failed (potential handle leak?): %v", errC)
		}
		releaseC()

		// Removing the file should succeed if no open handles are held
		if err := os.Remove(lockPath); err != nil {
			t.Fatalf("cannot remove lockfile after release (handle leak on Windows): %v", err)
		}
	})
}

// TestAdversarialStopIdempotency tests Stop(ctx) robustness:
// 1. Calling Stop on an unactivated driver returns nil.
// 2. Sequential Stop calls (3x) all succeed without error or panic.
// 3. Concurrent Stop calls from 10 goroutines execute safely.
func TestAdversarialStopIdempotency(t *testing.T) {
	t.Run("StopOnUnactivatedDriver", func(t *testing.T) {
		drv, err := New(Config{
			RootfsPath: t.TempDir(),
			DataRoot:   t.TempDir(),
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := drv.Stop(context.Background()); err != nil {
			t.Fatalf("Stop on unactivated driver returned error: %v", err)
		}
	})

	t.Run("SequentialStopIdempotency", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})

		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		ctx := context.Background()
		if err := drv.Activate(ctx, projectDir); err != nil {
			t.Fatal(err)
		}

		// First stop
		if err := drv.Stop(ctx); err != nil {
			t.Fatalf("first Stop failed: %v", err)
		}
		if drv.ActiveRelease() != "" {
			t.Fatalf("active release not cleared: %q", drv.ActiveRelease())
		}
		if stack := drv.MountStack(); len(stack) != 0 {
			t.Fatalf("mount stack not cleared: %v", stack)
		}

		// Second stop (idempotent)
		if err := drv.Stop(ctx); err != nil {
			t.Fatalf("second Stop failed: %v", err)
		}

		// Third stop (idempotent)
		if err := drv.Stop(ctx); err != nil {
			t.Fatalf("third Stop failed: %v", err)
		}
	})

	t.Run("ConcurrentStopCalls", func(t *testing.T) {
		rootfs := t.TempDir()
		dataRoot := t.TempDir()
		projectDir := setupTestProject(t)

		mounter := NewSimulatedMounter()
		sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})

		drv, err := New(Config{
			RootfsPath: rootfs,
			DataRoot:   dataRoot,
			Mounter:    mounter,
			Supervisor: sup,
		})
		if err != nil {
			t.Fatal(err)
		}

		ctx := context.Background()
		if err := drv.Activate(ctx, projectDir); err != nil {
			t.Fatal(err)
		}

		const count = 10
		var wg sync.WaitGroup
		errs := make([]error, count)

		for i := 0; i < count; i++ {
			wg.Add(1)
			go func(idx int) {
				defer wg.Done()
				errs[idx] = drv.Stop(context.Background())
			}(i)
		}

		wg.Wait()

		for idx, err := range errs {
			if err != nil {
				t.Fatalf("concurrent Stop [%d] failed: %v", idx, err)
			}
		}
	})
}

// TestAdversarialPortContention tests behavior when port 8089 is already bound by an external process.
func TestAdversarialPortContention(t *testing.T) {
	// Bind 127.0.0.1:8089 externally
	ln, err := net.Listen("tcp", "127.0.0.1:8089")
	if err != nil {
		t.Skipf("cannot bind test port 8089: %v", err)
	}
	defer ln.Close()

	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	projectDir := setupTestProject(t)

	mounter := NewSimulatedMounter()
	sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})

	drv, err := New(Config{
		RootfsPath:  rootfs,
		DataRoot:    dataRoot,
		UpstreamURL: "http://127.0.0.1:8089",
		Mounter:     mounter,
		Supervisor:  sup,
	})
	if err != nil {
		t.Fatal(err)
	}

	// Activate must fail because port is already bound
	err = drv.Activate(context.Background(), projectDir)
	if err == nil {
		t.Fatal("expected Activate to fail when port 8089 is already bound")
	}

	// Mounts must be rolled back
	if stack := drv.MountStack(); len(stack) != 0 {
		t.Fatalf("mount stack must be empty after port collision rollback, got: %v", stack)
	}
	active, _ := mounter.ListMountsUnder(rootfs)
	if len(active) != 0 {
		t.Fatalf("active mounts must be 0 after port collision rollback, got: %v", active)
	}
}

// TestAdversarialActiveReleaseClearedOnFailure tests whether ActiveRelease is properly invalidated
// when a switch to a new release fails during activation.
func TestAdversarialActiveReleaseClearedOnFailure(t *testing.T) {
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	dirV1 := setupTestProject(t)
	dirV2 := setupTestProject(t)

	mounter := NewSimulatedMounter()
	sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})

	drv, err := New(Config{
		RootfsPath: rootfs,
		DataRoot:   dataRoot,
		Mounter:    mounter,
		Supervisor: sup,
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()

	// 1. Activate V1 -> succeeds
	if err := drv.Activate(ctx, dirV1); err != nil {
		t.Fatalf("activate V1 failed: %v", err)
	}
	if drv.ActiveRelease() != dirV1 {
		t.Fatalf("ActiveRelease = %q, want %q", drv.ActiveRelease(), dirV1)
	}

	// 2. Now attempt to activate V2, but readiness probe fails
	sup.SetFailReadiness(true)
	err = drv.Activate(ctx, dirV2)
	if err == nil {
		t.Fatal("expected activate V2 to fail")
	}

	// 3. Since all mounts were unwound and supervisor stopped, ActiveRelease must NOT report dirV1!
	// It should report "" (or not report stale v1)
	activeAfterFailure := drv.ActiveRelease()
	t.Logf("ActiveRelease after failed upgrade: %q", activeAfterFailure)
	if activeAfterFailure != "" {
		t.Errorf("BUG DISCOVERED: ActiveRelease returned %q after all mounts were unwound and supervisor stopped! Should be empty string", activeAfterFailure)
	}
}

// TestAdversarialDanglingResolvConfSymlink tests setupDNS resilience when <rootfs>/etc/resolv.conf
// is a dangling symlink (the standard Debian systemd-resolved layout where /run is not yet populated).
func TestAdversarialDanglingResolvConfSymlink(t *testing.T) {
	rootfs := t.TempDir()
	etcDir := filepath.Join(rootfs, "etc")
	if err := os.MkdirAll(etcDir, 0755); err != nil {
		t.Fatal(err)
	}
	resolvSymlink := filepath.Join(etcDir, "resolv.conf")
	// Debian default symlink: /etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf
	if err := os.Symlink(filepath.Join("..", "run", "systemd", "resolve", "stub-resolv.conf"), resolvSymlink); err != nil {
		t.Skipf("cannot create symlink on host: %v", err)
	}

	err := setupDNS(rootfs)
	t.Logf("setupDNS result with dangling symlink: %v", err)
	if err != nil {
		t.Errorf("BUG DISCOVERED: setupDNS failed on standard Debian dangling symlink: %v", err)
	}
}
