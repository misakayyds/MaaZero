package driver

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// SimulatedSupervisor provides a realistic loopback HTTP service for testing on any OS.
type SimulatedSupervisor struct {
	mu            sync.Mutex
	cfg           Config
	running       bool
	server        *http.Server
	listener      net.Listener
	failStart     bool
	failReadiness bool
}

func NewSimulatedSupervisor(cfg Config) *SimulatedSupervisor {
	return &SimulatedSupervisor{cfg: cfg}
}

func (s *SimulatedSupervisor) SetFailStart(fail bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failStart = fail
}

func (s *SimulatedSupervisor) SetFailReadiness(fail bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failReadiness = fail
}

func (s *SimulatedSupervisor) Start(ctx context.Context, cfg Config, bundleDir string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.failStart {
		return errors.New("simulated supervisor start failed")
	}
	if s.running {
		return errors.New("supervisor already running")
	}
	s.cfg = cfg

	u, err := url.Parse(cfg.UpstreamURL)
	if err != nil {
		return err
	}
	host := u.Host
	if host == "" {
		host = "127.0.0.1:8089"
	}
	l, err := net.Listen("tcp", host)
	if err != nil {
		return err
	}
	s.listener = l

	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		fail := s.failReadiness
		s.mu.Unlock()
		if fail {
			http.Error(w, "simulated error", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok","simulated":true}`))
	})
	srv := &http.Server{Handler: mux}
	s.server = srv
	s.running = true

	go func() {
		_ = srv.Serve(l)
		s.mu.Lock()
		s.running = false
		s.mu.Unlock()
	}()

	return nil
}

func (s *SimulatedSupervisor) WaitReady(ctx context.Context, timeout time.Duration) error {
	s.mu.Lock()
	fail := s.failReadiness
	s.mu.Unlock()
	if fail {
		return errors.New("simulated readiness failure")
	}
	return waitReadyProbe(ctx, s.cfg.UpstreamURL, timeout, s.Running)
}

func (s *SimulatedSupervisor) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running
}

func (s *SimulatedSupervisor) Stop(ctx context.Context) error {
	s.mu.Lock()
	server := s.server
	listener := s.listener
	s.running = false
	s.server = nil
	s.listener = nil
	s.mu.Unlock()

	if server != nil {
		_ = server.Close()
	}
	if listener != nil {
		_ = listener.Close()
	}

	return nil
}
