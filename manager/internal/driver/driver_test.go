package driver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"maa-phone-manager/manager/internal/control"
	"maa-phone-manager/manager/internal/manifest"
)

func TestDriverImplementsInterface(t *testing.T) {
	var _ control.Driver = (*Driver)(nil)
}

func setupTestProject(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()

	files := map[string]string{
		"maa-run.sh": `#!/bin/bash
LOCK_FILE="/tmp/maa-task.lock"
echo "run"
`,
		"maa-auto-update.sh": `#!/bin/bash
echo "update"
`,
		"maa-webui.py": `PORT = 8089
server = WEB_SERVER_CLASS(("127.0.0.1", PORT), Handler)
`,
		"config.json": `{"version":"1.0"}
`,
	}

	for name, content := range files {
		// Use LF only
		lfContent := strings.ReplaceAll(content, "\r\n", "\n")
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(lfContent), 0755); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestDriverAcquireAndRelease(t *testing.T) {
	rootfs := t.TempDir()
	drv, err := New(Config{
		RootfsPath: rootfs,
		DataRoot:   t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	release, err := drv.Acquire(ctx)
	if err != nil {
		t.Fatalf("first acquire failed: %v", err)
	}

	// Lockfile should exist
	lockPath := filepath.Join(rootfs, "tmp", "maa-task.lock")
	if _, err := os.Stat(lockPath); err != nil {
		t.Fatalf("lock file does not exist: %v", err)
	}

	// While held, second acquire with short timeout should fail
	shortCtx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	_, err2 := drv.Acquire(shortCtx)
	if err2 == nil {
		t.Fatal("expected second acquire to fail while lock is held, but succeeded")
	}

	// Release first lock
	release()

	// Now third acquire should succeed
	thirdCtx, cancel3 := context.WithTimeout(context.Background(), time.Second)
	defer cancel3()
	release3, err3 := drv.Acquire(thirdCtx)
	if err3 != nil {
		t.Fatalf("acquire after release failed: %v", err3)
	}
	release3()
}

func TestDriverValidateProject(t *testing.T) {
	projectDir := setupTestProject(t)
	drv, err := New(Config{
		RootfsPath: t.TempDir(),
		DataRoot:   t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	m := &manifest.ChannelManifest{Channel: "stable"}

	// Valid project bundle
	if err := drv.Validate(ctx, m, projectDir); err != nil {
		t.Fatalf("valid project rejected: %v", err)
	}

	// Offline rollback (m == nil)
	if err := drv.Validate(ctx, nil, projectDir); err != nil {
		t.Fatalf("offline rollback validation failed: %v", err)
	}
}

func TestDriverValidateRejectsDirtyProject(t *testing.T) {
	drv, err := New(Config{
		RootfsPath: t.TempDir(),
		DataRoot:   t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()

	// 1. Missing directory
	if err := drv.Validate(ctx, nil, filepath.Join(t.TempDir(), "nonexistent")); err == nil {
		t.Fatal("expected error for nonexistent bundle directory")
	}

	// 2. Project containing forbidden helper script
	dirtyDir := setupTestProject(t)
	forbiddenFile := filepath.Join(dirtyDir, "wifi_watchdog.sh")
	if err := os.WriteFile(forbiddenFile, []byte("#!/bin/bash\n"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := drv.Validate(ctx, nil, dirtyDir); err == nil {
		t.Fatal("expected validation error for project containing wifi_watchdog.sh")
	}

	// 3. Project with CR line endings
	crDir := setupTestProject(t)
	crFile := filepath.Join(crDir, "maa-run.sh")
	if err := os.WriteFile(crFile, []byte("#!/bin/bash\r\nLOCK_FILE=\"/tmp/maa-task.lock\"\r\n"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := drv.Validate(ctx, nil, crDir); err == nil {
		t.Fatal("expected validation error for CR line endings")
	}
}

func TestDriverActivateAndMountLifecycle(t *testing.T) {
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
		t.Fatalf("activate failed: %v", err)
	}

	if drv.ActiveRelease() != projectDir {
		t.Fatalf("active release = %q, want %q", drv.ActiveRelease(), projectDir)
	}

	// Check that mounts were performed
	history := mounter.History()
	if len(history) == 0 {
		t.Fatal("expected mounts to be recorded")
	}

	// Check that DNS resolv.conf was written
	resolvPath := filepath.Join(rootfs, "etc", "resolv.conf")
	resolvContent, err := os.ReadFile(resolvPath)
	if err != nil {
		t.Fatalf("read resolv.conf: %v", err)
	}
	if !strings.Contains(string(resolvContent), "1.1.1.1") {
		t.Fatalf("resolv.conf content missing nameservers: %s", string(resolvContent))
	}

	// Check supervisor is running
	if !sup.Running() {
		t.Fatal("expected supervisor to be running")
	}

	// Stop driver
	if err := drv.Stop(ctx); err != nil {
		t.Fatalf("stop failed: %v", err)
	}

	// Check supervisor stopped
	if sup.Running() {
		t.Fatal("expected supervisor to be stopped")
	}

	// Check active release cleared
	if drv.ActiveRelease() != "" {
		t.Fatalf("expected active release to be cleared, got %q", drv.ActiveRelease())
	}
}

func TestDriverActivateRollbackOnMountFailure(t *testing.T) {
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	projectDir := setupTestProject(t)

	mounter := NewSimulatedMounter()
	// Simulate failure on user vol config
	failTarget := filepath.Join(rootfs, "root", ".config", "maa")
	mounter.SetFailOnMount(failTarget)

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
		t.Fatal("expected activate to fail on mount failure")
	}

	// Verify rollback: no mounts should remain active
	remaining, err := mounter.ListMountsUnder(rootfs)
	if err != nil {
		t.Fatal(err)
	}
	if len(remaining) > 0 {
		t.Fatalf("expected 0 remaining mounts after rollback, found: %v", remaining)
	}

	// Verify supervisor was never left running
	if sup.Running() {
		t.Fatal("supervisor should not be running after mount failure")
	}
}

func TestDriverActivateRollbackOnHealthCheckTimeout(t *testing.T) {
	rootfs := t.TempDir()
	dataRoot := t.TempDir()
	projectDir := setupTestProject(t)

	mounter := NewSimulatedMounter()
	sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})
	// Configure supervisor to fail readiness probe
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

	ctx := context.Background()
	err = drv.Activate(ctx, projectDir)
	if err == nil {
		t.Fatal("expected activate to fail on readiness failure")
	}

	// Verify mounts rolled back
	remaining, err := mounter.ListMountsUnder(rootfs)
	if err != nil {
		t.Fatal(err)
	}
	if len(remaining) > 0 {
		t.Fatalf("expected 0 remaining mounts after readiness rollback, found: %v", remaining)
	}

	// Verify supervisor stopped
	if sup.Running() {
		t.Fatal("supervisor should be stopped after readiness rollback")
	}
}

func TestDriverStopUnmountsLIFO(t *testing.T) {
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

	mountStack := drv.MountStack()
	if len(mountStack) == 0 {
		t.Fatal("expected non-empty mount stack")
	}

	if err := drv.Stop(ctx); err != nil {
		t.Fatal(err)
	}

	// Verify unmount operations occurred in reverse order
	history := mounter.History()
	var unmountTargets []string
	for _, op := range history {
		if op.Op == "unmount" {
			unmountTargets = append(unmountTargets, op.Target)
		}
	}

	if len(unmountTargets) < len(mountStack) {
		t.Fatalf("expected at least %d unmounts, got %d", len(mountStack), len(unmountTargets))
	}

	// The first unmounted target should be the last mounted target (LIFO)
	lastMounted := mountStack[len(mountStack)-1]
	firstUnmounted := unmountTargets[0]
	if lastMounted != firstUnmounted {
		t.Fatalf("LIFO violation: last mounted was %s, but first unmounted was %s", lastMounted, firstUnmounted)
	}
}

func TestMountPlanOrder(t *testing.T) {
	rootfs := "/test/rootfs"
	dataRoot := "/test/data"
	bundleDir := "/test/bundle"

	plan := buildMountPlan(rootfs, dataRoot, bundleDir)
	if len(plan) != 16 {
		t.Fatalf("expected 16 mount entries, got %d", len(plan))
	}

	// Verify key order requirements:
	// 1. proc first
	if plan[0].Name != "proc" {
		t.Fatalf("first entry must be proc, got %s", plan[0].Name)
	}
	// 2. sys
	if plan[1].Name != "sys" {
		t.Fatalf("second entry must be sys, got %s", plan[1].Name)
	}
	// 3. dev before devpts and devshm
	if plan[2].Name != "dev" {
		t.Fatalf("third entry must be dev, got %s", plan[2].Name)
	}
	if plan[3].Name != "devpts" || plan[4].Name != "devshm" {
		t.Fatalf("entries 4 and 5 must be devpts and devshm, got %s, %s", plan[3].Name, plan[4].Name)
	}
	// 6. tmp
	if plan[5].Name != "tmp" {
		t.Fatalf("entry 6 must be tmp, got %s", plan[5].Name)
	}
	// 16. bundleDir is last
	if plan[len(plan)-1].Name != "bundle-project" {
		t.Fatalf("last entry must be bundle-project, got %s", plan[len(plan)-1].Name)
	}
}

func TestSeedDefaultConfigsPreservesExistingUserFiles(t *testing.T) {
	dataRoot := t.TempDir()
	customCLI := filepath.Join(dataRoot, "data", "config", "cli.toml")
	if err := os.MkdirAll(filepath.Dir(customCLI), 0700); err != nil {
		t.Fatal(err)
	}
	const userCLI = "# custom user cli\n[core]\nchannel = \"Beta\"\n"
	if err := os.WriteFile(customCLI, []byte(userCLI), 0600); err != nil {
		t.Fatal(err)
	}

	if err := seedDefaultConfigs(dataRoot); err != nil {
		t.Fatalf("seedDefaultConfigs failed: %v", err)
	}

	gotCLI, err := os.ReadFile(customCLI)
	if err != nil {
		t.Fatal(err)
	}
	if string(gotCLI) != userCLI {
		t.Fatalf("existing cli.toml was overwritten: %q", string(gotCLI))
	}

	for _, rel := range []string{
		filepath.Join("profiles", "default.toml"),
		filepath.Join("tasks", "daily.toml"),
	} {
		if _, err := os.Stat(filepath.Join(dataRoot, "data", "config", rel)); err != nil {
			t.Fatalf("missing seeded file %s: %v", rel, err)
		}
	}
}

func TestSupervisorReadinessProbe(t *testing.T) {
	// Start a test server
	var ready bool
	var mu sync.Mutex
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if !ready {
			http.Error(w, "starting", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer ts.Close()

	ctx := context.Background()

	// Should succeed once ready is true
	go func() {
		time.Sleep(150 * time.Millisecond)
		mu.Lock()
		ready = true
		mu.Unlock()
	}()

	err := waitReadyProbe(ctx, ts.URL, 2*time.Second, func() bool { return true })
	if err != nil {
		t.Fatalf("readiness probe failed: %v", err)
	}

	// Should fail if timeout occurs
	mu.Lock()
	ready = false
	mu.Unlock()

	shortCtx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	errTimeout := waitReadyProbe(shortCtx, ts.URL, 150*time.Millisecond, func() bool { return true })
	if errTimeout == nil {
		t.Fatal("expected probe to fail on timeout, but succeeded")
	}
}
