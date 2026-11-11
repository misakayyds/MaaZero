package driver

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// Platform-independent mount flags.
const (
	FlagBind     uintptr = 1 << 0
	FlagRec      uintptr = 1 << 1
	FlagSlave    uintptr = 1 << 2
	FlagNoSuid   uintptr = 1 << 3
	FlagNoDev    uintptr = 1 << 4
	FlagNoExec   uintptr = 1 << 5
	FlagReadOnly uintptr = 1 << 6
)

// Platform-independent unmount flags.
const (
	UnmountDetach = 1 << 0
)

// Mounter abstracts Linux filesystem mounting for cross-platform support.
type Mounter interface {
	Mount(source, target, fstype string, flags uintptr, data string) error
	Unmount(target string, flags int) error
	IsMounted(target string) (bool, error)
	ListMountsUnder(prefix string) ([]string, error)
}

// MountEntry describes one mount operation.
type MountEntry struct {
	Name      string
	Source    string
	Target    string
	FSType    string
	Flags     uintptr
	Data      string
	MakeSlave bool
	Optional  bool
}

// buildMountPlan constructs the 16 ordered mount operations.
func buildMountPlan(rootfs, dataRoot, bundleDir string) []MountEntry {
	rootfs = filepath.Clean(rootfs)
	dataRoot = filepath.Clean(dataRoot)
	bundleDir = filepath.Clean(bundleDir)

	return []MountEntry{
		// 1. Pseudo: /proc
		{
			Name:   "proc",
			Source: "proc",
			Target: filepath.Join(rootfs, "proc"),
			FSType: "proc",
			Flags:  FlagNoSuid | FlagNoDev | FlagNoExec,
		},
		// 2. Pseudo: /sys
		{
			Name:      "sys",
			Source:    "/sys",
			Target:    filepath.Join(rootfs, "sys"),
			FSType:    "",
			Flags:     FlagBind | FlagRec,
			MakeSlave: true,
		},
		// 3. Pseudo: /dev
		{
			Name:      "dev",
			Source:    "/dev",
			Target:    filepath.Join(rootfs, "dev"),
			FSType:    "",
			Flags:     FlagBind | FlagRec,
			MakeSlave: true,
		},
		// 4. Pseudo: /dev/pts (newinstance devpts, optional fallback if /dev already has pts)
		{
			Name:     "devpts",
			Source:   "devpts",
			Target:   filepath.Join(rootfs, "dev", "pts"),
			FSType:   "devpts",
			Flags:    FlagNoSuid | FlagNoExec,
			Data:     "newinstance,ptmxmode=0666,mode=620,gid=5",
			Optional: true,
		},
		// 5. Pseudo: /dev/shm
		{
			Name:     "devshm",
			Source:   "tmpfs",
			Target:   filepath.Join(rootfs, "dev", "shm"),
			FSType:   "tmpfs",
			Flags:    FlagNoSuid | FlagNoDev | FlagNoExec,
			Data:     "mode=1777",
			Optional: true,
		},
		// 6. Pseudo: /tmp
		{
			Name:   "tmp",
			Source: "tmpfs",
			Target: filepath.Join(rootfs, "tmp"),
			FSType: "tmpfs",
			Flags:  FlagNoSuid | FlagNoDev,
			Data:   "mode=1777",
		},
		// 7. User vol: config
		{
			Name:   "vol-config",
			Source: filepath.Join(dataRoot, "data", "config"),
			Target: filepath.Join(rootfs, "root", ".config", "maa"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 8. User vol: state
		{
			Name:   "vol-state",
			Source: filepath.Join(dataRoot, "data", "state"),
			Target: filepath.Join(rootfs, "root", ".local", "state", "maa"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 9. User vol: cache
		{
			Name:   "vol-cache",
			Source: filepath.Join(dataRoot, "data", "cache"),
			Target: filepath.Join(rootfs, "root", ".cache", "maa"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 10. User vol: share
		{
			Name:   "vol-share",
			Source: filepath.Join(dataRoot, "data", "share"),
			Target: filepath.Join(rootfs, "root", ".local", "share", "maa"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 11. User vol: logs
		{
			Name:   "vol-logs",
			Source: filepath.Join(dataRoot, "logs"),
			Target: filepath.Join(rootfs, "var", "log", "maa"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 12. Pseudo: /run (for cron pidfiles and transient runtime sockets)
		{
			Name:   "run",
			Source: "tmpfs",
			Target: filepath.Join(rootfs, "run"),
			FSType: "tmpfs",
			Flags:  FlagNoSuid | FlagNoDev,
			Data:   "mode=0755",
		},
		// 13. User vol: local binaries (maa-cli)
		{
			Name:   "vol-bin",
			Source: filepath.Join(dataRoot, "data", "bin"),
			Target: filepath.Join(rootfs, "root", ".local", "bin"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 14. User vol: persistent Android ADB keypair
		{
			Name:   "vol-android",
			Source: filepath.Join(dataRoot, "data", "android"),
			Target: filepath.Join(rootfs, "root", ".android"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 15. User vol: persistent cron spool
		{
			Name:   "vol-crontabs",
			Source: filepath.Join(dataRoot, "data", "crontabs"),
			Target: filepath.Join(rootfs, "var", "spool", "cron", "crontabs"),
			FSType: "",
			Flags:  FlagBind,
		},
		// 16. Project release bundle (immutable release, read-only bind mount)
		{
			Name:   "bundle-project",
			Source: bundleDir,
			Target: filepath.Join(rootfs, "root", "maa-scripts"),
			FSType: "",
			Flags:  FlagBind | FlagReadOnly,
		},
	}
}

// prepareMountDirectories creates required host directories and rootfs mount points.
func prepareMountDirectories(entries []MountEntry) error {
	for _, e := range entries {
		// If it's a bind mount with an existing directory path on host
		if e.Flags&FlagBind != 0 && filepath.IsAbs(e.Source) {
			if err := os.MkdirAll(e.Source, 0700); err != nil {
				return fmt.Errorf("prepare source dir %s: %w", e.Source, err)
			}
		}
		// Target mount point in rootfs
		perm := os.FileMode(0755)
		if e.Name == "tmp" || e.Name == "devshm" {
			perm = 0777
		}
		if err := os.MkdirAll(e.Target, perm); err != nil {
			return fmt.Errorf("prepare target dir %s: %w", e.Target, err)
		}
	}
	return nil
}

// setupDNS writes standard resolv.conf to <rootfs>/etc/resolv.conf.
func setupDNS(rootfs string) error {
	etcDir := filepath.Join(rootfs, "etc")
	if err := os.MkdirAll(etcDir, 0755); err != nil {
		return fmt.Errorf("create rootfs etc dir: %w", err)
	}
	resolvPath := filepath.Join(etcDir, "resolv.conf")
	_ = os.Remove(resolvPath)
	content := "# Generated by maa-managerd\n" +
		"options timeout:1 attempts:1\n" +
		"nameserver 223.5.5.5\n" +
		"nameserver 119.29.29.29\n" +
		"nameserver 1.1.1.1\n"
	return os.WriteFile(resolvPath, []byte(content), 0644)
}

// unmountLIFO unmounts targets in strict reverse order with MNT_DETACH fallback.
func unmountLIFO(mounter Mounter, stack []string) error {
	var errs []error
	for i := len(stack) - 1; i >= 0; i-- {
		target := stack[i]
		err := mounter.Unmount(target, 0)
		if err != nil {
			// Fallback to lazy detach
			err = mounter.Unmount(target, UnmountDetach)
		}
		if err != nil {
			errs = append(errs, fmt.Errorf("unmount %s: %w", target, err))
		}
	}
	return errors.Join(errs...)
}

// cleanStaleMounts finds any existing mounts under rootfs and unmounts deepest first.
func cleanStaleMounts(mounter Mounter, rootfs string) error {
	mounts, err := mounter.ListMountsUnder(rootfs)
	if err != nil {
		return err
	}
	if len(mounts) == 0 {
		return nil
	}
	// Sort deepest path first
	sort.Slice(mounts, func(i, j int) bool {
		return len(mounts[i]) > len(mounts[j])
	})
	var errs []error
	for _, m := range mounts {
		err := mounter.Unmount(m, 0)
		if err != nil {
			err = mounter.Unmount(m, UnmountDetach)
		}
		if err != nil {
			errs = append(errs, fmt.Errorf("clean mount %s: %w", m, err))
		}
	}
	return errors.Join(errs...)
}
