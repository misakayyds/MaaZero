package runtime

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const (
	// The manager authenticates users and serves the daily UI on 8088, proxying
	// to the WebUI upstream. A distributed WebUI must therefore never bind a
	// public address, and must come up on the private upstream port.
	webuiScriptName     = "maa-webui.py"
	webuiPublicAddress  = "0.0.0.0"
	webuiListenAddress  = "127.0.0.1"
	webuiPortAnchor     = "PORT = 8089"
	webuiLoopbackAnchor = `server = WEB_SERVER_CLASS(("127.0.0.1", PORT), Handler)`

	maxProjectFileBytes = 4 << 20
	maxProjectBytes     = 64 << 20
	maxProjectFiles     = 4096
)

// requiredProjectFiles are the files a distributed project component cannot do
// without. start_webui.sh is deliberately not here: it writes /etc/rc.local and
// pkills by pattern, so the manager owns process start instead.
var requiredProjectFiles = []string{runnerScriptName, "maa-auto-update.sh", webuiScriptName}

var allowedProjectExtensions = map[string]bool{
	".py":   true,
	".sh":   true,
	".js":   true,
	".json": true,
}

// deviceHelperNames are the scripts that must never travel with a distribution
// runtime: they disable OPLUS HALs, drive charging/Wi-Fi, manage Android
// service.d, or perform device mutations (scheduled reboot, APK overwrite
// install, shell editor entry points). None of those are beginner defaults.
var deviceHelperNames = map[string]bool{
	"adb_tcp.sh":                       true,
	"charge_control.sh":                true,
	"disable_face_hal.sh":              true,
	"disable_fps_hal.sh":               true,
	"maa_post_boot_screen_off.sh":      true,
	"screen_backlight_off.sh":          true,
	"screen_on.sh":                     true,
	"ssh_watchdog.sh":                  true,
	"ssh_watchdog_worker.sh":           true,
	"start_debian.sh":                  true,
	"start_webui.sh":                   true,
	"wifi_enable_matrix_test.sh":       true,
	"wifi_settings_switch_selftest.sh": true,
	"wifi_ui_recover.sh":               true,
	"wifi_ui_recover_selftest.sh":      true,
	"wifi_watchdog.sh":                 true,
	"wifi_watchdog_worker.sh":          true,
	"maa-scheduled-reboot.sh":          true,
	"maa-edit.sh":                      true,
}

var (
	backupNamePattern     = regexp.MustCompile(`(?i)\.bak($|[._-])`)
	patchLeftoverPattern  = regexp.MustCompile(`(?i)(\.orig|\.rej|\.swp|\.tmp|\.log|\.pyc|\.pyo|~)$`)
	credentialNamePattern = regexp.MustCompile(`(?i)(^\.env|\.pem$|\.key$|\.p12$|\.pfx$|\.netrc$|^id_rsa|credential|secret|^tg\.txt$)`)
)

// projectFile is one shipped file with the hash the rootfs must match.
type projectFile struct {
	Name   string
	SHA256 string
	Size   int64
}

// ScanProjectDir validates the immutable project component of a bundle.
func ScanProjectDir(dir string) error {
	_, err := scanProjectDir(dir)
	return err
}

