//go:build linux

package driver

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// prepareHostRuntime configures Debian cron spool permissions and rooted Android
// loopback ADB (127.0.0.1:5555) authorization before supervised processes start.
func prepareHostRuntime(ctx context.Context, cfg Config) error {
	prepareCronSpool(cfg.RootfsPath, cfg.DataRoot)

	if _, err := os.Stat("/system/bin/setprop"); err != nil {
		// Not running on an Android host; skip Android adbd preparation.
		return nil
	}
	return prepareAndroidLoopbackADB(ctx, cfg)
}

func prepareCronSpool(rootfs, dataRoot string) {
	crontabGID := resolveGroupGID(filepath.Join(rootfs, "etc", "group"), "crontab", 105)
	for _, dir := range []string{
		filepath.Join(dataRoot, "data", "crontabs"),
		filepath.Join(rootfs, "var", "spool", "cron", "crontabs"),
	} {
		_ = os.Chown(dir, 0, crontabGID)
		_ = os.Chmod(dir, os.ModeSticky|0730)
	}
	rootCron := filepath.Join(dataRoot, "data", "crontabs", "root")
	if info, err := os.Lstat(rootCron); err == nil && info.Mode().IsRegular() {
		_ = os.Chown(rootCron, 0, crontabGID)
		_ = os.Chmod(rootCron, 0600)
	}
}

func resolveGroupGID(groupFile, groupName string, fallback int) int {
	raw, err := os.ReadFile(groupFile)
	if err != nil {
		return fallback
	}
	prefix := groupName + ":"
	for _, line := range strings.Split(string(raw), "\n") {
		if !strings.HasPrefix(line, prefix) {
			continue
		}
		parts := strings.Split(line, ":")
		if len(parts) >= 3 {
			if gid, err := strconv.Atoi(strings.TrimSpace(parts[2])); err == nil && gid >= 0 {
				return gid
			}
		}
	}
	return fallback
}

func prepareAndroidLoopbackADB(ctx context.Context, cfg Config) error {
	androidDir := filepath.Join(cfg.DataRoot, "data", "android")
	if err := os.MkdirAll(androidDir, 0700); err != nil {
		return fmt.Errorf("prepare android key dir: %w", err)
	}

	privKey := filepath.Join(androidDir, "adbkey")
	pubKey := filepath.Join(androidDir, "adbkey.pub")
	chrootADB := filepath.Join(cfg.RootfsPath, "usr", "bin", "adb")

	if !fileNonEmpty(privKey) || !fileNonEmpty(pubKey) {
		if _, err := os.Stat(chrootADB); err == nil {
			_ = os.Remove(privKey)
			_ = os.Remove(pubKey)
			genCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
			cmd := exec.CommandContext(genCtx, cfg.BusyboxPath, "chroot", cfg.RootfsPath, "/usr/bin/adb", "keygen", "/root/.android/adbkey")
			cmd.Env = []string{
				"HOME=/root",
				"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
			}
			out, genErr := cmd.CombinedOutput()
			cancel()
			if genErr != nil {
				return fmt.Errorf("adb keygen failed: %w (%s)", genErr, strings.TrimSpace(string(out)))
			}
		}
	}
	_ = os.Chmod(privKey, 0600)
	_ = os.Chmod(pubKey, 0644)

	keyAdded := false
	if pubBytes, err := os.ReadFile(pubKey); err == nil {
		pubLine := strings.TrimSpace(string(pubBytes))
		if pubLine != "" {
			added, authErr := ensureAndroidADBAuthorized(androidDir, pubLine)
			if authErr != nil {
				return authErr
			}
			keyAdded = added
		}
	}

	const adbAddr = "127.0.0.1:5555"
	if err := waitForAndroidBootCompleted(ctx); err != nil {
		return err
	}
	restrictADBPortToLoopback(ctx)

	adbEnabled := readAndroidGlobalSetting(ctx, "adb_enabled")
	persistPort := readAndroidProp(ctx, "persist.adb.tcp.port")
	if keyAdded || adbEnabled != "1" || persistPort != "5555" || !tcpReachable(adbAddr, 400*time.Millisecond) {
		if err := ensureAndroidADBDListening(ctx, adbAddr, keyAdded); err != nil {
			return err
		}
	}

	if _, err := os.Stat(chrootADB); err == nil && tcpReachable(adbAddr, 400*time.Millisecond) {
		if err := connectChrootADB(ctx, cfg, adbAddr, 4); err != nil {
			return err
		}
	}

	return nil
}

