package driver

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// trackingMounter records all mount and unmount events in a shared thread-safe slice.
type trackingMounter struct {
	mu            sync.Mutex
	events        *[]string
	failUnmountOn string
	*SimulatedMounter
}

func newTrackingMounter(events *[]string) *trackingMounter {
	return &trackingMounter{
		events:           events,
		SimulatedMounter: NewSimulatedMounter(),
	}
}

func (m *trackingMounter) Mount(source, target, fstype string, flags uintptr, data string) error {
	m.mu.Lock()
	*m.events = append(*m.events, "mount:"+filepath.Base(target))
	m.mu.Unlock()
	return m.SimulatedMounter.Mount(source, target, fstype, flags, data)
}

func (m *trackingMounter) Unmount(target string, flags int) error {
	m.mu.Lock()
	*m.events = append(*m.events, "unmount:"+filepath.Base(target))
	fail := m.failUnmountOn != "" && filepath.Clean(target) == m.failUnmountOn
	m.mu.Unlock()
	if fail {
		return errors.New("simulated unmount failure on " + target)
	}
	return m.SimulatedMounter.Unmount(target, flags)
}

// trackingSupervisor records start and stop events in a shared thread-safe slice.
type trackingSupervisor struct {
	mu           sync.Mutex
	events       *[]string
	failStop     bool
	slowStopWait time.Duration
	*SimulatedSupervisor
}

func newTrackingSupervisor(events *[]string, cfg Config) *trackingSupervisor {
	return &trackingSupervisor{
		events:              events,
		SimulatedSupervisor: NewSimulatedSupervisor(cfg),
	}
}

func (s *trackingSupervisor) Start(ctx context.Context, cfg Config, bundleDir string) error {
	s.mu.Lock()
	*s.events = append(*s.events, "supervisor:start")
	s.mu.Unlock()
	return s.SimulatedSupervisor.Start(ctx, cfg, bundleDir)
}

func (s *trackingSupervisor) Stop(ctx context.Context) error {
	s.mu.Lock()
	*s.events = append(*s.events, "supervisor:stop")
	fail := s.failStop
	delay := s.slowStopWait
	s.mu.Unlock()

	if delay > 0 {
		time.Sleep(delay)
	}
	_ = s.SimulatedSupervisor.Stop(ctx)
	if fail {
		return errors.New("simulated supervisor stop failure")
	}
	return nil
}

// TestAdversarialTeardownOrdering proves that supervisor.Stop strictly precedes
// any filesystem unmount operations during driver.Stop().
func TestAdversarialTeardownOrdering(t *testing.T) {
	var events []string
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	bundleDir := setupTestProject(t)

	mounter := newTrackingMounter(&events)
	sup := newTrackingSupervisor(&events, Config{UpstreamURL: "http://127.0.0.1:8089"})

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
	if err := drv.Activate(ctx, bundleDir); err != nil {
		t.Fatalf("activate failed: %v", err)
	}

	events = nil // reset event trace for Stop()
	if err := drv.Stop(ctx); err != nil {
		t.Fatalf("stop failed: %v", err)
	}

	if len(events) == 0 {
		t.Fatal("no events recorded during stop")
	}

	// First event MUST be supervisor:stop
	if events[0] != "supervisor:stop" {
		t.Fatalf("TEARDOWN VIOLATION: First stop action was %q, expected 'supervisor:stop'", events[0])
	}

	// Verify all subsequent events are unmount operations
	for i := 1; i < len(events); i++ {
		if len(events[i]) < 7 || events[i][:7] != "unmount" {
			t.Fatalf("TEARDOWN VIOLATION: Unexpected event after supervisor stop at index %d: %s", i, events[i])
		}
	}
}

// TestAdversarialUnmountResilience proves that even if supervisor.Stop fails,
// driver.Stop still attempts and succeeds at unmounting all mounted targets.
func TestAdversarialUnmountResilience(t *testing.T) {
	var events []string
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	bundleDir := setupTestProject(t)

	mounter := newTrackingMounter(&events)
	sup := newTrackingSupervisor(&events, Config{UpstreamURL: "http://127.0.0.1:8089"})
	sup.failStop = true // supervisor fails to stop

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
	if err := drv.Activate(ctx, bundleDir); err != nil {
		t.Fatalf("activate failed: %v", err)
	}

	mountsBefore := drv.MountStack()
	if len(mountsBefore) == 0 {
		t.Fatal("expected active mounts before stop")
	}

	// Stop must return an error because supervisor.Stop failed, BUT it must still unmount filesystems!
	err = drv.Stop(ctx)
	if err == nil {
		t.Fatal("expected error due to supervisor stop failure")
	}

	// Verify all mounts were unmounted despite supervisor stop error
	remaining, err := mounter.ListMountsUnder(rootfs)
	if err != nil {
		t.Fatal(err)
	}
	if len(remaining) > 0 {
		t.Fatalf("RESILIENCE FAILURE: %d mounts remained trapped under rootfs after failed supervisor stop: %v", len(remaining), remaining)
	}
	if len(drv.MountStack()) != 0 {
		t.Fatalf("expected empty mount stack, got %v", drv.MountStack())
	}
}

