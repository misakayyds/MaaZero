package runtime

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Fixtures. Nothing here mounts, chroots, touches ADB or starts a game: the
// adapter under test only ever sees a recorded command executor.
// ---------------------------------------------------------------------------

const (
	fixtureLockConstant = `LOCK_FILE="/tmp/maa-task.lock"`
	fixtureVersionOut   = "maa-cli 0.7.5\nMaaCore v6.17.5\n"
	fixtureConfigOut    = "/root/.config/maa\n"
)

// fixtureWebUI mirrors what the packaging step must produce: loopback bind and
// the manager's private upstream port.
func fixtureWebUI() string {
	return strings.Join([]string{
		"#!/usr/bin/env python3",
		webuiPortAnchor,
		webuiLoopbackAnchor,
		"",
	}, "\n")
}

func fixtureProjectFiles() map[string]string {
	return map[string]string{
		"maa-run.sh":                         "#!/bin/bash\n" + fixtureLockConstant + "\n",
		"maa-auto-update.sh":                 "#!/bin/bash\n",
		"maa-webui.py":                       fixtureWebUI(),
		"maa-weekly-editor.js":               "// weekly editor\n",
		"infrast-operator-requirements.json": "{\"schema\":1}\n",
	}
}

type fixture struct {
	dir     string
	bundle  string
	project string
	rootfs  string
	busybox string
	runner  *fakeRunner
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{
		dir:     t.TempDir(),
		busybox: "",
	}
	f.bundle = filepath.Join(f.dir, "bundle")
	f.project = filepath.Join(f.bundle, "project")
	f.rootfs = filepath.Join(f.bundle, "rootfs")
	f.busybox = filepath.Join(f.dir, "busybox")
	writeTestFile(t, filepath.Join(f.rootfs, "bin", "bash"), "#!/bin/sh\n", 0o755)
	writeTestFile(t, filepath.Join(f.rootfs, "usr", "bin", "python3"), "#!/bin/sh\n", 0o755)
	writeTestFile(t, filepath.Join(f.rootfs, "etc", "os-release"), "ID=debian\n", 0o644)
	writeTestFile(t, filepath.Join(f.rootfs, "root", ".config", "maa", "cli.toml"), "[core]\nchannel = \"Stable\"\n", 0o644)
	if err := os.MkdirAll(filepath.Join(f.rootfs, "tmp"), 0o755); err != nil {
		t.Fatalf("mkdir rootfs tmp: %v", err)
	}
	writeTestFile(t, f.busybox, "busybox\n", 0o755)
	for name, content := range fixtureProjectFiles() {
		writeTestFile(t, filepath.Join(f.project, name), content, 0o644)
		writeTestFile(t, filepath.Join(f.rootfs, projectRelDir, name), content, 0o644)
	}
	f.runner = &fakeRunner{results: f.cannedResults()}
	f.writeBundle(t, f.components()...)
	return f
}

func (f *fixture) probeKey(program string, args ...string) string {
	return requestKey(commandRequest{Rootfs: f.rootfs, Program: program, Args: args})
}

func (f *fixture) directKey(program string, args ...string) string {
	return requestKey(commandRequest{Program: program, Args: args})
}

func (f *fixture) cannedResults() map[string]commandResult {
	return map[string]commandResult{
		f.directKey(f.busybox, "--list"):                                      {stdout: []byte("chroot\nls\nsh\n")},
		f.probeKey("/bin/bash", "-n", "/root/maa-scripts/maa-run.sh"):         {},
		f.probeKey("/bin/bash", "-n", "/root/maa-scripts/maa-auto-update.sh"): {},
		f.probeKey("maa", "version"):                                          {stdout: []byte(fixtureVersionOut)},
		f.probeKey("maa", "dir", "config"):                                    {stdout: []byte(fixtureConfigOut)},
	}
}

func (f *fixture) components() []Component {
	return []Component{
		{ID: rootfsComponentID, Version: "12.0.1", Path: f.rootfs, Kind: kindRuntime},
		{ID: projectComponentID, Version: "0.1.0", Path: f.project, Kind: kindProject},
	}
}

