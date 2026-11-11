//go:build windows

package runtime

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

// errWouldBlock reports a lock that is currently held elsewhere, so the caller
// can wait instead of failing. 33 is ERROR_LOCK_VIOLATION, which the syscall
// package on this platform no longer exports.
var errWouldBlock = syscall.Errno(33)

var (
	kernel32         = syscall.NewLazyDLL("kernel32.dll")
	procLockFileEx   = kernel32.NewProc("LockFileEx")
	procUnlockFileEx = kernel32.NewProc("UnlockFileEx")
)

// tryLockFile takes an exclusive byte-range lock on the first byte of the task
// lock file. Production always runs on linux, where this path is flock(2); the
// Windows branch exists so the lock behaviour is exercised by local tests too.
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
