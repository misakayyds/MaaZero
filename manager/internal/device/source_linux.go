//go:build linux

package device

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
)

// systemGetprop is the fixed Android property reader. The probe only accepts
// property names from the package allowlist, so the executed command set is
// fixed at build time.
const systemGetprop = "/system/bin/getprop"

const selinuxEnforcePath = "/sys/fs/selinux/enforce"

// writeAccess is W_OK from <unistd.h>; the probe asks the kernel for write
// permission instead of creating a test file.
const writeAccess = 2

type systemSource struct{}

func newSystemSource() (ProbeSource, error) { return systemSource{}, nil }

func (systemSource) Prop(name string) (string, error) {
	if !allowedProp(name) {
		return "", fmt.Errorf("device: property %q is not in the fixed probe allowlist", name)
	}
	getprop := systemGetprop
	if _, err := os.Stat(getprop); err != nil {
		resolved, lookErr := exec.LookPath("getprop")
		if lookErr != nil {
			return "", fmt.Errorf("device: getprop is unavailable: %w", lookErr)
		}
		getprop = resolved
	}
	output, err := exec.Command(getprop, name).Output()
	if err != nil {
		return "", fmt.Errorf("device: run %s %s: %w", getprop, name, err)
	}
	return strings.TrimSpace(string(output)), nil
}

func (systemSource) UID() int { return os.Geteuid() }

func (systemSource) PathExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func (systemSource) Writable(path string) bool {
	return syscall.Access(path, writeAccess) == nil
}

func (systemSource) FreeBytes(path string) (int64, error) {
	var stats syscall.Statfs_t
	if err := syscall.Statfs(path, &stats); err != nil {
		return 0, err
	}
	return int64(stats.Bavail) * int64(stats.Bsize), nil
}

func (systemSource) SELinuxMode() (string, error) {
	value, err := os.ReadFile(selinuxEnforcePath)
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(string(value)) == "1" {
		return "enforcing", nil
	}
	return "permissive", nil
}

func allowedProp(name string) bool {
	switch name {
	case propABI, propABIList, propSDK, propRelease:
		return true
	default:
		return false
	}
}