// startHostWatchdog keeps loopback ADB (127.0.0.1:5555) online on OEM ROMs
// (such as ColorOS / Android 16) that disable adb_enabled ~20s after boot or after idle timeouts.
// When 127.0.0.1:5555 is healthy, each tick performs only a sub-millisecond loopback dial.
func startHostWatchdog(cfg Config) context.CancelFunc {
	if _, err := os.Stat("/system/bin/setprop"); err != nil {
		return func() {}
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		const adbAddr = "127.0.0.1:5555"
		chrootADB := filepath.Join(cfg.RootfsPath, "usr", "bin", "adb")
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()

		verifiedAuth := false
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if tcpReachable(adbAddr, 300*time.Millisecond) {
					if !verifiedAuth {
						if _, err := os.Stat(chrootADB); err == nil {
							if chrootADBDeviceReady(ctx, cfg, adbAddr) {
								verifiedAuth = true
							} else if connectChrootADB(ctx, cfg, adbAddr, 1) == nil && chrootADBDeviceReady(ctx, cfg, adbAddr) {
								verifiedAuth = true
							}
						} else {
							verifiedAuth = true
						}
					}
					continue
				}

				verifiedAuth = false
				if readAndroidProp(ctx, "sys.boot_completed") != "1" {
					continue
				}
				restrictADBPortToLoopback(ctx)
				_ = ensureAndroidADBDListening(ctx, adbAddr, false)
				if _, err := os.Stat(chrootADB); err == nil && tcpReachable(adbAddr, 400*time.Millisecond) {
					if connectChrootADB(ctx, cfg, adbAddr, 2) == nil && chrootADBDeviceReady(ctx, cfg, adbAddr) {
						verifiedAuth = true
					}
				}
			}
		}
	}()
	return cancel
}

func ensureAndroidADBDListening(ctx context.Context, adbAddr string, forceRestart bool) error {
	restartCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	forceFlag := "0"
	if forceRestart {
		forceFlag = "1"
	}
	script := fmt.Sprintf(`
was_running=0
if [ "$(/system/bin/getprop init.svc.adbd 2>/dev/null)" = "running" ]; then
    was_running=1
fi
/system/bin/setprop persist.adb.tcp.port 5555 || true
/system/bin/setprop service.adb.tcp.port 5555 || true
if [ "$(/system/bin/settings get global adb_enabled 2>/dev/null)" != "1" ]; then
    /system/bin/settings put global adb_enabled 1 >/dev/null 2>&1 || true
    sleep 1
fi
if [ "$was_running" = "1" ] || [ "%s" = "1" ]; then
    stop adbd >/dev/null 2>&1 || true
    sleep 1
fi
start adbd >/dev/null 2>&1 || true
`, forceFlag)
	cmd := exec.CommandContext(restartCtx, "/system/bin/sh", "-c", script)
	_ = cmd.Run()
	cancel()

	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		if tcpReachable(adbAddr, 300*time.Millisecond) {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
	return nil
}

func chrootADBDeviceReady(ctx context.Context, cfg Config, adbAddr string) bool {
	connCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	devs := exec.CommandContext(connCtx, cfg.BusyboxPath, "chroot", cfg.RootfsPath, "/usr/bin/adb", "devices")
	devs.Env = []string{
		"HOME=/root",
		"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
	}
	out, err := devs.CombinedOutput()
	return err == nil && strings.Contains(string(out), adbAddr+"\tdevice")
}

func connectChrootADB(ctx context.Context, cfg Config, adbAddr string, attempts int) error {
	chrootEnv := []string{
		"HOME=/root",
		"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
	}
	for attempt := 0; attempt < attempts; attempt++ {
		connCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
		if attempt > 0 {
			disc := exec.CommandContext(connCtx, cfg.BusyboxPath, "chroot", cfg.RootfsPath, "/usr/bin/adb", "disconnect", adbAddr)
			disc.Env = chrootEnv
			_ = disc.Run()
		}
		cmd := exec.CommandContext(connCtx, cfg.BusyboxPath, "chroot", cfg.RootfsPath, "/usr/bin/adb", "connect", adbAddr)
		cmd.Env = chrootEnv
		_ = cmd.Run()
		devs := exec.CommandContext(connCtx, cfg.BusyboxPath, "chroot", cfg.RootfsPath, "/usr/bin/adb", "devices")
		devs.Env = chrootEnv
		out, _ := devs.CombinedOutput()
		cancel()
		if strings.Contains(string(out), adbAddr+"\tdevice") {
			return nil
		}
		if attempt+1 < attempts {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(1500 * time.Millisecond):
			}
		}
	}
	return nil
}