func (f *fixture) writeBundle(t *testing.T, components ...Component) {
	t.Helper()
	data, err := json.Marshal(Bundle{Schema: 1, Components: components})
	if err != nil {
		t.Fatalf("marshal bundle: %v", err)
	}
	writeTestFile(t, filepath.Join(f.bundle, bundleFileName), string(data), 0o644)
}

func (f *fixture) adapter(t *testing.T, hostOS string) *Adapter {
	t.Helper()
	a, err := New(f.rootfs, f.busybox)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	a.hostOS = hostOS
	a.runner = f.runner
	return a
}

type fakeRunner struct {
	results map[string]commandResult
	fail    map[string]error
	calls   []commandRequest
}

func (f *fakeRunner) Run(ctx context.Context, req commandRequest) (commandResult, error) {
	if err := ctx.Err(); err != nil {
		return commandResult{}, err
	}
	f.calls = append(f.calls, req)
	key := requestKey(req)
	if err, ok := f.fail[key]; ok {
		return commandResult{}, err
	}
	res, ok := f.results[key]
	if !ok {
		return commandResult{}, fmt.Errorf("fixture has no canned result for %q", key)
	}
	return res, nil
}

func requestKey(req commandRequest) string {
	parts := append([]string{req.Rootfs, req.Program}, req.Args...)
	return strings.Join(parts, "\x00")
}

func writeTestFile(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// ---------------------------------------------------------------------------
// New
// ---------------------------------------------------------------------------

func TestNewRejectsUnusablePaths(t *testing.T) {
	busybox := filepath.Join(t.TempDir(), "busybox")
	cases := []struct {
		name    string
		rootfs  string
		busybox string
	}{
		{"empty rootfs", "", busybox},
		{"relative rootfs", "relative/rootfs", busybox},
		{"empty busybox", t.TempDir(), ""},
		{"relative busybox", t.TempDir(), "busybox"},
		{"nul byte", t.TempDir(), "C:\\tmp\\busybox\x00x"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := New(tc.rootfs, tc.busybox); err == nil {
				t.Fatalf("New(%q, %q) unexpectedly succeeded", tc.rootfs, tc.busybox)
			}
		})
	}
	if _, err := New("/data/local/maa-platform/runtime/rootfs", "/data/adb/magisk/busybox"); err != nil {
		t.Fatalf("New rejected absolute paths: %v", err)
	}
	if _, err := New("/data/local/maa-platform/runtime/rootfs", "/data/local/maa-platform/runtime/rootfs"); err == nil {
		t.Fatal("New accepted identical rootfs and busybox paths")
	}
}

// ---------------------------------------------------------------------------
// Check
// ---------------------------------------------------------------------------

func TestCheckFixtureBundlePasses(t *testing.T) {
	f := newFixture(t)
	a := f.adapter(t, "linux")
	if err := a.Check(context.Background(), f.bundle); err != nil {
		t.Fatalf("Check: %v", err)
	}
	wantCalls := []commandRequest{
		{Program: f.busybox, Args: []string{"--list"}},
		{Rootfs: f.rootfs, Program: "/bin/bash", Args: []string{"-n", "/root/maa-scripts/maa-run.sh"}, Env: probeEnv()},
		{Rootfs: f.rootfs, Program: "/bin/bash", Args: []string{"-n", "/root/maa-scripts/maa-auto-update.sh"}, Env: probeEnv()},
		{Rootfs: f.rootfs, Program: "maa", Args: []string{"version"}, Env: probeEnv()},
		{Rootfs: f.rootfs, Program: "maa", Args: []string{"dir", "config"}, Env: probeEnv()},
	}
	if !reflect.DeepEqual(f.runner.calls, wantCalls) {
		t.Fatalf("probe calls = %#v, want %#v", f.runner.calls, wantCalls)
	}
}

