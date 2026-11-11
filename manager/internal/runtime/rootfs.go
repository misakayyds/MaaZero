package runtime

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"runtime"
)

// Facts the probes and the runner depend on inside a Debian rootfs.
const (
	rootfsBashPath      = "bin/bash"
	rootfsPython3Path   = "usr/bin/python3"
	rootfsOSReleasePath = "etc/os-release"
	rootfsDebianVersion = "etc/debian_version"
)

// checkRootfsLayout verifies the parts of the rootfs the fixed probes need
// before any of them run: an interpreter, a Debian marker, the lock directory
// and the project visible at its own /root/maa-scripts target.
// requireProvisionedRuntime refuses to continue when the activated runtime was
// never materialized. There is no clean rootfs/runtime recipe in this repository
// yet, so an absent runtime is an integration blocker that must be reported
// rather than skipped.
func (a *Adapter) requireProvisionedRuntime() error {
	info, err := os.Stat(a.rootfsPath)
	if err != nil {
		return fmt.Errorf("%w: runtime component %s is not provisioned: %v (the clean rootfs/runtime recipe is an open integration blocker; this adapter must not report health without it)", ErrInvalidRootfs, a.rootfsPath, err)
	}
	if !info.IsDir() {
		return fmt.Errorf("%w: runtime component %s is not a directory", ErrInvalidRootfs, a.rootfsPath)
	}
	return nil
}

func (a *Adapter) checkRootfsLayout(project []projectFile) error {
	root, err := os.OpenRoot(a.rootfsPath)
	if err != nil {
		return fmt.Errorf("%w: %s: %v", ErrInvalidRootfs, a.rootfsPath, err)
	}
	defer root.Close()
	if err := requireRootFile(root, rootfsBashPath, true); err != nil {
		return err
	}
	if err := requireRootFile(root, rootfsPython3Path, true); err != nil {
		return err
	}
	if err := requireOneOfRootFiles(root, rootfsOSReleasePath, rootfsDebianVersion); err != nil {
		return err
	}
	if err := requireRootDirectory(root, "tmp"); err != nil {
		return err
	}
	return verifyMaterializedProject(root, project)
}

func requireRootFile(root *os.Root, rel string, executable bool) error {
	info, err := root.Lstat(filepath.FromSlash(rel))
	if err != nil {
		return fmt.Errorf("%w: rootfs %s: %v", ErrInvalidRootfs, rel, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("%w: rootfs %s must not be a link", ErrInvalidRootfs, rel)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%w: rootfs %s must be a regular file", ErrInvalidRootfs, rel)
	}
	// Windows has no executable bit, so this check only means something on the
	// host the runtime actually runs on.
	if executable && runtime.GOOS == linuxHost && info.Mode().Perm()&0o111 == 0 {
		return fmt.Errorf("%w: rootfs %s is not executable", ErrInvalidRootfs, rel)
	}
	return nil
}

func requireOneOfRootFiles(root *os.Root, options ...string) error {
	var lastErr error
	for _, rel := range options {
		info, err := root.Lstat(filepath.FromSlash(rel))
		if err != nil {
			lastErr = err
			continue
		}
		if info.Mode().IsRegular() {
			return nil
		}
		lastErr = fmt.Errorf("%s is not a regular file", rel)
	}
	return fmt.Errorf("%w: rootfs has none of %v (%v)", ErrInvalidRootfs, options, lastErr)
}

func requireRootDirectory(root *os.Root, rel string) error {
	info, err := root.Lstat(filepath.FromSlash(rel))
	if err != nil {
		return fmt.Errorf("%w: rootfs %s: %v", ErrInvalidRootfs, rel, err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("%w: rootfs %s must not be a link", ErrInvalidRootfs, rel)
	}
	if !info.IsDir() {
		return fmt.Errorf("%w: rootfs %s must be a directory", ErrInvalidRootfs, rel)
	}
	return nil
}

// verifyMaterializedProject proves the project the manager activates is the one
// inside the rootfs. Check therefore fails instead of reporting health for a
// runtime that only shares the bundle's version string.
func verifyMaterializedProject(root *os.Root, project []projectFile) error {
	info, err := root.Lstat(filepath.FromSlash(projectRelDir))
	if err != nil {
		return fmt.Errorf("%w: the activated rootfs does not expose %s: %v (the main controller must materialize the bundle project there)", ErrUnsafeProject, projectRelDir, err)
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return fmt.Errorf("%w: %s in the activated rootfs must be a real directory", ErrUnsafeProject, projectRelDir)
	}
	names, err := readDirNames(root, projectRelDir)
	if err != nil {
		return fmt.Errorf("%w: %s: %v", ErrUnsafeProject, projectRelDir, err)
	}
	for _, name := range names {
		if isForbiddenRuntimeName(name) {
			return fmt.Errorf("%w: %s/%s is a personal-device helper and must not exist in a distribution runtime", ErrUnsafeProject, projectRelDir, name)
		}
	}
	for _, want := range project {
		rel := path.Join(projectRelDir, want.Name)
		data, err := readFileLimited(root, rel, maxProjectFileBytes)
		if err != nil {
			return fmt.Errorf("%w: %s is missing from the activated rootfs: %v", ErrUnsafeProject, rel, err)
		}
		sum := sha256.Sum256(data)
		if hex.EncodeToString(sum[:]) != want.SHA256 {
			return fmt.Errorf("%w: %s in the activated rootfs differs from the bundle project component", ErrUnsafeProject, rel)
		}
	}
	return nil
}
