// Package store owns version selection and durable update metadata, never user data.
// Callers must hold the runtime's task lock while switching or checking health.
package store

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
)

const stateFile = "transactions/state.json"

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,95}$`)
var hashPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type Versions struct {
	Current  string `json:"current"`
	Previous string `json:"previous"`
}

type sequence struct {
	Number uint64 `json:"number"`
	SHA256 string `json:"sha256"`
}

type diskState struct {
	Schema    int                 `json:"schema"`
	Versions  map[string]Versions `json:"versions"`
	Sequences map[string]sequence `json:"sequences"`
	Pending   *Record             `json:"pending"`
	Last      *Record             `json:"last"`
}

type Store struct {
	mu     sync.Mutex
	root   *os.Root
	lock   *os.File
	state  diskState
	closed bool
	fault  error
	// probing holds the transaction id whose health callback is currently running
	// outside the mutex. It keeps a second Commit on the same transaction, and any
	// new transaction, from overlapping that callback (which activates a release).
	probing string
}

func Open(path string) (_ *Store, err error) {
	if err = os.MkdirAll(path, 0700); err != nil {
		return nil, err
	}
	r, err := os.OpenRoot(path)
	if err != nil {
		return nil, err
	}
	s := &Store{root: r}
	defer func() {
		if err != nil {
			_ = s.Close()
		}
	}()
	for _, dir := range []string{"transactions", "releases", "data", "downloads", "logs"} {
		if err = s.safePath(dir); err != nil {
			return nil, err
		}
		if err = r.MkdirAll(dir, 0700); err != nil {
			return nil, err
		}
	}
	if err = syncDirectory(r, "."); err != nil {
		return nil, err
	}
	if err = s.safePath("transactions/writer.lock"); err != nil {
		return nil, err
	}
	s.lock, err = r.OpenFile("transactions/writer.lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = lockExclusive(s.lock); err != nil {
		return nil, fmt.Errorf("store already locked: %w", err)
	}
	if err = s.safePath(stateFile); err != nil {
		return nil, err
	}
	f, err := r.Open(stateFile)
	if errors.Is(err, os.ErrNotExist) {
		s.state = diskState{Schema: 1, Versions: map[string]Versions{}, Sequences: map[string]sequence{}}
		if err = s.save(s.state); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	} else {
		b, readErr := io.ReadAll(io.LimitReader(f, (1<<20)+1))
		closeErr := f.Close()
		if readErr != nil || closeErr != nil {
			return nil, errors.Join(readErr, closeErr)
		}
		if len(b) > 1<<20 {
			return nil, errors.New("store state exceeds limit")
		}
		d := json.NewDecoder(bytes.NewReader(b))
		d.DisallowUnknownFields()
		if err = d.Decode(&s.state); err != nil {
			return nil, err
		}
		if err = d.Decode(new(any)); err != io.EOF {
			return nil, errors.New("trailing store state")
		}
		if err = validate(s.state); err != nil {
			return nil, err
		}
	}
	if p := s.state.Pending; p != nil && (p.Stage == Switching || p.Stage == HealthCheck) {
		if err = s.rollback(p.ID, "interrupted_switch"); err != nil {
			return nil, err
		}
	}
	return s, nil
}

func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil
	}
	s.closed = true
	var err error
	if s.lock != nil {
		unlockExclusive(s.lock)
		err = s.lock.Close()
	}
	if s.root != nil {
		err = errors.Join(err, s.root.Close())
	}
	return err
}

func validName(v string) bool { return namePattern.MatchString(v) && !strings.HasSuffix(v, ".") }

// Reject links even when they remain inside the store (e.g. releases -> data).
// The data root must be private to the manager; os.Root also confines path races.
func (s *Store) safePath(path string) error {
	part := ""
	for _, p := range strings.Split(filepath.ToSlash(path), "/") {
		part = filepath.Join(part, p)
		info, err := s.root.Lstat(part)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return errors.New("store path contains link")
		}
	}
	return nil
}

func (s *Store) Root() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.root == nil {
		return ""
	}
	return s.root.Name()
}

func (s *Store) ReleasePath(component, version string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.ready(); err != nil {
		return "", err
	}
	if !validName(component) || !validName(version) {
		return "", errors.New("invalid release name")
	}
	p := filepath.Join("releases", component, version)
	if err := s.safePath(p); err != nil {
		return "", err
	}
	return filepath.Join(s.root.Name(), p), nil
}

func (s *Store) Versions() map[string]Versions {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string]Versions{}
	for k, v := range s.state.Versions {
		out[k] = v
	}
	return out
}

func (s *Store) Pending() *Record {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state.Pending == nil {
		return nil
	}
	p := *s.state.Pending
	return &p
}

// LastComponent returns the component of the most recent terminal transaction.
// Offline callers (a rollback that must not depend on the release source) use it
// to resolve the release directory without a manifest. It is empty when no
// transaction has completed yet, and a failed transaction reports the component
// it tried to switch.
func (s *Store) LastComponent() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.state.Last == nil {
		return ""
	}
	return s.state.Last.Component
}

// AcceptSequence must only be called after signature verification of the raw
// manifest bytes. Rolling back code never rolls back these high-water marks.
func (s *Store) AcceptSequence(channel string, number uint64, sha256 string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.ready(); err != nil {
		return err
	}
	if (channel != "stable" && channel != "beta") || number == 0 || !hashPattern.MatchString(sha256) {
		return errors.New("invalid sequence")
	}
	old := s.state.Sequences[channel]
	if number < old.Number || (number == old.Number && sha256 != old.SHA256) {
		return errors.New("manifest replay or equivocation")
	}
	next := s.copyState()
	next.Sequences[channel] = sequence{number, sha256}
	return s.save(next)
}

func (s *Store) ready() error {
	if s.closed {
		return errors.New("store closed")
	}
	if s.fault != nil {
		return fmt.Errorf("store needs reopen after I/O failure: %w", s.fault)
	}
	return nil
}

func (s *Store) copyState() diskState {
	n := s.state
	n.Versions = map[string]Versions{}
	for k, v := range s.state.Versions {
		n.Versions[k] = v
	}
	n.Sequences = map[string]sequence{}
	for k, v := range s.state.Sequences {
		n.Sequences[k] = v
	}
	if n.Pending != nil {
		p := *n.Pending
		n.Pending = &p
	}
	if n.Last != nil {
		p := *n.Last
		n.Last = &p
	}
	return n
}

func randomID() string {
	var b [16]byte
	// crypto/rand.Read does not return errors on supported Go versions.
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// A single record contains both logical pointers and the journal: no two-file
// current/previous consistency window. Runtime adapters resolve through Versions.
func (s *Store) save(next diskState) (err error) {
	if err = validate(next); err != nil {
		return err
	}
	b, err := json.Marshal(next)
	if err != nil {
		return err
	}
	if len(b) > 1<<20 {
		return errors.New("store state exceeds limit")
	}
	tmp := "transactions/state-" + randomID() + ".tmp"
	f, err := s.root.OpenFile(tmp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close(); _ = s.root.Remove(tmp) }()
	if _, err = f.Write(b); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = s.root.Rename(tmp, stateFile); err != nil {
		s.fault = err
		return err
	}
	if err = syncDirectory(s.root, "transactions"); err != nil {
		s.fault = err
		return err
	}
	s.state = next
	return nil
}

func validate(st diskState) error {
	if st.Schema != 1 || st.Versions == nil || st.Sequences == nil {
		return errors.New("invalid store schema")
	}
	for k, v := range st.Versions {
		if !validName(k) || (v.Current != "" && !validName(v.Current)) || (v.Previous != "" && !validName(v.Previous)) {
			return errors.New("invalid stored version")
		}
	}
	for k, v := range st.Sequences {
		if (k != "stable" && k != "beta") || v.Number == 0 || !hashPattern.MatchString(v.SHA256) {
			return errors.New("invalid stored sequence")
		}
	}
	if p := st.Pending; p != nil {
		if err := validateRecord(*p); err != nil {
			return err
		}
		if p.Stage == Committed || p.Stage == FailedSafe {
			return errors.New("terminal pending transaction")
		}
		want := p.Before
		if p.Stage == Switching || p.Stage == HealthCheck {
			want = Versions{p.To, p.Before.Current}
		}
		if st.Versions[p.Component] != want {
			return errors.New("inconsistent version pointers")
		}
	}
	if st.Last != nil {
		if err := validateRecord(*st.Last); err != nil {
			return err
		}
		if st.Last.Stage != Committed && st.Last.Stage != FailedSafe {
			return errors.New("nonterminal history")
		}
	}
	return nil
}
