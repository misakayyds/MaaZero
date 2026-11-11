//go:build linux

package driver

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

type defaultMounter struct{}

func newDefaultMounter() Mounter {
	return &defaultMounter{}
}

func toLinuxFlags(flags uintptr) uintptr {
	var out uintptr
	if flags&FlagBind != 0 {
		out |= syscall.MS_BIND
	}
	if flags&FlagRec != 0 {
		out |= syscall.MS_REC
	}
	if flags&FlagSlave != 0 {
		out |= syscall.MS_SLAVE
	}
	if flags&FlagNoSuid != 0 {
		out |= syscall.MS_NOSUID
	}
	if flags&FlagNoDev != 0 {
		out |= syscall.MS_NODEV
	}
	if flags&FlagNoExec != 0 {
		out |= syscall.MS_NOEXEC
	}
	if flags&FlagReadOnly != 0 {
		out |= syscall.MS_RDONLY
	}
	return out
}

func toLinuxUnmountFlags(flags int) int {
	var out int
	if flags&UnmountDetach != 0 {
		out |= syscall.MNT_DETACH
	}
	return out
}

func (m *defaultMounter) Mount(source, target, fstype string, flags uintptr, data string) error {
	linuxFlags := toLinuxFlags(flags)
	if err := syscall.Mount(source, target, fstype, linuxFlags, data); err != nil {
		return err
	}
	if flags&FlagBind != 0 && flags&FlagReadOnly != 0 {
		if err := syscall.Mount("", target, "", linuxFlags|syscall.MS_REMOUNT|syscall.MS_RDONLY, ""); err != nil {
			_ = syscall.Unmount(target, 0)
			return err
		}
	}
	return nil
}

func (m *defaultMounter) Unmount(target string, flags int) error {
	linuxFlags := toLinuxUnmountFlags(flags)
	return syscall.Unmount(target, linuxFlags)
}

func (m *defaultMounter) IsMounted(target string) (bool, error) {
	mounts, err := m.ListMountsUnder(target)
	if err != nil {
		return false, err
	}
	targetClean := filepath.Clean(target)
	for _, mnt := range mounts {
		if filepath.Clean(mnt) == targetClean {
			return true, nil
		}
	}
	return false, nil
}

func (m *defaultMounter) ListMountsUnder(prefix string) ([]string, error) {
	file, err := os.Open("/proc/mounts")
	if err != nil {
		return nil, err
	}
	defer file.Close()

	cleanPrefix := filepath.Clean(prefix)
	prefixWithSlash := cleanPrefix + string(filepath.Separator)

	var result []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		mnt := filepath.Clean(fields[1])
		if mnt == cleanPrefix || strings.HasPrefix(mnt, prefixWithSlash) {
			result = append(result, mnt)
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return result, nil
}