func waitForAndroidBootCompleted(ctx context.Context) error {
	if readAndroidProp(ctx, "sys.boot_completed") == "1" {
		return nil
	}
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		if readAndroidProp(ctx, "sys.boot_completed") == "1" {
			// Allow vendor/OEM boot-completed receivers (e.g. ColorOS UsbService) to
			// settle before enabling adbd so they do not immediately turn it back off.
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(4 * time.Second):
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(1 * time.Second):
		}
	}
	return nil
}

func readAndroidProp(ctx context.Context, prop string) string {
	cctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	out, err := exec.CommandContext(cctx, "/system/bin/getprop", prop).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func readAndroidGlobalSetting(ctx context.Context, key string) string {
	if _, err := os.Stat("/system/bin/settings"); err != nil {
		return ""
	}
	cctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(cctx, "/system/bin/settings", "get", "global", key).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func restrictADBPortToLoopback(ctx context.Context) {
	for _, bin := range []string{"/system/bin/iptables", "/system/bin/ip6tables"} {
		if _, err := os.Stat(bin); err != nil {
			continue
		}
		cctx, cancel := context.WithTimeout(ctx, 4*time.Second)
		chk := exec.CommandContext(cctx, bin, "-w", "-C", "INPUT", "!", "-i", "lo", "-p", "tcp", "--dport", "5555", "-j", "DROP")
		if chk.Run() != nil {
			_ = exec.CommandContext(cctx, bin, "-w", "-I", "INPUT", "1", "!", "-i", "lo", "-p", "tcp", "--dport", "5555", "-j", "DROP").Run()
		}
		cancel()
	}
}

func ensureAndroidADBAuthorized(backupDir, pubLine string) (bool, error) {
	const adbKeysDir = "/data/misc/adb"
	const adbKeysPath = "/data/misc/adb/adb_keys"

	if info, err := os.Stat(adbKeysDir); err != nil || !info.IsDir() {
		return false, nil
	}

	existing, err := os.ReadFile(adbKeysPath)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return false, fmt.Errorf("read %s: %w", adbKeysPath, err)
	}

	pubMaterial := adbKeyMaterial(pubLine)
	for _, line := range strings.Split(string(existing), "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		if trimmed == pubLine || (pubMaterial != "" && adbKeyMaterial(trimmed) == pubMaterial) {
			return false, nil
		}
	}

	backupPath := filepath.Join(backupDir, "adb_keys.bak")
	if len(existing) > 0 && !fileNonEmpty(backupPath) {
		_ = os.WriteFile(backupPath, existing, 0600)
	}

	var merged strings.Builder
	if trimmedExisting := strings.TrimRight(string(existing), "\r\n"); trimmedExisting != "" {
		merged.WriteString(trimmedExisting)
		merged.WriteByte('\n')
	}
	merged.WriteString(pubLine)
	merged.WriteByte('\n')

	tmpPath := adbKeysPath + ".maa.tmp"
	if err := os.WriteFile(tmpPath, []byte(merged.String()), 0640); err != nil {
		return false, fmt.Errorf("write %s: %w", tmpPath, err)
	}
	// system (1000) : shell (2000) 0640 is Android adbd's expected ownership on /data/misc/adb/adb_keys.
	_ = os.Chown(tmpPath, 1000, 2000)
	_ = os.Chmod(tmpPath, 0640)
	if err := os.Rename(tmpPath, adbKeysPath); err != nil {
		_ = os.Remove(tmpPath)
		return false, fmt.Errorf("rename %s: %w", adbKeysPath, err)
	}
	_ = os.Chown(adbKeysPath, 1000, 2000)
	_ = os.Chmod(adbKeysPath, 0640)
	if _, err := os.Stat("/system/bin/restorecon"); err == nil {
		_ = exec.Command("/system/bin/restorecon", adbKeysPath).Run()
	}
	return true, nil
}

func adbKeyMaterial(line string) string {
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return ""
	}
	if len(fields) >= 2 && (strings.HasPrefix(fields[0], "ssh-") || strings.HasPrefix(fields[0], "ecdsa-")) {
		return fields[0] + " " + fields[1]
	}
	return fields[0]
}

func fileNonEmpty(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular() && info.Size() > 0
}

func tcpReachable(addr string, timeout time.Duration) bool {
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}