// TestAdversarialPartialUnmountFailure proves that if one unmount fails,
// driver.Stop does not abort early and continues unmounting the remaining targets.
func TestAdversarialPartialUnmountFailure(t *testing.T) {
	var events []string
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	bundleDir := setupTestProject(t)

	mounter := newTrackingMounter(&events)
	sup := newTrackingSupervisor(&events, Config{UpstreamURL: "http://127.0.0.1:8089"})

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
	if err := drv.Activate(ctx, bundleDir); err != nil {
		t.Fatalf("activate failed: %v", err)
	}

	mounts := drv.MountStack()
	// Target the middle mount to fail unmounting
	failTarget := mounts[len(mounts)/2]
	mounter.failUnmountOn = failTarget

	err = drv.Stop(ctx)
	if err == nil {
		t.Fatal("expected error from failed unmount")
	}

	// Count unmount events: all mounts in the stack should have been attempted
	unmountCount := 0
	for _, ev := range events {
		if len(ev) >= 7 && ev[:7] == "unmount" {
			unmountCount++
		}
	}
	if unmountCount < len(mounts) {
		t.Fatalf("UNMOUNT ABORTION: attempted %d unmounts, expected at least %d", unmountCount, len(mounts))
	}
}

// TestAdversarialConcurrentStop proves that calling Stop concurrently
// from multiple goroutines is safe, thread-safe, and idempotent.
func TestAdversarialConcurrentStop(t *testing.T) {
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	bundleDir := setupTestProject(t)

	drv, err := New(Config{
		RootfsPath: rootfs,
		DataRoot:   dataRoot,
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	if err := drv.Activate(ctx, bundleDir); err != nil {
		t.Fatalf("activate failed: %v", err)
	}

	var wg sync.WaitGroup
	errCh := make(chan error, 10)
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := drv.Stop(ctx); err != nil {
				errCh <- err
			}
		}()
	}
	wg.Wait()
	close(errCh)

	for err := range errCh {
		t.Fatalf("concurrent stop failed: %v", err)
	}

	if drv.ActiveRelease() != "" {
		t.Fatalf("expected empty active release, got %q", drv.ActiveRelease())
	}
	if len(drv.MountStack()) != 0 {
		t.Fatalf("expected empty mount stack, got %v", drv.MountStack())
	}
}

// TestAdversarialReactivationWithNewBundle proves switching candidate releases
// remounts the release bundle to the new candidate without unmounting base filesystems.
func TestAdversarialReactivationWithNewBundle(t *testing.T) {
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	bundle1 := setupTestProject(t)
	bundle2 := setupTestProject(t)

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
	if err := drv.Activate(ctx, bundle1); err != nil {
		t.Fatalf("first activate failed: %v", err)
	}
	if drv.ActiveRelease() != bundle1 {
		t.Fatalf("expected bundle1 active, got %s", drv.ActiveRelease())
	}

	// Switch to bundle2
	if err := drv.Activate(ctx, bundle2); err != nil {
		t.Fatalf("second activate failed: %v", err)
	}
	if drv.ActiveRelease() != bundle2 {
		t.Fatalf("expected bundle2 active, got %s", drv.ActiveRelease())
	}

	// Verify bundleTarget mount in simulated mounter points to bundle2
	bundleTarget := filepath.Join(rootfs, "root", "maa-scripts")
	mounted, err := mounter.IsMounted(bundleTarget)
	if err != nil || !mounted {
		t.Fatalf("bundleTarget %s not mounted after switch", bundleTarget)
	}
	record := mounter.mounts[bundleTarget]
	if record.Source != bundle2 {
		t.Fatalf("bundleTarget source = %s, expected %s", record.Source, bundle2)
	}

	// Clean teardown
	if err := drv.Stop(ctx); err != nil {
		t.Fatalf("stop failed: %v", err)
	}
}