func TestCheckRejectsNonLinuxHost(t *testing.T) {
	if runtime.GOOS == "linux" {
		t.Skip("only reachable on a non-linux development host")
	}
	f := newFixture(t)
	a, err := New(f.rootfs, f.busybox)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	err = a.Check(context.Background(), f.bundle)
	if !errors.Is(err, ErrUnsupportedHost) {
		t.Fatalf("Check on %s = %v, want ErrUnsupportedHost", runtime.GOOS, err)
	}
	var unsupported *UnsupportedError
	if !errors.As(err, &unsupported) {
		t.Fatalf("expected *UnsupportedError, got %#v", err)
	}
	if unsupported.Host != runtime.GOOS {
		t.Fatalf("UnsupportedError.Host = %q, want %q", unsupported.Host, runtime.GOOS)
	}
}

func TestCheckRejectsBundleDirectoryProblems(t *testing.T) {
	f := newFixture(t)
	a := f.adapter(t, "linux")
	file := filepath.Join(f.dir, "not-a-dir")
	writeTestFile(t, file, "x", 0o644)
	cases := []struct {
		name string
		dir  string
	}{
		{"missing", filepath.Join(f.dir, "missing")},
		{"relative", "bundle"},
		{"file", file},
		{"empty", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := a.Check(context.Background(), tc.dir); !errors.Is(err, ErrInvalidBundle) {
				t.Fatalf("Check(%q) = %v, want ErrInvalidBundle", tc.dir, err)
			}
		})
	}
	if err := a.Check(context.Background(), t.TempDir()); !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("Check without bundle.json = %v, want ErrInvalidBundle", err)
	}
}

func TestCheckRejectsForeignRootfsComponent(t *testing.T) {
	f := newFixture(t)
	other := t.TempDir()
	f.writeBundle(t, Component{ID: rootfsComponentID, Version: "12.0.1", Path: other, Kind: kindRuntime},
		Component{ID: projectComponentID, Version: "0.1.0", Path: f.project, Kind: kindProject})
	a := f.adapter(t, "linux")
	err := a.Check(context.Background(), f.bundle)
	if !errors.Is(err, ErrInvalidBundle) {
		t.Fatalf("Check = %v, want ErrInvalidBundle", err)
	}
	if !strings.Contains(err.Error(), f.rootfs) {
		t.Fatalf("error %q should name the adapter rootfs", err)
	}
}

// The clean runtime/rootfs recipe does not exist yet. A bundle that points at a
// runtime path which was never provisioned must fail loudly instead of passing.
func TestCheckRejectsUnprovisionedRuntime(t *testing.T) {
	f := newFixture(t)
	missing := filepath.Join(f.dir, "runtime-not-provisioned")
	f.writeBundle(t, Component{ID: rootfsComponentID, Version: "12.0.1", Path: missing, Kind: kindRuntime},
		Component{ID: projectComponentID, Version: "0.1.0", Path: f.project, Kind: kindProject})
	a, err := New(missing, f.busybox)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	a.hostOS = "linux"
	a.runner = f.runner
	err = a.Check(context.Background(), f.bundle)
	if !errors.Is(err, ErrInvalidRootfs) {
		t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
	}
	if !strings.Contains(err.Error(), "recipe") {
		t.Fatalf("error %q should name the missing clean recipe as a blocker", err)
	}
}

