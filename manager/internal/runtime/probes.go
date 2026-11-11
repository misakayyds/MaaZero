package runtime

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
)

const (
	probeOutputLimit = 64 << 10
	probeErrorLimit  = 512
)

// commandRequest is the only way this package runs anything. Rootfs empty means
// "run on the host" (used to inspect the provided busybox itself).
type commandRequest struct {
	Rootfs  string
	Program string
	Args    []string
	Env     []string
}

type commandResult struct {
	stdout   []byte
	stderr   []byte
	exitCode int
}

type commandRunner interface {
	Run(ctx context.Context, req commandRequest) (commandResult, error)
}

// probeSpec is one fixed, game-free check executed inside the rootfs.
type probeSpec struct {
	ID      string
	Program string
	Args    []string
	check   func(a *Adapter, res commandResult) error
}

// probeTable is the complete command surface of this adapter. Nothing here
// starts a game, touches ADB, restarts services or runs a MAA task.
var probeTable = []probeSpec{
	{
		ID:      "runner-syntax",
		Program: "/bin/bash",
		Args:    []string{"-n", "/root/maa-scripts/maa-run.sh"},
	},
	{
		ID:      "updater-syntax",
		Program: "/bin/bash",
		Args:    []string{"-n", "/root/maa-scripts/maa-auto-update.sh"},
	},
	{
		ID:      "maa-cli-version",
		Program: "maa",
		Args:    []string{"version"},
		check: func(_ *Adapter, res commandResult) error {
			if len(bytes.TrimSpace(res.stdout)) == 0 {
				return errors.New("maa version printed nothing; the rootfs must ship maa-cli and MaaCore")
			}
			return nil
		},
	},
	{
		ID:      "maa-config-dir",
		Program: "maa",
		Args:    []string{"dir", "config"},
		check: func(a *Adapter, res commandResult) error {
			return a.checkConfigDir(strings.TrimSpace(string(res.stdout)))
		},
	},
}

// probeEnv mirrors the environment maa-run.sh sets for itself: maa-cli lives in
// /root/.local/bin and resolves its config through HOME=/root.
func probeEnv() []string {
	return []string{
		"PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"HOME=/root",
		"LANG=en_US.UTF-8",
	}
}

func (a *Adapter) verifyBusybox(ctx context.Context) error {
	if a.hostOS != linuxHost {
		return &UnsupportedError{Op: "check the bundled runtime", Host: a.hostOS, Reason: "entering a Debian rootfs requires a linux host"}
	}
	if err := requireHostFile(a.busyboxPath); err != nil {
		return err
	}
	res, err := a.runner.Run(ctx, commandRequest{Program: a.busyboxPath, Args: []string{"--list"}})
	if err != nil {
		return fmt.Errorf("%w: busybox %s --list: %v", ErrInvalidRootfs, a.busyboxPath, err)
	}
	if res.exitCode != 0 {
		return fmt.Errorf("%w: busybox %s --list exited with %d: %s", ErrInvalidRootfs, a.busyboxPath, res.exitCode, res.message())
	}
	for _, line := range strings.Split(string(res.stdout), "\n") {
		if strings.TrimSpace(line) == "chroot" {
			return nil
		}
	}
	return fmt.Errorf("%w: busybox %s does not provide the chroot applet", ErrInvalidRootfs, a.busyboxPath)
}

func requireHostFile(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("%w: busybox %s: %v", ErrInvalidRootfs, path, err)
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%w: busybox %s must be a regular file", ErrInvalidRootfs, path)
	}
	return nil
}

func (a *Adapter) runProbes(ctx context.Context) error {
	env := probeEnv()
	for _, probe := range probeTable {
		if err := ctx.Err(); err != nil {
			return err
		}
		res, err := a.runner.Run(ctx, commandRequest{Rootfs: a.rootfsPath, Program: probe.Program, Args: probe.Args, Env: env})
		if err != nil {
			return fmt.Errorf("%w: probe %s: %v", ErrInvalidRootfs, probe.ID, err)
		}
		if res.exitCode != 0 {
			return fmt.Errorf("%w: probe %s exited with %d: %s", ErrInvalidRootfs, probe.ID, res.exitCode, res.message())
		}
		if probe.check != nil {
			if err := probe.check(a, res); err != nil {
				return fmt.Errorf("%w: probe %s: %v", ErrInvalidRootfs, probe.ID, err)
			}
		}
	}
	return nil
}

func (a *Adapter) checkConfigDir(dir string) error {
	if dir == "" {
		return errors.New("maa dir config printed nothing")
	}
	path, err := joinRootRelative(a.rootfsPath, dir)
	if err != nil {
		return fmt.Errorf("maa dir config printed %q: %v", dir, err)
	}
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("maa config dir %s is not present inside the rootfs: %v", dir, err)
	}
	if !info.IsDir() {
		return fmt.Errorf("maa config dir %s is not a directory inside the rootfs", dir)
	}
	return nil
}

// buildChrootArgv is the single place that turns a request into an argv.
func buildChrootArgv(busybox, rootfs, program string, args []string) []string {
	out := make([]string, 0, len(args)+4)
	if rootfs != "" {
		out = append(out, busybox, "chroot", rootfs)
	}
	out = append(out, program)
	return append(out, args...)
}

// execRunner is the production runner: busybox chroot, no shell, fixed env and
// bounded output buffers.
type execRunner struct {
	busyboxPath string
}

func (r execRunner) Run(ctx context.Context, req commandRequest) (commandResult, error) {
	argv := buildChrootArgv(r.busyboxPath, req.Rootfs, req.Program, req.Args)
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	if req.Env != nil {
		cmd.Env = req.Env
	} else {
		cmd.Env = []string{}
	}
	stdout := &limitedBuffer{limit: probeOutputLimit}
	stderr := &limitedBuffer{limit: probeOutputLimit}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	err := cmd.Run()
	res := commandResult{stdout: stdout.Bytes(), stderr: stderr.Bytes()}
	if err == nil {
		return res, nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		res.exitCode = exitErr.ExitCode()
		return res, nil
	}
	return res, err
}

type limitedBuffer struct {
	limit int
	buf   bytes.Buffer
}

func (b *limitedBuffer) Write(p []byte) (int, error) {
	if remaining := b.limit - b.buf.Len(); remaining > 0 {
		if len(p) > remaining {
			b.buf.Write(p[:remaining])
		} else {
			b.buf.Write(p)
		}
	}
	return len(p), nil
}

func (b *limitedBuffer) Bytes() []byte { return b.buf.Bytes() }

// message renders the bounded, sanitized output of a failed command.
func (r commandResult) message() string {
	combined := append(append([]byte{}, r.stdout...), r.stderr...)
	return sanitizeOutput(combined, probeErrorLimit)
}

var ansiPattern = regexp.MustCompile("\x1b\\[[0-9;?]*[A-Za-z]")

// sanitizeOutput keeps probe output usable in an error message: no escapes, no
// control bytes, no blank lines and a hard length limit.
func sanitizeOutput(raw []byte, limit int) string {
	text := ansiPattern.ReplaceAllString(string(raw), "")
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	lines := make([]string, 0, 8)
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(strings.Map(func(r rune) rune {
			if r < 0x20 && r != '\t' {
				return -1
			}
			return r
		}, line))
		if line != "" {
			lines = append(lines, line)
		}
	}
	out := strings.Join(lines, "\n")
	if limit > 0 && len(out) > limit {
		out = out[:limit] + "...(truncated)"
	}
	return out
}
