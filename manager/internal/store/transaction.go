package store

import (
	"errors"
	"fmt"
	"path/filepath"
	"time"
)

type Stage string

const (
	Checking    Stage = "checking"
	Downloading Stage = "downloading"
	Verifying   Stage = "verifying"
	Staging     Stage = "staging"
	WaitingIdle Stage = "waiting_idle"
	Switching   Stage = "switching"
	HealthCheck Stage = "health_check"
	Committed   Stage = "committed"
	FailedSafe  Stage = "failed_safe"
)

type Record struct {
	ID        string   `json:"id"`
	Component string   `json:"component"`
	Before    Versions `json:"before"`
	To        string   `json:"to"`
	SHA256    string   `json:"sha256"`
	Stage     Stage    `json:"stage"`
	StartedAt string   `json:"started_at"`
	UpdatedAt string   `json:"updated_at"`
	Reason    string   `json:"reason,omitempty"`
}

type Transaction struct {
	store *Store
	id    string
}

func now() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func (s *Store) BeginUpdate(component, fromVersion, toVersion, sha256 string) (*Transaction, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.ready(); err != nil {
		return nil, err
	}
	if !validName(component) || !validName(toVersion) || !hashPattern.MatchString(sha256) {
		return nil, errors.New("invalid update")
	}
	if s.state.Pending != nil {
		return nil, errors.New("update already pending")
	}
	// A health callback activates a release outside the store mutex; starting a
	// new transaction while one is running could overlap two activations.
	if s.probing != "" {
		return nil, errors.New("health check in progress")
	}
	before := s.state.Versions[component]
	if before.Current != fromVersion || fromVersion == toVersion {
		return nil, errors.New("source version mismatch or unchanged target")
	}
	n := s.copyState()
	n.Pending = &Record{ID: randomID(), Component: component, Before: before, To: toVersion, SHA256: sha256, Stage: Checking, StartedAt: now(), UpdatedAt: now()}
	if err := s.save(n); err != nil {
		return nil, err
	}
	return &Transaction{s, n.Pending.ID}, nil
}

func (s *Store) Resume() (*Transaction, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.ready(); err != nil {
		return nil, err
	}
	if s.state.Pending == nil {
		return nil, errors.New("no pending update")
	}
	return &Transaction{s, s.state.Pending.ID}, nil
}

func (tx *Transaction) active() error {
	if err := tx.store.ready(); err != nil {
		return err
	}
	p := tx.store.state.Pending
	if p == nil || p.ID != tx.id {
		return errors.New("stale transaction")
	}
	return nil
}

func allowed(from, to Stage) bool {
	switch from {
	case Checking:
		return to == Downloading
	case Downloading:
		return to == Verifying
	case Verifying:
		return to == Staging
	case Staging:
		return to == WaitingIdle || to == Switching
	case WaitingIdle:
		return to == Switching
	case Switching:
		return to == HealthCheck
	}
	return false
}

// Advance records installer progress; it does not perform downloads or verify
// artifact bytes. Only the trusted installer may advance past Verifying.
func (tx *Transaction) Advance(stage Stage) error {
	s := tx.store
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := tx.active(); err != nil {
		return err
	}
	n := s.copyState()
	p := n.Pending
	if !allowed(p.Stage, stage) {
		return fmt.Errorf("illegal stage transition %s -> %s", p.Stage, stage)
	}
	if stage == Switching {
		path := filepath.Join("releases", p.Component, p.To)
		if err := s.safePath(path); err != nil {
			return err
		}
		info, err := s.root.Stat(path)
		if err != nil {
			return err
		}
		if !info.IsDir() {
			return errors.New("candidate is not a directory")
		}
		n.Versions[p.Component] = Versions{p.To, p.Before.Current}
	}
	p.Stage, p.UpdatedAt = stage, now()
	return s.save(n)
}

// Commit requires a real no-game health check. The callback runs without the
// store mutex: a slow runtime probe must never block status readers, and the
// durable journal already records the health_check stage, so a crash during the
// probe is recovered on the next Open. Serialization is preserved without the
// lock held across the probe: at most one probe runs at a time, BeginUpdate
// refuses while a pending record or a probe exists, and the result is
// re-validated afterwards, so a rollback that races the probe wins and nothing is
// committed. The caller must still stop its own workers before Store.Close.
func (tx *Transaction) Commit(health func() error) error {
	if health == nil {
		return errors.New("health check required")
	}
	s := tx.store
	s.mu.Lock()
	if err := tx.active(); err != nil {
		s.mu.Unlock()
		return err
	}
	if s.state.Pending.Stage != HealthCheck {
		s.mu.Unlock()
		return errors.New("health check required")
	}
	if s.probing != "" {
		s.mu.Unlock()
		return errors.New("health check already in progress")
	}
	s.probing = tx.id
	s.mu.Unlock()
	healthErr := health()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.probing = ""
	if err := tx.active(); err != nil {
		return err
	}
	if s.state.Pending.Stage != HealthCheck {
		return errors.New("transaction changed during health check")
	}
	if healthErr != nil {
		return errors.Join(fmt.Errorf("candidate health check failed: %w", healthErr), s.rollback(tx.id, "health_check_failed"))
	}
	n := s.copyState()
	n.Pending.Stage, n.Pending.UpdatedAt = Committed, now()
	n.Last, n.Pending = n.Pending, nil
	return s.save(n)
}

func (tx *Transaction) Rollback(reason string) error {
	s := tx.store
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := tx.active(); err != nil {
		return err
	}
	return s.rollback(tx.id, reason)
}

func (s *Store) rollback(id, reason string) error {
	if !validName(reason) {
		return errors.New("rollback requires a safe reason code")
	}
	if s.state.Pending == nil || s.state.Pending.ID != id {
		return errors.New("stale transaction")
	}
	n := s.copyState()
	p := n.Pending
	n.Versions[p.Component] = p.Before
	p.Stage, p.Reason, p.UpdatedAt = FailedSafe, reason, now()
	n.Last, n.Pending = p, nil
	return s.save(n)
}

func validateRecord(p Record) error {
	if len(p.ID) != 32 || !validName(p.ID) || !validName(p.Component) || !validName(p.To) || !hashPattern.MatchString(p.SHA256) {
		return errors.New("invalid transaction")
	}
	if (p.Before.Current != "" && !validName(p.Before.Current)) || (p.Before.Previous != "" && !validName(p.Before.Previous)) || p.To == p.Before.Current {
		return errors.New("invalid transaction source")
	}
	if _, err := time.Parse(time.RFC3339Nano, p.StartedAt); err != nil {
		return err
	}
	if _, err := time.Parse(time.RFC3339Nano, p.UpdatedAt); err != nil {
		return err
	}
	if p.Reason != "" && !validName(p.Reason) {
		return errors.New("invalid reason")
	}
	switch p.Stage {
	case Checking, Downloading, Verifying, Staging, WaitingIdle, Switching, HealthCheck, Committed, FailedSafe:
		return nil
	default:
		return errors.New("unknown transaction stage")
	}
}