// scanProjectDir validates the immutable project component of a bundle. The
// project is flat (maa-run.sh and friends deploy straight into /root/maa-scripts),
// allowlisted to the four runtime file kinds, LF-only and free of credentials,
// device helpers and patch leftovers.
func scanProjectDir(dir string) ([]projectFile, error) {
	if err := checkAbsPath("project component", dir); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnsafeProject, err)
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return nil, fmt.Errorf("%w: project component %s: %v", ErrUnsafeProject, dir, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return nil, fmt.Errorf("%w: project component %s must be a real directory", ErrUnsafeProject, dir)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrUnsafeProject, dir, err)
	}
	defer root.Close()
	names, err := readDirNames(root, ".")
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrUnsafeProject, dir, err)
	}
	if len(names) > maxProjectFiles {
		return nil, fmt.Errorf("%w: project component has %d files, limit is %d", ErrUnsafeProject, len(names), maxProjectFiles)
	}
	files := make([]projectFile, 0, len(names))
	contents := map[string][]byte{}
	var total int64
	for _, name := range names {
		entry, err := root.Lstat(filepath.FromSlash(name))
		if err != nil {
			return nil, fmt.Errorf("%w: %s: %v", ErrUnsafeProject, name, err)
		}
		if entry.IsDir() {
			return nil, fmt.Errorf("%w: project component must be flat, %s is a directory", ErrUnsafeProject, name)
		}
		if entry.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("%w: project component must not contain links, %s is one", ErrUnsafeProject, name)
		}
		if !entry.Mode().IsRegular() {
			return nil, fmt.Errorf("%w: project component entry %s is not a regular file", ErrUnsafeProject, name)
		}
		if isForbiddenBundleName(name) {
			return nil, fmt.Errorf("%w: %s must not be part of a distributed project", ErrUnsafeProject, name)
		}
		if !allowedProjectExtensions[strings.ToLower(filepath.Ext(name))] {
			return nil, fmt.Errorf("%w: %s is not an allowlisted project file (.py, .sh, .js, .json)", ErrUnsafeProject, name)
		}
		data, err := readFileLimited(root, name, maxProjectFileBytes)
		if err != nil {
			return nil, fmt.Errorf("%w: %s: %v", ErrUnsafeProject, name, err)
		}
		if len(data) == 0 {
			return nil, fmt.Errorf("%w: %s is empty", ErrUnsafeProject, name)
		}
		if bytes.ContainsRune(data, '\r') {
			return nil, fmt.Errorf("%w: %s contains CR bytes; the project must be LF only", ErrUnsafeProject, name)
		}
		total += int64(len(data))
		if total > maxProjectBytes {
			return nil, fmt.Errorf("%w: project component exceeds %d bytes", ErrUnsafeProject, maxProjectBytes)
		}
		sum := sha256.Sum256(data)
		files = append(files, projectFile{Name: name, SHA256: hex.EncodeToString(sum[:]), Size: int64(len(data))})
		contents[name] = data
	}
	for _, required := range requiredProjectFiles {
		if _, ok := contents[required]; !ok {
			return nil, fmt.Errorf("%w: project component is missing %s", ErrUnsafeProject, required)
		}
	}
	if err := checkLockContract(contents[runnerScriptName]); err != nil {
		return nil, err
	}
	if err := checkWebUIBindContract(contents[webuiScriptName]); err != nil {
		return nil, err
	}
	return files, nil
}

// isForbiddenBundleName rejects credentials, patch leftovers and device helpers
// from a shipped project component.
func isForbiddenBundleName(name string) bool {
	if deviceHelperNames[name] {
		return true
	}
	lower := strings.ToLower(name)
	if lower == "backups" || lower == "backup" || lower == "logs" || lower == "__pycache__" || lower == "_legacy_flat" {
		return true
	}
	if backupNamePattern.MatchString(name) || patchLeftoverPattern.MatchString(name) || credentialNamePattern.MatchString(name) {
		return true
	}
	return false
}

// isForbiddenRuntimeName rejects the personal-device helpers from the activated
// rootfs as well, so a contaminated runtime cannot pass Check. Operator data
// such as /root/maa-scripts/tg.txt is allowed: it is not shipped, it is owned by
// the operator of that device.
func isForbiddenRuntimeName(name string) bool { return deviceHelperNames[name] }

// checkLockContract proves the shipped runner locks exactly the file this
// adapter locks. A different constant would silently defeat mutual exclusion.
func checkLockContract(runner []byte) error {
	seen := 0
	for _, line := range strings.Split(string(runner), "\n") {
		trimmed := strings.TrimSpace(line)
		if !strings.HasPrefix(trimmed, "LOCK_FILE=") {
			continue
		}
		seen++
		if trimmed != runnerLockConstant {
			return fmt.Errorf("%w: %s declares %q, the adapter locks %s", ErrLockContract, runnerScriptName, trimmed, runnerLockConstant)
		}
	}
	switch {
	case seen == 0:
		return fmt.Errorf("%w: %s does not declare %s", ErrLockContract, runnerScriptName, runnerLockConstant)
	case seen > 1:
		return fmt.Errorf("%w: %s declares LOCK_FILE %d times", ErrLockContract, runnerScriptName, seen)
	}
	return nil
}

// checkWebUIBindContract proves the shipped WebUI only listens on the manager's
// private upstream. This is the runtime half of the packaging rewrite; without
// it a world-readable WebUI would sit behind the manager unnoticed.
func checkWebUIBindContract(webui []byte) error {
	text := string(webui)
	if strings.Contains(text, webuiPublicAddress) {
		return fmt.Errorf("%w: %s still binds %s; the managed distribution must bind %s and be reached through the authenticated manager on 8088", ErrUnsafeProject, webuiScriptName, webuiPublicAddress, webuiListenAddress)
	}
	if got := strings.Count(text, webuiLoopbackAnchor); got != 1 {
		return fmt.Errorf("%w: %s must contain exactly one %q, found %d", ErrUnsafeProject, webuiScriptName, webuiLoopbackAnchor, got)
	}
	if got := strings.Count(text, webuiPortAnchor); got != 1 {
		return fmt.Errorf("%w: %s must contain exactly one %q, found %d", ErrUnsafeProject, webuiScriptName, webuiPortAnchor, got)
	}
	return nil
}
