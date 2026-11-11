package store

import (
	"os"
	"syscall"
	"unsafe"
)

var (
	kernel32DLL  = syscall.NewLazyDLL("kernel32.dll")
	lockFileEx   = kernel32DLL.NewProc("LockFileEx")
	unlockFileEx = kernel32DLL.NewProc("UnlockFileEx")
)

func lockExclusive(f *os.File) error {
	var overlapped syscall.Overlapped
	result, _, err := lockFileEx.Call(f.Fd(), 3, 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
	if result == 0 {
		return err
	}
	return nil
}

func unlockExclusive(f *os.File) {
	var overlapped syscall.Overlapped
	_, _, _ = unlockFileEx.Call(f.Fd(), 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
}

// Windows is a development/test host. File.Sync is exercised, but directory
// fsync and power-loss guarantees must be validated on the Linux/Android target.
func syncDirectory(_ *os.Root, _ string) error { return nil }
