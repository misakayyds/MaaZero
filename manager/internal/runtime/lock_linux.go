//go:build linux

package runtime

import (
	"errors"
	"os"
	"syscall"
)

// errWouldBlock reports a lock that is currently held elsewhere, so the caller
// can wait instead of failing.
var errWouldBlock = syscall.EWOULDBLOCK

// tryLockFile takes the same exclusive flock(2) that `flock` in maa-run.sh takes
// on the same path, so the two cannot run at the same time.
func tryLockFile(f *os.File) error {
	for {
		err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return nil
		}
		if errors.Is(err, syscall.EINTR) {
			continue
		}
		return err
	}
}

func unlockFile(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
}
