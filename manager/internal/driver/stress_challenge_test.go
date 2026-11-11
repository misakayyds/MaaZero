package driver

import (
	"context"
	"fmt"
	"math/rand"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// TestStressSimulatedSupervisorRapidStartStop stresses SimulatedSupervisor under rapid,
// concurrent Start and Stop calls across hundreds of iterations to prove zero nil-pointer dereferences or panics.
func TestStressSimulatedSupervisorRapidStartStop(t *testing.T) {
	const iterations = 500
	for i := 0; i < iterations; i++ {
		// Use dynamic free loopback port
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatalf("iter %d: failed to listen on dynamic port: %v", i, err)
		}
		addr := ln.Addr().String()
		_ = ln.Close()

		sup := NewSimulatedSupervisor(Config{
			UpstreamURL: fmt.Sprintf("http://%s", addr),
		})

		var wg sync.WaitGroup
		wg.Add(2)

		// Goroutine 1: Rapid Start
		go func() {
			defer wg.Done()
			// Random tiny jitter
			if rand.Intn(2) == 0 {
				time.Sleep(time.Duration(rand.Intn(500)) * time.Microsecond)
			}
			_ = sup.Start(context.Background(), Config{UpstreamURL: fmt.Sprintf("http://%s", addr)}, "dummy-bundle")
		}()

		// Goroutine 2: Rapid Stop
		go func() {
			defer wg.Done()
			if rand.Intn(2) == 0 {
				time.Sleep(time.Duration(rand.Intn(500)) * time.Microsecond)
			}
			_ = sup.Stop(context.Background())
		}()

		wg.Wait()
		// Ensure stopped
		_ = sup.Stop(context.Background())
	}
}

// TestStressAllMountPositionsFailureInjection iterates through every single mount position
// in buildMountPlan, injects a failure at that specific position, and verifies 100% clean LIFO unwinding.
func TestStressAllMountPositionsFailureInjection(t *testing.T) {
	dummyRootfs := "C:\\dummy\\rootfs"
	dummyData := "C:\\dummy\\data"
	dummyBundle := "C:\\dummy\\bundle"
	plan := buildMountPlan(dummyRootfs, dummyData, dummyBundle)

	for idx, entry := range plan {
		t.Run(fmt.Sprintf("Pos_%d_%s", idx, entry.Name), func(t *testing.T) {
			rootfs := t.TempDir()
			dataRoot := t.TempDir()
			projectDir := setupTestProject(t)

			mounter := NewSimulatedMounter()
			sup := NewSimulatedSupervisor(Config{UpstreamURL: "http://127.0.0.1:8089"})

			actualPlan := buildMountPlan(rootfs, dataRoot, projectDir)
			targetToFail := actualPlan[idx].Target
			isOptional := actualPlan[idx].Optional

			mounter.SetFailOnMount(targetToFail)

			drv, err := New(Config{
				RootfsPath:  rootfs,
				DataRoot:    dataRoot,
				UpstreamURL: "http://127.0.0.1:8089",
				Mounter:     mounter,
				Supervisor:  sup,
			})
			if err != nil {
				t.Fatalf("failed to create driver: %v", err)
			}

			ctx := context.Background()
			activateErr := drv.Activate(ctx, projectDir)

			if isOptional {
				// Optional mounts (devpts, devshm) MUST NOT fail activation
				if activateErr != nil {
					t.Fatalf("expected activation to succeed when optional mount %s fails, got: %v", entry.Name, activateErr)
				}
				if !sup.Running() {
					t.Fatal("supervisor should be running after successful activation with optional mount failure")
				}
				// Clean teardown
				if err := drv.Stop(ctx); err != nil {
					t.Fatalf("clean teardown failed: %v", err)
				}
			} else {
				// Non-optional mounts MUST fail activation, unwind completely, and clear ActiveRelease
				if activateErr == nil {
					t.Fatalf("expected activation to fail when mandatory mount %s fails", entry.Name)
				}

				// 1. ActiveRelease must be empty
				if active := drv.ActiveRelease(); active != "" {
					t.Fatalf("ActiveRelease was not cleared on failure, got %q", active)
				}

				// 2. MountStack must be 0
				if stack := drv.MountStack(); len(stack) != 0 {
					t.Fatalf("MountStack not empty on failure, got %v", stack)
				}

				// 3. Mounter must have 0 active mounts under rootfs
				activeMounts, err := mounter.ListMountsUnder(rootfs)
				if err != nil {
					t.Fatal(err)
				}
				if len(activeMounts) != 0 {
					t.Fatalf("Mounter still has %d active mounts: %v", len(activeMounts), activeMounts)
				}

				// 4. Verify exact LIFO reverse ordering of unmount operations
				history := mounter.History()
				var mountedTargets []string
				var unmountedTargets []string
				for _, op := range history {
					if op.Op == "mount" {
						if len(mountedTargets) == 0 || mountedTargets[len(mountedTargets)-1] != op.Target {
							mountedTargets = append(mountedTargets, op.Target)
						}
					} else if op.Op == "unmount" {
						unmountedTargets = append(unmountedTargets, op.Target)
					}
				}

				if len(mountedTargets) != len(unmountedTargets) {
					t.Fatalf("mounted (%d) != unmounted (%d). Mounted: %v, Unmounted: %v",
						len(mountedTargets), len(unmountedTargets), mountedTargets, unmountedTargets)
				}

				for i := 0; i < len(mountedTargets); i++ {
					expected := mountedTargets[len(mountedTargets)-1-i]
					actual := unmountedTargets[i]
					if expected != actual {
						t.Fatalf("LIFO violation at step %d: expected %s, got %s", i, expected, actual)
					}
				}

				// 5. Supervisor must not be running
				if sup.Running() {
					t.Fatalf("supervisor must not be running after failed activation rollback")
				}
			}
		})
	}
}

