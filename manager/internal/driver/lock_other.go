//go:build !linux && !windows

package driver

import (
	"errors"
	"os"
)

var errWouldBlock = errors.New("lock would block")

func tryLockFile(f *os.File) error {
	return nil
}

func unlockFile(f *os.File) error {
	return nil
}
