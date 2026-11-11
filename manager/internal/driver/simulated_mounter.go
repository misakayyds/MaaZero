package driver

import (
	"errors"
	"path/filepath"
	"strings"
	"sync"
)

// SimulatedMounter tracks simulated mounts in-memory for testing and non-Linux hosts.
type SimulatedMounter struct {
	mu            sync.Mutex
	mounts        map[string]MountRecord
	history       []MountOp
	failOnMount   string
	failOnUnmount string
}

type MountRecord struct {
	Source string
	Target string
	FSType string
	Flags  uintptr
	Data   string
}

type MountOp struct {
	Op     string // "mount" or "unmount"
	Target string
	Flags  uintptr
}

func NewSimulatedMounter() *SimulatedMounter {
	return &SimulatedMounter{
		mounts: make(map[string]MountRecord),
	}
}

func (m *SimulatedMounter) SetFailOnMount(target string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.failOnMount = filepath.Clean(target)
}

func (m *SimulatedMounter) SetFailOnUnmount(target string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.failOnUnmount = filepath.Clean(target)
}

func (m *SimulatedMounter) Mount(source, target, fstype string, flags uintptr, data string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	cleanTarget := filepath.Clean(target)
	if m.failOnMount != "" && (cleanTarget == m.failOnMount || strings.Contains(cleanTarget, m.failOnMount)) {
		return errors.New("simulated mount failure on " + cleanTarget)
	}

	m.mounts[cleanTarget] = MountRecord{
		Source: source,
		Target: cleanTarget,
		FSType: fstype,
		Flags:  flags,
		Data:   data,
	}
	m.history = append(m.history, MountOp{Op: "mount", Target: cleanTarget, Flags: flags})
	return nil
}

func (m *SimulatedMounter) Unmount(target string, flags int) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	cleanTarget := filepath.Clean(target)
	if m.failOnUnmount != "" && (cleanTarget == m.failOnUnmount || strings.Contains(cleanTarget, m.failOnUnmount)) {
		return errors.New("simulated unmount failure on " + cleanTarget)
	}

	delete(m.mounts, cleanTarget)
	m.history = append(m.history, MountOp{Op: "unmount", Target: cleanTarget, Flags: uintptr(flags)})
	return nil
}

func (m *SimulatedMounter) IsMounted(target string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.mounts[filepath.Clean(target)]
	return ok, nil
}

func (m *SimulatedMounter) ListMountsUnder(prefix string) ([]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	cleanPrefix := filepath.Clean(prefix)
	prefixWithSlash := cleanPrefix + string(filepath.Separator)

	var res []string
	for target := range m.mounts {
		if target == cleanPrefix || strings.HasPrefix(target, prefixWithSlash) {
			res = append(res, target)
		}
	}
	return res, nil
}

func (m *SimulatedMounter) History() []MountOp {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]MountOp, len(m.history))
	copy(out, m.history)
	return out
}
