package runtime

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func newProjectDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range fixtureProjectFiles() {
		writeTestFile(t, filepath.Join(dir, name), content, 0o644)
	}
	return dir
}

func TestScanProjectDirAcceptsShippedLayout(t *testing.T) {
	dir := newProjectDir(t)
	files, err := scanProjectDir(dir)
	if err != nil {
		t.Fatalf("scanProjectDir: %v", err)
	}
	want := []string{
		"infrast-operator-requirements.json",
		"maa-auto-update.sh",
		"maa-run.sh",
		"maa-webui.py",
		"maa-weekly-editor.js",
	}
	if len(files) != len(want) {
		t.Fatalf("got %d files %#v, want %d", len(files), files, len(want))
	}
	for i, name := range want {
		if files[i].Name != name {
			t.Fatalf("file %d = %q, want %q", i, files[i].Name, name)
		}
	}
	sum := sha256.Sum256([]byte(fixtureProjectFiles()["maa-weekly-editor.js"]))
	if files[4].SHA256 != hex.EncodeToString(sum[:]) {
		t.Fatalf("sha256 = %q, want %q", files[4].SHA256, hex.EncodeToString(sum[:]))
	}
}

func TestScanProjectDirRejectsUnsafeLayouts(t *testing.T) {
	cases := []struct {
		name string
		file string
		body string
		want error
	}{
		{"credentials", "tg.txt", "token\n", ErrUnsafeProject},
		{"private key", "deploy.key", "-----BEGIN\n", ErrUnsafeProject},
		{"device helper", "ssh_watchdog.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"hal helper", "disable_face_hal.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"screen helper", "screen_backlight_off.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"launcher", "start_webui.sh", "#!/bin/sh\n", ErrUnsafeProject},
		{"reboot automation", "maa-scheduled-reboot.sh", "#!/bin/bash\n", ErrUnsafeProject},
		{"shell editor entry", "maa-edit.sh", "#!/bin/bash\n", ErrUnsafeProject},
		{"backup", "maa-webui.py.bak-20260912", "x\n", ErrUnsafeProject},
		{"backup dir marker", "backups", "x\n", ErrUnsafeProject},
		{"log", "maa-run.log", "x\n", ErrUnsafeProject},
		{"bytecode", "maa-webui.pyc", "x\n", ErrUnsafeProject},
		{"not allowlisted", "README.md", "x\n", ErrUnsafeProject},
		{"crlf", "maa-weekly-editor.js", "// x\r\n", ErrUnsafeProject},
		{"empty", "maa-weekly-editor.js", "", ErrUnsafeProject},
		{"public bind", "maa-webui.py", "PORT = 8089\nserver = WEB_SERVER_CLASS((\"0.0.0.0\", PORT), Handler)\n", ErrUnsafeProject},
		{"missing loopback bind", "maa-webui.py", "PORT = 8089\n", ErrUnsafeProject},
		{"wrong port", "maa-webui.py", webuiLoopbackAnchor + "\n", ErrUnsafeProject},
		{"missing lock constant", "maa-run.sh", "#!/bin/bash\n", ErrLockContract},
		{"changed lock constant", "maa-run.sh", `LOCK_FILE="/tmp/maa.lock"` + "\n", ErrLockContract},
		{"duplicate lock constant", "maa-run.sh", fixtureLockConstant + "\n" + `LOCK_FILE="/tmp/other.lock"` + "\n", ErrLockContract},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := newProjectDir(t)
			writeTestFile(t, filepath.Join(dir, tc.file), tc.body, 0o644)
			if _, err := scanProjectDir(dir); !errors.Is(err, tc.want) {
				t.Fatalf("scanProjectDir = %v, want %v", err, tc.want)
			}
		})
	}
}

func TestScanProjectDirRejectsMissingEntryPoints(t *testing.T) {
	for _, name := range []string{"maa-run.sh", "maa-auto-update.sh", "maa-webui.py"} {
		t.Run(name, func(t *testing.T) {
			dir := newProjectDir(t)
			if err := os.Remove(filepath.Join(dir, name)); err != nil {
				t.Fatal(err)
			}
			if _, err := scanProjectDir(dir); !errors.Is(err, ErrUnsafeProject) {
				t.Fatalf("scanProjectDir = %v, want ErrUnsafeProject", err)
			}
		})
	}
}

func TestScanProjectDirRejectsNestedEntries(t *testing.T) {
	dir := newProjectDir(t)
	writeTestFile(t, filepath.Join(dir, "nested", "helper.sh"), "#!/bin/sh\n", 0o644)
	if _, err := scanProjectDir(dir); !errors.Is(err, ErrUnsafeProject) {
		t.Fatalf("scanProjectDir = %v, want ErrUnsafeProject", err)
	}
}

func TestScanProjectDirRejectsSymlink(t *testing.T) {
	dir := newProjectDir(t)
	outside := filepath.Join(t.TempDir(), "outside.sh")
	writeTestFile(t, outside, "#!/bin/sh\n", 0o644)
	if err := os.Symlink(outside, filepath.Join(dir, "linked.sh")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if _, err := scanProjectDir(dir); !errors.Is(err, ErrUnsafeProject) {
		t.Fatalf("scanProjectDir = %v, want ErrUnsafeProject", err)
	}
}

func TestScanProjectDirRejectsOversizedFile(t *testing.T) {
	dir := newProjectDir(t)
	writeTestFile(t, filepath.Join(dir, "maa-weekly-editor.js"), strings.Repeat("x", maxProjectFileBytes+1), 0o644)
	if _, err := scanProjectDir(dir); !errors.Is(err, ErrUnsafeProject) {
		t.Fatalf("scanProjectDir = %v, want ErrUnsafeProject", err)
	}
}

func TestScanProjectDirRejectsMissingDirectory(t *testing.T) {
	if _, err := scanProjectDir(filepath.Join(t.TempDir(), "missing")); !errors.Is(err, ErrUnsafeProject) {
		t.Fatalf("scanProjectDir = %v, want ErrUnsafeProject", err)
	}
}