// TestStressDNSResolvConfScenarios tests setupDNS against various adversarial filesystem states:
// 1. Relative dangling symlink (Debian standard)
// 2. Absolute dangling symlink
// 3. Pre-existing regular file with content
func TestStressDNSResolvConfScenarios(t *testing.T) {
	t.Run("RelativeDanglingSymlink", func(t *testing.T) {
		rootfs := t.TempDir()
		etcDir := filepath.Join(rootfs, "etc")
		if err := os.MkdirAll(etcDir, 0755); err != nil {
			t.Fatal(err)
		}
		sym := filepath.Join(etcDir, "resolv.conf")
		if err := os.Symlink(filepath.Join("..", "run", "systemd", "resolve", "stub-resolv.conf"), sym); err != nil {
			t.Skipf("symlink not supported: %v", err)
		}
		if err := setupDNS(rootfs); err != nil {
			t.Fatalf("setupDNS failed on relative dangling symlink: %v", err)
		}
		content, err := os.ReadFile(sym)
		if err != nil {
			t.Fatalf("failed to read resolv.conf: %v", err)
		}
		if len(content) == 0 {
			t.Fatal("resolv.conf content is empty")
		}
	})

	t.Run("AbsoluteDanglingSymlink", func(t *testing.T) {
		rootfs := t.TempDir()
		etcDir := filepath.Join(rootfs, "etc")
		if err := os.MkdirAll(etcDir, 0755); err != nil {
			t.Fatal(err)
		}
		sym := filepath.Join(etcDir, "resolv.conf")
		if err := os.Symlink("C:\\nonexistent\\path\\resolv.conf", sym); err != nil {
			t.Skipf("symlink not supported: %v", err)
		}
		if err := setupDNS(rootfs); err != nil {
			t.Fatalf("setupDNS failed on absolute dangling symlink: %v", err)
		}
		content, err := os.ReadFile(sym)
		if err != nil {
			t.Fatalf("failed to read resolv.conf: %v", err)
		}
		if len(content) == 0 {
			t.Fatal("resolv.conf content is empty")
		}
	})

	t.Run("PreExistingRegularFile", func(t *testing.T) {
		rootfs := t.TempDir()
		etcDir := filepath.Join(rootfs, "etc")
		if err := os.MkdirAll(etcDir, 0755); err != nil {
			t.Fatal(err)
		}
		f := filepath.Join(etcDir, "resolv.conf")
		if err := os.WriteFile(f, []byte("nameserver 10.0.0.1"), 0644); err != nil {
			t.Fatal(err)
		}
		if err := setupDNS(rootfs); err != nil {
			t.Fatalf("setupDNS failed on pre-existing regular file: %v", err)
		}
	})
}
