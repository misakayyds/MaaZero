package driver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"maa-phone-manager/manager/internal/control"
	"maa-phone-manager/manager/internal/manifest"
	"maa-phone-manager/manager/internal/runtime"
)

// Driver implements control.Driver to manage the Debian chroot runtime lifecycle,
// filesystems mounts, process group isolation and process supervision.
type Driver struct {
	mu         sync.Mutex
	cfg        Config
	mounter    Mounter
	supervisor Supervisor
	locker     Locker
	realHost   bool
	activeDir  string
	mountStack []string
	hostCancel context.CancelFunc
}

// Ensure Driver implements control.Driver at compile time.
var _ control.Driver = (*Driver)(nil)

// New initializes a new chroot Driver.
func New(cfg Config) (*Driver, error) {
	realHost := cfg.Mounter == nil && cfg.Supervisor == nil
	cfg.setDefaults()

	mounter := cfg.Mounter
	if mounter == nil {
		mounter = newDefaultMounter()
	}

	supervisor := cfg.Supervisor
	if supervisor == nil {
		supervisor = newDefaultSupervisor(cfg)
	}

	locker := cfg.Locker
	if locker == nil {
		locker = newDefaultLocker()
	}

	return &Driver{
		cfg:        cfg,
		mounter:    mounter,
		supervisor: supervisor,
		locker:     locker,
		realHost:   realHost,
	}, nil
}

// Acquire takes the live task lock inode on <rootfs>/tmp/maa-task.lock.
// It waits until the lock is free or ctx is cancelled.
func (d *Driver) Acquire(ctx context.Context) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	lockPath := filepath.Join(d.cfg.RootfsPath, "tmp", "maa-task.lock")
	return d.locker.Acquire(ctx, lockPath)
}

// Validate verifies a candidate bundle before switching.
// When m is nil, offline rollback validation is performed using the directory layout alone.
func (d *Driver) Validate(ctx context.Context, m *manifest.ChannelManifest, bundleDir string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if bundleDir == "" || !filepath.IsAbs(bundleDir) {
		return fmt.Errorf("driver: candidate bundle path must be absolute: %q", bundleDir)
	}
	info, err := os.Stat(bundleDir)
	if err != nil {
		return fmt.Errorf("driver: cannot access bundle %s: %w", bundleDir, err)
	}
	if !info.IsDir() {
		return fmt.Errorf("driver: bundle %s must be a directory", bundleDir)
	}

	// If candidate contains project files (e.g. maa-run.sh), perform deep project scan
	runnerPath := filepath.Join(bundleDir, "maa-run.sh")
	if _, err := os.Stat(runnerPath); err == nil {
		if err := runtime.ScanProjectDir(bundleDir); err != nil {
			return fmt.Errorf("driver: project validation failed: %w", err)
		}
	}

	// Validate manifest compatibility if manifest is supplied
	if m != nil {
		if m.Channel == "" {
			return errors.New("driver: manifest declares empty channel")
		}
	}

	return nil
}

