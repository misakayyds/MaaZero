package driver

import (
	"os"
	"os/exec"
	"path/filepath"
)

// Config configures the Chroot runtime driver.
type Config struct {
	DataRoot    string // e.g. /data/local/maa-platform
	RootfsPath  string // e.g. /data/local/maa-platform/runtime/rootfs
	BusyboxPath string // e.g. /system/bin/busybox
	UpstreamURL string // loopback WebUI URL, e.g. http://127.0.0.1:8089
	WebUIPort   int    // loopback port, default 8089

	// Mounter, Supervisor and Locker are seams for unit testing.
	// When nil, production implementations (or platform defaults) are used.
	Mounter    Mounter
	Supervisor Supervisor
	Locker     Locker
}

func (c *Config) setDefaults() {
	if c.DataRoot == "" {
		c.DataRoot = "/data/local/maa-platform"
	}
	if c.RootfsPath == "" {
		c.RootfsPath = filepath.Join(c.DataRoot, "runtime", "rootfs")
	}
	if c.BusyboxPath == "" {
		c.BusyboxPath = ResolveBusybox()
	}
	if c.UpstreamURL == "" {
		c.UpstreamURL = "http://127.0.0.1:8089"
	}
	if c.WebUIPort <= 0 {
		c.WebUIPort = 8089
	}
}

// ResolveBusybox finds the busybox binary on Android or the host.
func ResolveBusybox() string {
	candidates := []string{
		"/system/bin/busybox",
		"/data/adb/magisk/busybox",
		"/data/adb/ksu/bin/busybox",
		"/data/adb/ap/bin/busybox",
	}
	for _, candidate := range candidates {
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	if path, err := exec.LookPath("busybox"); err == nil {
		return path
	}
	return "/system/bin/busybox"
}
