// Package runtime validates one activated MAA runtime bundle and owns the task
// lock shared with the existing maa-run.sh runner.
//
// Scope (2026-09-12): this package is deliberately validation-only. It never
// starts Debian, the WebUI or a MAA task, never mounts anything, never touches
// ADB and never starts the game. The main controller owns bundle activation,
// the authenticated reverse proxy and the local API; this package only answers
// "is this bundle safe to run" (Check) and "give me the runner's task lock"
// (Acquire).
//
// Check fails closed: on a host that cannot enter the bundled rootfs it returns
// ErrUnsupportedHost instead of reporting a healthy runtime, and every probe it
// runs comes from the fixed probeTable with no caller-supplied command strings.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

const (
	bundleFileName = "bundle.json"

	// Component ids and kinds of the distribution bundle contract owned by the
	// main controller: components[]{id,version,path,kind}. The kind string is the
	// same generic vocabulary as the signed manifest (Kind "runtime"), so the
	// rootfs-shaped component is declared as "runtime", not "rootfs".
	rootfsComponentID  = "debian-rootfs"
	projectComponentID = "maa-phone-project"
	kindRuntime        = "runtime"
	kindProject        = "project"

	// maa-run.sh declares exactly this constant and locks it with flock. Because
	// the adapter addresses the same file through the activated rootfs, both sides
	// lock the same inode.
	runnerScriptName   = "maa-run.sh"
	runnerLockConstant = `LOCK_FILE="/tmp/maa-task.lock"`
	runnerLockRelPath  = "tmp/maa-task.lock"

	// /root/maa-scripts is the project's own deployment target inside maa-run.sh.
	projectRelDir = "root/maa-scripts"

	linuxHost = "linux"

	maxBundleBytes = 1 << 20
	maxPathBytes   = 4096

	lockPollInterval = 50 * time.Millisecond
)

// Sentinels for errors.Is; every failure in this package wraps one of them.
var (
	ErrUnsupportedHost = errors.New("runtime: unsupported host")
	ErrInvalidBundle   = errors.New("runtime: invalid bundle")
	ErrUnsafeProject   = errors.New("runtime: unsafe project component")
	ErrInvalidRootfs   = errors.New("runtime: invalid rootfs layout")
	ErrLockContract    = errors.New("runtime: task lock contract mismatch")
	ErrLockHeld        = errors.New("runtime: task lock is held")
)

// UnsupportedError reports a host that must not pretend the runtime is healthy.
type UnsupportedError struct {
	Op     string
	Host   string
	Reason string
}

func (e *UnsupportedError) Error() string {
	return fmt.Sprintf("%s: %s cannot %s: %s", ErrUnsupportedHost, e.Host, e.Op, e.Reason)
}

// Is makes errors.Is(err, ErrUnsupportedHost) work for every unsupported host.
func (e *UnsupportedError) Is(target error) bool { return target == ErrUnsupportedHost }

// Adapter binds one activated rootfs to the busybox used to enter it. Paths are
// always caller-supplied: there is no built-in device root.
type Adapter struct {
	rootfsPath  string
	busyboxPath string

	// hostOS and runner are seams for fixture tests that must not mount or chroot.
	// New always installs the real host and the real chroot runner.
	hostOS string
	runner commandRunner
}

// New validates the two paths the main controller resolved from the bundle. It
// does not touch the rootfs: existence is Check's job, so an adapter can be
// built before the runtime is materialized.
func New(rootfsPath, busyboxPath string) (*Adapter, error) {
	if err := checkAbsPath("rootfs", rootfsPath); err != nil {
		return nil, err
	}
	if err := checkAbsPath("busybox", busyboxPath); err != nil {
		return nil, err
	}
	if samePath(rootfsPath, busyboxPath) {
		return nil, errors.New("runtime: rootfs and busybox paths must differ")
	}
	return &Adapter{
		rootfsPath:  filepath.Clean(rootfsPath),
		busyboxPath: filepath.Clean(busyboxPath),
		hostOS:      runtime.GOOS,
		runner:      execRunner{busyboxPath: filepath.Clean(busyboxPath)},
	}, nil
}

// Check validates the bundle at bundleDir and runs the fixed, game-free rootfs
// probes. It reports an error for every condition it cannot verify.
func (a *Adapter) Check(ctx context.Context, bundleDir string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	bundle, err := loadBundle(bundleDir)
	if err != nil {
		return err
	}
	runtimeComponent, ok := bundle.componentByKind(kindRuntime)
	if !ok {
		return fmt.Errorf("%w: bundle %s has no %s component", ErrInvalidBundle, bundleDir, kindRuntime)
	}
	if !samePath(runtimeComponent.Path, a.rootfsPath) {
		return fmt.Errorf("%w: bundle runtime %s is not the activated rootfs %s", ErrInvalidBundle, runtimeComponent.Path, a.rootfsPath)
	}
	if err := a.requireProvisionedRuntime(); err != nil {
		return err
	}
	project, ok := bundle.componentByKind(kindProject)
	if !ok {
		return fmt.Errorf("%w: bundle %s has no %s component", ErrInvalidBundle, bundleDir, kindProject)
	}
	files, err := scanProjectDir(project.Path)
	if err != nil {
		return err
	}
	if err := a.checkRootfsLayout(files); err != nil {
		return err
	}
	if err := a.verifyBusybox(ctx); err != nil {
		return err
	}
	return a.runProbes(ctx)
}