// Activate configures mounts, starts supervised processes, and verifies WebUI loopback readiness.
func (d *Driver) Activate(ctx context.Context, bundleDir string) error {
	d.mu.Lock()
	defer d.mu.Unlock()

	if err := ctx.Err(); err != nil {
		return err
	}
	if bundleDir == "" {
		return errors.New("driver: cannot activate empty bundle directory")
	}

	// 1. Stop any running host watchdog and supervised processes before switching
	d.stopHostWatchdogLocked()
	if d.supervisor.Running() {
		if err := d.supervisor.Stop(ctx); err != nil {
			return fmt.Errorf("stop existing supervisor: %w", err)
		}
	}

	// 2. Perform mounts if not already mounted
	if len(d.mountStack) == 0 {
		// Clean any lingering stale mounts from previous runs/crashes
		_ = cleanStaleMounts(d.mounter, d.cfg.RootfsPath)

		entries := buildMountPlan(d.cfg.RootfsPath, d.cfg.DataRoot, bundleDir)
		if err := prepareMountDirectories(entries); err != nil {
			return fmt.Errorf("prepare mount directories: %w", err)
		}

		for _, e := range entries {
			// Ensure target directory exists on currently visible VFS layer
			perm := os.FileMode(0755)
			if e.Name == "tmp" || e.Name == "devshm" {
				perm = 0777
			}
			if err := os.MkdirAll(e.Target, perm); err != nil {
				if !e.Optional {
					_ = unmountLIFO(d.mounter, d.mountStack)
					d.mountStack = nil
					d.activeDir = ""
					return fmt.Errorf("prepare mount target %s: %w", e.Target, err)
				}
				continue
			}

			err := d.mounter.Mount(e.Source, e.Target, e.FSType, e.Flags, e.Data)
			if err != nil {
				if e.Optional {
					continue
				}
				// Rollback partial mounts in reverse LIFO order
				_ = unmountLIFO(d.mounter, d.mountStack)
				d.mountStack = nil
				d.activeDir = ""
				return fmt.Errorf("mount %s to %s: %w", e.Name, e.Target, err)
			}
			if e.MakeSlave {
				_ = d.mounter.Mount("", e.Target, "", FlagSlave|FlagRec, "")
			}
			d.mountStack = append(d.mountStack, e.Target)
		}

		// Configure DNS resolution inside rootfs
		if err := setupDNS(d.cfg.RootfsPath); err != nil {
			_ = unmountLIFO(d.mounter, d.mountStack)
			d.mountStack = nil
			d.activeDir = ""
			return fmt.Errorf("setup dns: %w", err)
		}
	} else if d.activeDir != bundleDir {
		// Filesystems already mounted, update project bind mount to new release bundle
		bundleTarget := filepath.Join(d.cfg.RootfsPath, "root", "maa-scripts")
		if err := d.mounter.Unmount(bundleTarget, 0); err != nil {
			_ = d.mounter.Unmount(bundleTarget, UnmountDetach)
		}
		if err := d.mounter.Mount(bundleDir, bundleTarget, "", FlagBind|FlagReadOnly, ""); err != nil {
			_ = unmountLIFO(d.mounter, d.mountStack)
			d.mountStack = nil
			d.activeDir = ""
			return fmt.Errorf("remount bundle to %s: %w", bundleTarget, err)
		}
	}

	// 3. Seed default user configuration files if absent and prepare host runtime
	if err := seedDefaultConfigs(d.cfg.DataRoot); err != nil {
		_ = unmountLIFO(d.mounter, d.mountStack)
		d.mountStack = nil
		d.activeDir = ""
		return fmt.Errorf("seed default configs: %w", err)
	}
	if d.realHost {
		if err := prepareHostRuntime(ctx, d.cfg); err != nil {
			_ = unmountLIFO(d.mounter, d.mountStack)
			d.mountStack = nil
			d.activeDir = ""
			return fmt.Errorf("prepare host runtime: %w", err)
		}
	}

	// 4. Start supervised processes (WebUI, crond)
	if err := d.supervisor.Start(ctx, d.cfg, bundleDir); err != nil {
		_ = unmountLIFO(d.mounter, d.mountStack)
		d.mountStack = nil
		d.activeDir = ""
		return fmt.Errorf("start supervisor: %w", err)
	}

	// 5. Actively probe WebUI loopback readiness before returning success
	readyCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	if err := d.supervisor.WaitReady(readyCtx, 15*time.Second); err != nil {
		_ = d.supervisor.Stop(context.Background())
		_ = unmountLIFO(d.mounter, d.mountStack)
		d.mountStack = nil
		d.activeDir = ""
		return fmt.Errorf("webui readiness probe failed: %w", err)
	}

	d.activeDir = bundleDir
	if d.realHost {
		d.hostCancel = startHostWatchdog(d.cfg)
	}
	return nil
}

func (d *Driver) stopHostWatchdogLocked() {
	if d.hostCancel != nil {
		d.hostCancel()
		d.hostCancel = nil
	}
}

// Stop gracefully stops supervised processes, unmounts all bind mounts in reverse order,
// and cleans up any lingering rootfs mounts.
func (d *Driver) Stop(ctx context.Context) error {
	d.mu.Lock()
	defer d.mu.Unlock()

	var errs []error
	d.stopHostWatchdogLocked()

	// 1. Terminate all supervised processes BEFORE unmounting
	if err := d.supervisor.Stop(ctx); err != nil {
		errs = append(errs, fmt.Errorf("stop supervisor: %w", err))
	}

	// 2. Unmount all mounted targets in strict reverse LIFO order
	if len(d.mountStack) > 0 {
		if err := unmountLIFO(d.mounter, d.mountStack); err != nil {
			errs = append(errs, err)
		}
		d.mountStack = nil
	}

	// 3. Clean any remaining stale mounts under rootfs
	if err := cleanStaleMounts(d.mounter, d.cfg.RootfsPath); err != nil {
		errs = append(errs, err)
	}

	d.activeDir = ""
	return errors.Join(errs...)
}

// ActiveRelease returns the currently activated release directory, or empty if not active.
func (d *Driver) ActiveRelease() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.activeDir
}

// MountStack returns a copy of currently mounted paths.
func (d *Driver) MountStack() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, len(d.mountStack))
	copy(out, d.mountStack)
	return out
}
