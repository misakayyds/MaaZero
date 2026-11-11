//go:build linux

package driver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

type linuxSupervisor struct {
	mu           sync.Mutex
	cfg          Config
	webuiCmd     *exec.Cmd
	cronCmd      *exec.Cmd
	webuiDone    chan struct{}
	cronDone     chan struct{}
	webuiRunning bool
	cronRunning  bool
	stopping     bool
	webuiPGID    int
	cronPGID     int
}

func newDefaultSupervisor(cfg Config) Supervisor {
	return &linuxSupervisor{cfg: cfg}
}

func (s *linuxSupervisor) Start(ctx context.Context, cfg Config, bundleDir string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if s.webuiRunning || s.cronRunning || s.webuiCmd != nil || s.cronCmd != nil {
		return errors.New("supervisor already running")
	}
	s.cfg = cfg
	s.stopping = false

	// Launch WebUI inside chroot (-B / PYTHONDONTWRITEBYTECODE=1 keeps immutable bundle clean)
	pythonPath := "/usr/bin/python3"
	webuiScript := "/root/maa-scripts/maa-webui.py"
	webuiCmd := exec.Command(cfg.BusyboxPath, "chroot", cfg.RootfsPath, pythonPath, "-B", webuiScript)
	webuiCmd.Env = []string{
		"HOME=/root",
		"LANG=en_US.UTF-8",
		"PYTHONDONTWRITEBYTECODE=1",
		"PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"MAA_HOME=/root/maa-scripts",
		"MAA_CONFIG_DIR=/root/.config/maa",
		"MAA_DATA_DIR=/root/.local/share/maa",
		"MAA_STATE_DIR=/root/.local/state/maa",
		"MAA_CACHE_DIR=/root/.cache/maa",
		"MAA_LOG_DIR=/var/log/maa",
		"MAA_ADB_ADDR=127.0.0.1:5555",
		fmt.Sprintf("MAA_WEBUI_PORT=%d", cfg.WebUIPort),
	}
	var logFile *os.File
	if cfg.DataRoot != "" {
		logDir := filepath.Join(cfg.DataRoot, "logs")
		if err := os.MkdirAll(logDir, 0700); err == nil {
			if f, err := os.OpenFile(filepath.Join(logDir, "webui.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600); err == nil {
				logFile = f
				webuiCmd.Stdout = f
				webuiCmd.Stderr = f
			}
		}
	}
	// Process group isolation via setpgid
	webuiCmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid: true,
	}

	if err := webuiCmd.Start(); err != nil {
		if logFile != nil {
			_ = logFile.Close()
		}
		return fmt.Errorf("start webui: %w", err)
	}

	webuiDone := make(chan struct{})
	s.webuiCmd = webuiCmd
	s.webuiPGID = webuiCmd.Process.Pid
	s.webuiDone = webuiDone
	s.webuiRunning = true

	go func() {
		_ = webuiCmd.Wait()
		if logFile != nil {
			_ = logFile.Close()
		}
		s.mu.Lock()
		s.webuiRunning = false
		s.mu.Unlock()
		close(webuiDone)
	}()

	// Optionally launch cron inside chroot if present
	cronBin := filepath.Join(cfg.RootfsPath, "usr", "sbin", "cron")
	if _, err := os.Stat(cronBin); err == nil {
		cronCmd := exec.Command(cfg.BusyboxPath, "chroot", cfg.RootfsPath, "/usr/sbin/cron", "-f")
		cronCmd.Env = webuiCmd.Env
		cronCmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		if err := cronCmd.Start(); err == nil {
			cronDone := make(chan struct{})
			s.cronCmd = cronCmd
			s.cronPGID = cronCmd.Process.Pid
			s.cronDone = cronDone
			s.cronRunning = true
			go func() {
				_ = cronCmd.Wait()
				s.mu.Lock()
				s.cronRunning = false
				s.mu.Unlock()
				close(cronDone)
			}()
		}
	}

	return nil
}

func (s *linuxSupervisor) webuiAlive() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.webuiRunning
}

func (s *linuxSupervisor) WaitReady(ctx context.Context, timeout time.Duration) error {
	return waitReadyProbe(ctx, s.cfg.UpstreamURL, timeout, s.webuiAlive)
}

func (s *linuxSupervisor) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.webuiRunning || s.cronRunning
}

func (s *linuxSupervisor) Stop(ctx context.Context) error {
	s.mu.Lock()
	s.stopping = true
	webuiCmd := s.webuiCmd
	cronCmd := s.cronCmd
	webuiPGID := s.webuiPGID
	cronPGID := s.cronPGID
	webuiDone := s.webuiDone
	cronDone := s.cronDone
	s.mu.Unlock()

	var errs []error

	// Stop cron if running
	if cronCmd != nil && cronPGID > 0 {
		_ = syscall.Kill(-cronPGID, syscall.SIGTERM)
		if cronDone != nil {
			select {
			case <-cronDone:
			case <-time.After(2 * time.Second):
				_ = syscall.Kill(-cronPGID, syscall.SIGKILL)
				select {
				case <-cronDone:
				case <-time.After(1 * time.Second):
				case <-ctx.Done():
					errs = append(errs, ctx.Err())
				}
			case <-ctx.Done():
				errs = append(errs, ctx.Err())
			}
		}
	}

	// Stop WebUI process group: SIGTERM -> 5s timeout -> SIGKILL
	if webuiCmd != nil && webuiPGID > 0 {
		_ = syscall.Kill(-webuiPGID, syscall.SIGTERM)
		if webuiDone != nil {
			select {
			case <-webuiDone:
			case <-time.After(5 * time.Second):
				_ = syscall.Kill(-webuiPGID, syscall.SIGKILL)
				select {
				case <-webuiDone:
				case <-time.After(1 * time.Second):
				case <-ctx.Done():
					errs = append(errs, ctx.Err())
				}
			case <-ctx.Done():
				errs = append(errs, ctx.Err())
			}
		}
	}

	s.mu.Lock()
	s.webuiRunning = false
	s.cronRunning = false
	s.webuiCmd = nil
	s.cronCmd = nil
	s.webuiDone = nil
	s.cronDone = nil
	s.webuiPGID = 0
	s.cronPGID = 0
	s.mu.Unlock()

	return errors.Join(errs...)
}
