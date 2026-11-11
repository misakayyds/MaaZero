package main

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestVersionAndRejectedCommands(t *testing.T) {
	var out bytes.Buffer
	if err := run([]string{"version"}, &out); err != nil {
		t.Fatal(err)
	}
	if out.String() != "0.1.0-preview\n" {
		t.Fatal(out.String())
	}
	for _, args := range [][]string{{}, {"shell", "id"}, {"serve", "--listen", "0.0.0.0:8088"}} {
		if err := run(args, &out); err == nil {
			t.Fatalf("accepted %v", args)
		}
	}
}

// fakeInfo lets the data-root decision be tested on hosts whose filesystem
// synthesizes permission bits.
type fakeInfo struct {
	mode os.FileMode
	dir  bool
}

func (f fakeInfo) Name() string       { return "maa-platform" }
func (f fakeInfo) Size() int64        { return 0 }
func (f fakeInfo) Mode() os.FileMode  { return f.mode }
func (f fakeInfo) ModTime() time.Time { return time.Time{} }
func (f fakeInfo) IsDir() bool        { return f.dir }
func (f fakeInfo) Sys() any           { return nil }

func TestDaemonVersionIsSingleSourced(t *testing.T) {
	var out bytes.Buffer
	if err := run([]string{"version"}, &out); err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(out.String()); got != daemonVersion() {
		t.Fatalf("cli reports %q but the daemon reports %q", got, daemonVersion())
	}
	if !strings.HasPrefix(daemonVersion(), "0.1.") || !strings.HasSuffix(daemonVersion(), "-preview") {
		t.Fatalf("packaged preview version drifted: %q", daemonVersion())
	}
}

func TestCreateDataRootDecisions(t *testing.T) {
	root := t.TempDir()
	for _, tc := range []struct {
		name    string
		info    os.FileInfo
		wantErr bool
	}{
		{"private", fakeInfo{mode: 0700, dir: true}, false},
		{"group readable", fakeInfo{mode: 0750, dir: true}, true},
		{"world readable", fakeInfo{mode: 0755, dir: true}, true},
		{"group writable", fakeInfo{mode: 0770, dir: true}, true},
		{"file", fakeInfo{mode: 0700}, true},
		{"missing", nil, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(root, strings.ReplaceAll(tc.name, " ", "-"))
			err := createDataRoot(path, func(string) (os.FileInfo, error) { return tc.info, nil })
			if tc.wantErr && err == nil {
				t.Fatal("insecure data root accepted")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("private data root rejected: %v", err)
			}
		})
	}
}

func TestPrepareDataRootFailsClosedOnAnExistingInsecureDirectory(t *testing.T) {
	root := t.TempDir()
	insecure := filepath.Join(root, "insecure")
	if err := os.Mkdir(insecure, 0755); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(insecure)
	if err != nil {
		t.Fatal(err)
	}
	if err := prepareDataRoot(insecure); err == nil {
		t.Fatal("insecure existing data root accepted")
	}
	after, err := os.Stat(insecure)
	if err != nil {
		t.Fatal(err)
	}
	if before.Mode() != after.Mode() {
		t.Fatalf("manager changed an existing directory mode: %v -> %v", before.Mode(), after.Mode())
	}
}

func TestPrepareDataRootCreatesANewPrivateDirectory(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("permission bits are synthesized on this host")
	}
	fresh := filepath.Join(t.TempDir(), "maa-platform")
	if err := prepareDataRoot(fresh); err != nil {
		t.Fatalf("new data root rejected: %v", err)
	}
	info, err := os.Stat(fresh)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		t.Fatalf("new data root is not private: %v %v", info, err)
	}
}
