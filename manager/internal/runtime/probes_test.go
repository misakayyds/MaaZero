package runtime

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// The probe table is the complete set of commands this adapter may ever run.
// Pinning it here makes any addition a reviewed, deliberate change.
func TestProbeTableIsFixedAndGameFree(t *testing.T) {
	want := []probeSpec{
		{ID: "runner-syntax", Program: "/bin/bash", Args: []string{"-n", "/root/maa-scripts/maa-run.sh"}},
		{ID: "updater-syntax", Program: "/bin/bash", Args: []string{"-n", "/root/maa-scripts/maa-auto-update.sh"}},
		{ID: "maa-cli-version", Program: "maa", Args: []string{"version"}},
		{ID: "maa-config-dir", Program: "maa", Args: []string{"dir", "config"}},
	}
	if len(probeTable) != len(want) {
		t.Fatalf("probe table has %d entries %#v, want %d", len(probeTable), probeTable, len(want))
	}
	for i := range want {
		if probeTable[i].ID != want[i].ID || probeTable[i].Program != want[i].Program || !reflect.DeepEqual(probeTable[i].Args, want[i].Args) {
			t.Fatalf("probe %d = %#v, want %#v", i, probeTable[i], want[i])
		}
	}
	forbidden := []string{
		"arknights", "adb", "monkey", "am start", "reboot", "pkill",
		"startup-health", "daily", "service.d", "-c ", "sh -c", "curl", "wget",
	}
	for _, probe := range probeTable {
		joined := probe.Program + " " + strings.Join(probe.Args, " ")
		for _, bad := range forbidden {
			if strings.Contains(joined, bad) {
				t.Fatalf("probe %s runs a forbidden operation: %s", probe.ID, joined)
			}
		}
		for _, arg := range probe.Args {
			if strings.ContainsAny(arg, "|&;`$><\n") {
				t.Fatalf("probe %s passes a shell metacharacter: %q", probe.ID, arg)
			}
		}
	}
}

func TestProbeEnvIsFixed(t *testing.T) {
	want := []string{
		"PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		"HOME=/root",
		"LANG=en_US.UTF-8",
	}
	if !reflect.DeepEqual(probeEnv(), want) {
		t.Fatalf("probeEnv() = %#v, want %#v", probeEnv(), want)
	}
}

func TestBuildChrootArgv(t *testing.T) {
	busybox := "/data/adb/magisk/busybox"
	rootfs := "/data/local/maa-platform/runtime/rootfs/12.0.1"
	got := buildChrootArgv(busybox, rootfs, "maa", []string{"version"})
	want := []string{busybox, "chroot", rootfs, "maa", "version"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("buildChrootArgv = %#v, want %#v", got, want)
	}
	direct := buildChrootArgv(busybox, "", busybox, []string{"--list"})
	if !reflect.DeepEqual(direct, []string{busybox, "--list"}) {
		t.Fatalf("direct argv = %#v", direct)
	}
	args := []string{"version"}
	argv := buildChrootArgv(busybox, rootfs, "maa", args)
	args[0] = "mutated"
	if argv[len(argv)-1] != "version" {
		t.Fatalf("argv aliases caller args: %#v", argv)
	}
}

func TestSanitizeOutput(t *testing.T) {
	cases := []struct {
		name  string
		raw   string
		limit int
		want  string
	}{
		{"ansi", "\x1b[31merror\x1b[0m", 64, "error"},
		{"crlf", "line1\r\nline2\r\n", 64, "line1\nline2"},
		{"nul", "a\x00b", 64, "ab"},
		{"blank lines", "a\n\n\nb\n", 64, "a\nb"},
		{"spaces", "  padded  ", 64, "padded"},
		{"truncated", strings.Repeat("x", 20), 5, "xxxxx...(truncated)"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := sanitizeOutput([]byte(tc.raw), tc.limit); got != tc.want {
				t.Fatalf("sanitizeOutput(%q) = %q, want %q", tc.raw, got, tc.want)
			}
		})
	}
}

func TestJoinRootRelative(t *testing.T) {
	root := t.TempDir()
	got, err := joinRootRelative(root, "/root/.config/maa")
	if err != nil {
		t.Fatalf("joinRootRelative: %v", err)
	}
	want := filepath.Join(root, filepath.FromSlash("/root/.config/maa"))
	if got != want {
		t.Fatalf("joinRootRelative = %q, want %q", got, want)
	}
	for _, bad := range []string{"", "relative/path", "/root/\x00"} {
		if _, err := joinRootRelative(root, bad); err == nil {
			t.Fatalf("joinRootRelative(%q) unexpectedly succeeded", bad)
		}
	}
}
