//go:build windows

package driver

import (
	"errors"
	"os"
	"syscall"
	"unsafe"
)

const (
	lockfileFailImmediately = 0x00000001
	lockfileExclusiveLock   = 0x00000002
)

var errWouldBlock = syscall.Errno(33) // ERROR_LOCK_VIOLATION

var (
	kernel32         = syscall.NewLazyDLL("kernel32.dll")
	procLockFileEx   = kernel32.NewProc("LockFileEx")
	procUnlockFileEx = kernel32.NewProc("UnlockFileEx")
)

func tryLockFile(f *os.File) error {
	var overlapped syscall.Overlapped
	ok, _, err := procLockFileEx.Call(uintptr(f.Fd()), lockfileExclusiveLock|lockfileFailImmediately, 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
	if ok != 0 {
		return nil
	}
	if errors.Is(err, errWouldBlock) {
		return errWouldBlock
	}
	return err
}

func unlockFile(f *os.File) error {
	var overlapped syscall.Overlapped
	ok, _, err := procUnlockFileEx.Call(uintptr(f.Fd()), 0, 1, 0, uintptr(unsafe.Pointer(&overlapped)))
	if ok != 0 {
		return nil
	}
	return err
}
