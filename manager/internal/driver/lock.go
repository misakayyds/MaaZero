package driver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

var (
	ErrLockHeld = errors.New("driver: task lock is held")
)

// Locker coordinates exclusive task locking on live task inodes.
type Locker interface {
	Acquire(ctx context.Context, lockPath string) (func(), error)
}

type defaultLocker struct {
	pollInterval time.Duration
}

func newDefaultLocker() Locker {
	return &defaultLocker{pollInterval: 50 * time.Millisecond}
}

func (l *defaultLocker) Acquire(ctx context.Context, lockPath string) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	dir := filepath.Dir(lockPath)
	if err := os.MkdirAll(dir, 0777); err != nil {
		return nil, fmt.Errorf("create lock dir: %w", err)
	}
	file, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0644)
	if err != nil {
		return nil, fmt.Errorf("open lock file %s: %w", lockPath, err)
	}

	for {
		err := tryLockFile(file)
		if err == nil {
			break
		}
		if !errors.Is(err, errWouldBlock) {
			file.Close()
			return nil, fmt.Errorf("%w: %s: %v", ErrLockHeld, lockPath, err)
		}
		select {
		case <-ctx.Done():
			file.Close()
			return nil, fmt.Errorf("%w: %s: %w", ErrLockHeld, lockPath, ctx.Err())
		case <-time.After(l.pollInterval):
		}
	}

	var once sync.Once
	return func() {
		once.Do(func() {
			_ = unlockFile(file)
			_ = file.Close()
		})
	}, nil
}
