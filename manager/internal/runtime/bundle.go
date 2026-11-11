package runtime

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
)

const maxBundleComponents = 64

var (
	componentIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
	componentVersion   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`)
)

// Bundle mirrors the manifest the main controller writes next to an activated
// runtime: components[]{id,version,path,kind}. Paths are absolute and point at
// immutable component directories.
type Bundle struct {
	Schema     int         `json:"schema"`
	Components []Component `json:"components"`
}

type Component struct {
	ID      string `json:"id"`
	Version string `json:"version"`
	Path    string `json:"path"`
	Kind    string `json:"kind"`
}

func (b *Bundle) componentByKind(kind string) (Component, bool) {
	for _, c := range b.Components {
		if c.Kind == kind {
			return c, true
		}
	}
	return Component{}, false
}

// loadBundle reads bundle.json through an os.Root so a symlinked manifest
// cannot pull in anything outside the bundle directory.
func loadBundle(dir string) (*Bundle, error) {
	if err := checkAbsPath("bundle directory", dir); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidBundle, err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		return nil, fmt.Errorf("%w: bundle directory %s: %v", ErrInvalidBundle, dir, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%w: bundle path %s is not a directory", ErrInvalidBundle, dir)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidBundle, dir, err)
	}
	defer root.Close()
	f, err := root.Open(bundleFileName)
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidBundle, bundleFileName, err)
	}
	defer f.Close()
	stat, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidBundle, bundleFileName, err)
	}
	if !stat.Mode().IsRegular() {
		return nil, fmt.Errorf("%w: %s is not a regular file", ErrInvalidBundle, bundleFileName)
	}
	if stat.Size() > maxBundleBytes {
		return nil, fmt.Errorf("%w: %s exceeds %d bytes", ErrInvalidBundle, bundleFileName, maxBundleBytes)
	}
	data, err := io.ReadAll(io.LimitReader(f, maxBundleBytes+1))
	if err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidBundle, bundleFileName, err)
	}
	if len(data) > maxBundleBytes {
		return nil, fmt.Errorf("%w: %s exceeds %d bytes", ErrInvalidBundle, bundleFileName, maxBundleBytes)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var bundle Bundle
	if err := decoder.Decode(&bundle); err != nil {
		return nil, fmt.Errorf("%w: %s: %v", ErrInvalidBundle, bundleFileName, err)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return nil, fmt.Errorf("%w: %s has trailing content", ErrInvalidBundle, bundleFileName)
	}
	if err := validateBundle(&bundle); err != nil {
		return nil, err
	}
	return &bundle, nil
}

func validateBundle(b *Bundle) error {
	invalid := func(format string, args ...any) error {
		return fmt.Errorf("%w: %s", ErrInvalidBundle, fmt.Sprintf(format, args...))
	}
	if b.Schema != 0 && b.Schema != 1 {
		return invalid("unsupported bundle schema %d", b.Schema)
	}
	if len(b.Components) == 0 {
		return invalid("bundle declares no components")
	}
	if len(b.Components) > maxBundleComponents {
		return invalid("bundle declares %d components, limit is %d", len(b.Components), maxBundleComponents)
	}
	seenIDs := map[string]bool{}
	seenPaths := map[string]bool{}
	rootfs, project := 0, 0
	for _, c := range b.Components {
		if !componentIDPattern.MatchString(c.ID) {
			return invalid("component id %q is not a safe identifier", c.ID)
		}
		if seenIDs[c.ID] {
			return invalid("duplicate component id %q", c.ID)
		}
		seenIDs[c.ID] = true
		if !componentVersion.MatchString(c.Version) {
			return invalid("component %q has invalid version %q", c.ID, c.Version)
		}
		if err := checkAbsPath("component "+c.ID, c.Path); err != nil {
			return invalid("%v", err)
		}
		key := cleanSlash(c.Path)
		if seenPaths[key] {
			return invalid("duplicate component path %s", c.Path)
		}
		seenPaths[key] = true
		switch c.Kind {
		case kindRuntime:
			rootfs++
			if c.ID != rootfsComponentID {
				return invalid("%s component must use id %q, got %q", kindRuntime, rootfsComponentID, c.ID)
			}
		case kindProject:
			project++
			if c.ID != projectComponentID {
				return invalid("project component must use id %q, got %q", projectComponentID, c.ID)
			}
		default:
			return invalid("component %q has unsupported kind %q (this adapter version handles %q and %q)", c.ID, c.Kind, kindRuntime, kindProject)
		}
	}
	if rootfs != 1 {
		return invalid("bundle must declare exactly one %s component, got %d", kindRuntime, rootfs)
	}
	if project != 1 {
		return invalid("bundle must declare exactly one %s component, got %d", kindProject, project)
	}
	return nil
}

// readDirNames lists one directory of an os.Root, sorted for deterministic
// error messages and inventories.
func readDirNames(root *os.Root, dir string) ([]string, error) {
	d, err := root.Open(filepath.FromSlash(dir))
	if err != nil {
		return nil, err
	}
	defer d.Close()
	entries, err := d.ReadDir(-1)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	return names, nil
}

func readFileLimited(root *os.Root, rel string, limit int64) ([]byte, error) {
	f, err := root.Open(filepath.FromSlash(rel))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("%s exceeds %d bytes", rel, limit)
	}
	return data, nil
}