// Acquire takes the runtime's own task lock, <rootfs>/tmp/maa-task.lock, which
// is the same inode maa-run.sh locks from inside the chroot. It waits until the
// lock is free or ctx is done. The returned release function is idempotent and
// never removes the lock file, matching flock's behaviour.
func (a *Adapter) Acquire(ctx context.Context) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := a.verifyLockContract(); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(a.rootfsPath)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidRootfs, a.rootfsPath, err)
	}
	defer root.Close()
	if err := requireRootDirectory(root, "tmp"); err != nil {
		return nil, err
	}
	file, err := root.OpenFile(filepath.FromSlash(runnerLockRelPath), os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidRootfs, runnerLockRelPath, err)
	}
	if err := lockContext(ctx, file); err != nil {
		file.Close()
		return nil, err
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			// Best effort: the lock disappears with the file descriptor anyway.
			_ = unlockFile(file)
			_ = file.Close()
		})
	}, nil
}

func (a *Adapter) verifyLockContract() error {
	root, err := os.OpenRoot(a.rootfsPath)
	if err != nil {
		return fmt.Errorf("%w: %s: %v", ErrInvalidRootfs, a.rootfsPath, err)
	}
	defer root.Close()
	data, err := readFileLimited(root, path.Join(projectRelDir, runnerScriptName), maxProjectFileBytes)
	if err != nil {
		return fmt.Errorf("%w: %s/%s is not readable in the activated rootfs: %v", ErrUnsafeProject, projectRelDir, runnerScriptName, err)
	}
	return checkLockContract(data)
}

func lockContext(ctx context.Context, file *os.File) error {
	for {
		err := tryLockFile(file)
		if err == nil {
			return nil
		}
		if !errors.Is(err, errWouldBlock) {
			return fmt.Errorf("%w: %s: %v", ErrLockHeld, runnerLockRelPath, err)
		}
		timer := time.NewTimer(lockPollInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return fmt.Errorf("%w: %s: %w", ErrLockHeld, runnerLockRelPath, ctx.Err())
		case <-timer.C:
		}
	}
}

// checkAbsPath rejects paths that could address something other than what the
// caller meant. Both host-native absolute paths and rootfs-absolute POSIX paths
// are accepted, because a bundle built for Android is inspected by this code on
// the device.
func checkAbsPath(what, p string) error {
	if strings.TrimSpace(p) == "" {
		return fmt.Errorf("runtime: %s path is empty", what)
	}
	if len(p) > maxPathBytes {
		return fmt.Errorf("runtime: %s path exceeds %d bytes", what, maxPathBytes)
	}
	if strings.ContainsRune(p, 0) {
		return fmt.Errorf("runtime: %s path contains NUL", what)
	}
	if !isAbsPath(p) {
		return fmt.Errorf("runtime: %s path %q must be absolute", what, p)
	}
	if cleanSlash(p) != filepath.ToSlash(p) {
		return fmt.Errorf("runtime: %s path %q must be clean (no trailing slash, no . or ..)", what, p)
	}
	return nil
}

func isAbsPath(p string) bool {
	if filepath.IsAbs(p) {
		return true
	}
	return strings.HasPrefix(p, "/") && !strings.HasPrefix(p, "//")
}

func cleanSlash(p string) string { return path.Clean(filepath.ToSlash(p)) }

func samePath(a, b string) bool {
	left, right := cleanSlash(a), cleanSlash(b)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(left, right)
	}
	return left == right
}

// joinRootRelative resolves an absolute path published by a program inside the
// rootfs (for example `maa dir config`) to a host path inside that rootfs.
func joinRootRelative(root, inside string) (string, error) {
	if inside == "" {
		return "", errors.New("empty path")
	}
	if strings.ContainsRune(inside, 0) {
		return "", errors.New("path contains NUL")
	}
	if !strings.HasPrefix(inside, "/") {
		return "", fmt.Errorf("%q must be an absolute path inside the rootfs", inside)
	}
	rel := strings.TrimPrefix(cleanSlash(inside), "/")
	if rel == "" {
		return "", errors.New("path resolves to the rootfs root")
	}
	return filepath.Join(root, filepath.FromSlash(rel)), nil
}