func TestCheckRejectsUnsafeProjectComponent(t *testing.T) {
	cases := []struct {
		name    string
		file    string
		content string
		want    error
	}{
		{"credentials", "tg.txt", "123:token\n", ErrUnsafeProject},
		{"device helper", "wifi_watchdog.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"hal helper", "disable_fps_hal.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"launcher", "start_webui.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"reboot automation", "maa-scheduled-reboot.sh", "#!/bin/bash\n", ErrUnsafeProject},
		{"apk installer", "arknights-apk-update.sh", "#!/bin/bash\n", ErrUnsafeProject},
		{"shell editor entry", "maa-edit.sh", "#!/bin/bash\n", ErrUnsafeProject},
		{"backup copy", "maa-run.sh.bak-20260101", "#!/bin/bash\n", ErrUnsafeProject},
		{"compiled python", "maa-webui.pyc", "x", ErrUnsafeProject},
		{"public bind", "maa-webui.py", "PORT = 8089\nserver = WEB_SERVER_CLASS((\"0.0.0.0\", PORT), Handler)\n", ErrUnsafeProject},
		{"wrong port", "maa-webui.py", "PORT = 8088\n" + webuiLoopbackAnchor + "\n", ErrUnsafeProject},
		{"crlf", "maa-weekly-editor.js", "// a\r\n", ErrUnsafeProject},
		{"empty", "maa-weekly-editor.js", "", ErrUnsafeProject},
		{"not allowlisted", "notes.md", "x\n", ErrUnsafeProject},
		{"patch leftover", "maa-run.sh.orig", "x\n", ErrUnsafeProject},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t)
			writeTestFile(t, filepath.Join(f.project, tc.file), tc.content, 0o644)
			a := f.adapter(t, "linux")
			if err := a.Check(context.Background(), f.bundle); !errors.Is(err, tc.want) {
				t.Fatalf("Check = %v, want %v", err, tc.want)
			}
		})
	}
	t.Run("nested", func(t *testing.T) {
		f := newFixture(t)
		writeTestFile(t, filepath.Join(f.project, "nested", "helper.sh"), "#!/bin/sh\n", 0o644)
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
	t.Run("missing entry point", func(t *testing.T) {
		f := newFixture(t)
		if err := os.Remove(filepath.Join(f.project, "maa-auto-update.sh")); err != nil {
			t.Fatal(err)
		}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
	t.Run("lock contract", func(t *testing.T) {
		f := newFixture(t)
		writeTestFile(t, filepath.Join(f.project, "maa-run.sh"), "LOCK_FILE=\"/tmp/other.lock\"\n", 0o644)
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrLockContract) {
			t.Fatalf("Check = %v, want ErrLockContract", err)
		}
	})
}

func TestCheckRejectsBrokenRootfsLayout(t *testing.T) {
	for _, rel := range []string{filepath.Join("bin", "bash"), filepath.Join("usr", "bin", "python3"), filepath.Join("etc", "os-release")} {
		t.Run(rel, func(t *testing.T) {
			f := newFixture(t)
			if err := os.Remove(filepath.Join(f.rootfs, rel)); err != nil {
				t.Fatal(err)
			}
			a := f.adapter(t, "linux")
			if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrInvalidRootfs) {
				t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
			}
		})
	}
	t.Run("tmp", func(t *testing.T) {
		f := newFixture(t)
		if err := os.RemoveAll(filepath.Join(f.rootfs, "tmp")); err != nil {
			t.Fatal(err)
		}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrInvalidRootfs) {
			t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
		}
	})
	t.Run("project not materialized", func(t *testing.T) {
		f := newFixture(t)
		if err := os.RemoveAll(filepath.Join(f.rootfs, projectRelDir)); err != nil {
			t.Fatal(err)
		}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
	t.Run("materialized mismatch", func(t *testing.T) {
		f := newFixture(t)
		writeTestFile(t, filepath.Join(f.rootfs, projectRelDir, "maa-webui.py"), "PORT = 8089\n", 0o644)
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
	t.Run("device helper present", func(t *testing.T) {
		f := newFixture(t)
		writeTestFile(t, filepath.Join(f.rootfs, projectRelDir, "charge_control.sh"), "#!/bin/sh\n", 0o644)
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
	t.Run("device mutation present", func(t *testing.T) {
		f := newFixture(t)
		writeTestFile(t, filepath.Join(f.rootfs, projectRelDir, "arknights-apk-update.sh"), "#!/bin/bash\n", 0o644)
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
}

func TestCheckRejectsFailedProbes(t *testing.T) {
	t.Run("syntax error", func(t *testing.T) {
		f := newFixture(t)
		f.runner.results[f.probeKey("/bin/bash", "-n", "/root/maa-scripts/maa-run.sh")] = commandResult{exitCode: 1, stderr: []byte("syntax error near line 12")}
		a := f.adapter(t, "linux")
		err := a.Check(context.Background(), f.bundle)
		if !errors.Is(err, ErrInvalidRootfs) {
			t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
		}
		if !strings.Contains(err.Error(), "syntax error") {
			t.Fatalf("error %q should carry the probe output", err)
		}
	})
	t.Run("empty version", func(t *testing.T) {
		f := newFixture(t)
		f.runner.results[f.probeKey("maa", "version")] = commandResult{}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrInvalidRootfs) {
			t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
		}
	})
	t.Run("relative config dir", func(t *testing.T) {
		f := newFixture(t)
		f.runner.results[f.probeKey("maa", "dir", "config")] = commandResult{stdout: []byte("relative/config\n")}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrInvalidRootfs) {
			t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
		}
	})
	t.Run("missing config dir", func(t *testing.T) {
		f := newFixture(t)
		f.runner.results[f.probeKey("maa", "dir", "config")] = commandResult{stdout: []byte("/root/.config/missing\n")}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrInvalidRootfs) {
			t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
		}
	})
	t.Run("busybox without chroot", func(t *testing.T) {
		f := newFixture(t)
		f.runner.results[f.directKey(f.busybox, "--list")] = commandResult{stdout: []byte("ls\nsh\n")}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrInvalidRootfs) {
			t.Fatalf("Check = %v, want ErrInvalidRootfs", err)
		}
	})
	t.Run("runner missing in rootfs", func(t *testing.T) {
		f := newFixture(t)
		if err := os.Remove(filepath.Join(f.rootfs, projectRelDir, "maa-run.sh")); err != nil {
			t.Fatal(err)
		}
		a := f.adapter(t, "linux")
		if err := a.Check(context.Background(), f.bundle); !errors.Is(err, ErrUnsafeProject) {
			t.Fatalf("Check = %v, want ErrUnsafeProject", err)
		}
	})
}

// ---------------------------------------------------------------------------
// Acquire
// ---------------------------------------------------------------------------

func TestAcquireTakesRunnerTaskLock(t *testing.T) {
	f := newFixture(t)
	a := f.adapter(t, "linux")
	lockPath := filepath.Join(f.rootfs, "tmp", "maa-task.lock")
	release, err := a.Acquire(context.Background())
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	if _, err := os.Stat(lockPath); err != nil {
		t.Fatalf("lock file %s: %v", lockPath, err)
	}
	release()
	release() // idempotent
	again, err := a.Acquire(context.Background())
	if err != nil {
		t.Fatalf("re-Acquire after release: %v", err)
	}
	again()
}

func TestAcquireBlocksWhileHeld(t *testing.T) {
	f := newFixture(t)
	a := f.adapter(t, "linux")
	release, err := a.Acquire(context.Background())
	if err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	defer release()
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	start := time.Now()
	if _, err := a.Acquire(ctx); !errors.Is(err, ErrLockHeld) {
		t.Fatalf("second Acquire = %v, want ErrLockHeld", err)
	}
	if elapsed := time.Since(start); elapsed < 100*time.Millisecond {
		t.Fatalf("second Acquire returned after %s, expected it to wait", elapsed)
	}
}

func TestAcquireHonoursCancelledContext(t *testing.T) {
	f := newFixture(t)
	a := f.adapter(t, "linux")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := a.Acquire(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("Acquire = %v, want context.Canceled", err)
	}
}

func TestAcquireRejectsOtherLockContract(t *testing.T) {
	f := newFixture(t)
	writeTestFile(t, filepath.Join(f.rootfs, projectRelDir, "maa-run.sh"), "LOCK_FILE=\"/tmp/other.lock\"\n", 0o644)
	a := f.adapter(t, "linux")
	if _, err := a.Acquire(context.Background()); !errors.Is(err, ErrLockContract) {
		t.Fatalf("Acquire = %v, want ErrLockContract", err)
	}
	if _, err := os.Stat(filepath.Join(f.rootfs, "tmp", "maa-task.lock")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("lock file must not be created when the contract does not match: %v", err)
	}
}

func TestAcquireRejectsMissingTmpDir(t *testing.T) {
	f := newFixture(t)
	if err := os.RemoveAll(filepath.Join(f.rootfs, "tmp")); err != nil {
		t.Fatal(err)
	}
	a := f.adapter(t, "linux")
	if _, err := a.Acquire(context.Background()); !errors.Is(err, ErrInvalidRootfs) {
		t.Fatalf("Acquire = %v, want ErrInvalidRootfs", err)
	}
}
