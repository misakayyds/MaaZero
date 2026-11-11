package device

import (
	"errors"
	"runtime"
	"strings"
	"testing"

	"maa-phone-manager/manager/internal/manifest"
)

// fixtureSource injects the read-only data a real Android host would provide,
// so probe behaviour is testable without a device.
type fixtureSource struct {
	props    map[string]string
	propErrs map[string]error
	uid      int
	paths    map[string]bool
	writable map[string]bool
	free     int64
	freeErr  error
	selinux  string
}

func (source fixtureSource) Prop(name string) (string, error) {
	if err, ok := source.propErrs[name]; ok {
		return "", err
	}
	return source.props[name], nil
}

func (source fixtureSource) UID() int { return source.uid }

func (source fixtureSource) PathExists(path string) bool { return source.paths[path] }

func (source fixtureSource) Writable(path string) bool { return source.writable[path] }

func (source fixtureSource) FreeBytes(string) (int64, error) {
	if source.freeErr != nil {
		return 0, source.freeErr
	}
	return source.free, nil
}

func (source fixtureSource) SELinuxMode() (string, error) { return source.selinux, nil }

// androidDevice returns a fixture that satisfies every declared requirement:
// Android 11 (API 30) arm64 with Magisk, root uid and a writable /data/local.
func androidDevice() fixtureSource {
	return fixtureSource{
		props: map[string]string{
			propABI:     "arm64-v8a",
			propABIList: "arm64-v8a,armeabi-v7a,armeabi",
			propSDK:     "30",
			propRelease: "11",
		},
		uid: 0,
		paths: map[string]bool{
			"/data/adb/magisk":    true,
			"/data/local":         true,
			"/system/bin/sh":      true,
			"/system/bin/getprop": true,
		},
		writable: map[string]bool{"/data/local": true},
		free:     8 << 30,
		selinux:  "enforcing",
	}
}

func (source fixtureSource) with(mutate func(*fixtureSource)) fixtureSource {
	// Copy every map: fixtures derived from one base must not alias each other.
	props := make(map[string]string, len(source.props))
	for key, value := range source.props {
		props[key] = value
	}
	propErrs := make(map[string]error, len(source.propErrs))
	for key, value := range source.propErrs {
		propErrs[key] = value
	}
	paths := make(map[string]bool, len(source.paths))
	for key, value := range source.paths {
		paths[key] = value
	}
	writable := make(map[string]bool, len(source.writable))
	for key, value := range source.writable {
		writable[key] = value
	}
	source.props = props
	source.propErrs = propErrs
	source.paths = paths
	source.writable = writable
	mutate(&source)
	return source
}

func mustProbe(t *testing.T, source ProbeSource) Facts {
	t.Helper()
	facts, err := ProbeWith(source)
	if err != nil {
		t.Fatalf("ProbeWith: %v", err)
	}
	return facts
}

func TestProbeMagiskDevice(t *testing.T) {
	facts := mustProbe(t, androidDevice())

	if facts.DeviceFacts.Architecture != TargetArchitecture {
		t.Fatalf("architecture = %q", facts.DeviceFacts.Architecture)
	}
	if facts.DeviceFacts.AndroidAPI != 30 || facts.AndroidRelease != "11" {
		t.Fatalf("api/release = %d/%q", facts.DeviceFacts.AndroidAPI, facts.AndroidRelease)
	}
	if facts.DeviceFacts.RootManager != RootManagerMagisk {
		t.Fatalf("root manager = %q", facts.DeviceFacts.RootManager)
	}
	if facts.UID != 0 {
		t.Fatalf("uid = %d", facts.UID)
	}
	if facts.DataRoot != DataRoot || facts.DataRootAnchor != "/data/local" {
		t.Fatalf("data root = %q anchor = %q", facts.DataRoot, facts.DataRootAnchor)
	}
	if !facts.DataRootWritable || facts.FreeBytes != 8<<30 {
		t.Fatalf("writable = %v free = %d", facts.DataRootWritable, facts.FreeBytes)
	}
	if len(facts.MissingPaths) != 0 {
		t.Fatalf("missing paths = %v", facts.MissingPaths)
	}
	if facts.SELinuxMode != "enforcing" {
		t.Fatalf("selinux = %q", facts.SELinuxMode)
	}
	if err := facts.Supported(); err != nil {
		t.Fatalf("Supported: %v", err)
	}

	selected := facts.DeviceFacts
	if alias := facts.ManifestDeviceFacts(); alias != selected {
		t.Fatalf("ManifestDeviceFacts alias drifted from the DeviceFacts field: %+v", alias)
	}
	if selected.Architecture != TargetArchitecture || selected.AndroidAPI != 30 || selected.RootManager != RootManagerMagisk {
		t.Fatalf("manifest device facts = %+v", selected)
	}

	if len(facts.RootManagerMarkers) != 1 || facts.RootManagerMarkers[0] != "magisk:/data/adb/magisk" {
		t.Fatalf("markers = %v", facts.RootManagerMarkers)
	}
}

func TestRootManagerDetectionStaysConservative(t *testing.T) {
	magisk := androidDevice()
	cases := []struct {
		name   string
		source fixtureSource
		want   string
	}{
		{"magisk", magisk, RootManagerMagisk},
		{"kernelsu", magisk.with(func(s *fixtureSource) {
			s.paths["/data/adb/magisk"] = false
			s.paths["/data/adb/ksu"] = true
			s.paths["/data/adb/ksud"] = true
		}), RootManagerKernelSU},
		{"apatch", magisk.with(func(s *fixtureSource) {
			s.paths["/data/adb/magisk"] = false
			s.paths["/data/adb/ap"] = true
		}), RootManagerAPatch},
		{"no markers", magisk.with(func(s *fixtureSource) {
			s.paths["/data/adb/magisk"] = false
		}), ""},
		{"ambiguous markers", magisk.with(func(s *fixtureSource) {
			s.paths["/data/adb/ksu"] = true
		}), ""},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			facts := mustProbe(t, testCase.source)
			if facts.DeviceFacts.RootManager != testCase.want {
				t.Fatalf("root manager = %q, want %q", facts.DeviceFacts.RootManager, testCase.want)
			}
			err := facts.Supported()
			if testCase.want == "" {
				if err == nil {
					t.Fatal("unknown root manager reported as supported")
				}
				if !strings.Contains(err.Error(), "root manager") {
					t.Fatalf("error does not name the root manager: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("Supported: %v", err)
			}
		})
	}
}

func TestUnsupportedTargetsAreReported(t *testing.T) {
	cases := []struct {
		name   string
		source fixtureSource
		want   string
	}{
		{"unsupported abi", androidDevice().with(func(s *fixtureSource) {
			s.props[propABI] = "armeabi-v7a"
			s.props[propABIList] = "armeabi-v7a,armeabi"
		}), "architecture"},
		{"android api below floor", androidDevice().with(func(s *fixtureSource) {
			s.props[propSDK] = "29"
		}), "android api 29"},
		{"not running as root", androidDevice().with(func(s *fixtureSource) {
			s.uid = 1000
		}), "uid 1000"},
		{"data root not writable", androidDevice().with(func(s *fixtureSource) {
			s.writable["/data/local"] = false
		}), "not writable"},
		{"missing shell", androidDevice().with(func(s *fixtureSource) {
			s.paths["/system/bin/sh"] = false
		}), "/system/bin/sh"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			facts := mustProbe(t, testCase.source)
			err := facts.Supported()
			if err == nil {
				t.Fatal("unsupported device reported as supported")
			}
			if !strings.Contains(err.Error(), testCase.want) {
				t.Fatalf("error %q does not mention %q", err, testCase.want)
			}
			if testCase.name == "missing shell" && len(facts.MissingPaths) != 1 {
				t.Fatalf("missing paths = %v", facts.MissingPaths)
			}
		})
	}
}

func TestABIListFallbackAcceptsSixtyFourBitCapableDevice(t *testing.T) {
	source := androidDevice().with(func(s *fixtureSource) {
		s.props[propABI] = "armeabi-v7a"
		s.props[propABIList] = "armeabi-v7a,arm64-v8a"
	})
	facts := mustProbe(t, source)
	if facts.DeviceFacts.Architecture != TargetArchitecture {
		t.Fatalf("architecture = %q", facts.DeviceFacts.Architecture)
	}
	if facts.PrimaryABI != "armeabi-v7a" {
		t.Fatalf("primary abi = %q", facts.PrimaryABI)
	}
	if err := facts.Supported(); err != nil {
		t.Fatalf("Supported: %v", err)
	}
}

func TestProbeErrorsAreExplicit(t *testing.T) {
	if _, err := ProbeWith(nil); err == nil {
		t.Fatal("nil source accepted")
	}

	brokenProp := androidDevice().with(func(s *fixtureSource) {
		s.propErrs = map[string]error{propABI: errors.New("getprop unavailable")}
	})
	if _, err := ProbeWith(brokenProp); err == nil || !strings.Contains(err.Error(), propABI) {
		t.Fatalf("property failure = %v", err)
	}

	badSDK := androidDevice().with(func(s *fixtureSource) {
		s.props[propSDK] = "not-a-number"
	})
	if _, err := ProbeWith(badSDK); err == nil || !strings.Contains(err.Error(), propSDK) {
		t.Fatalf("invalid sdk = %v", err)
	}

	noAnchor := androidDevice().with(func(s *fixtureSource) {
		s.paths["/data/local"] = false
		s.freeErr = errors.New("statfs failed")
	})
	if _, err := ProbeWith(noAnchor); err == nil || !strings.Contains(err.Error(), "statfs failed") {
		t.Fatalf("free space failure = %v", err)
	}
}

func TestHasFreeBytes(t *testing.T) {
	facts := mustProbe(t, androidDevice())
	if err := facts.HasFreeBytes(4 << 30); err != nil {
		t.Fatalf("HasFreeBytes: %v", err)
	}
	err := facts.HasFreeBytes(9 << 30)
	if err == nil || !strings.Contains(err.Error(), "free space") {
		t.Fatalf("insufficient space = %v", err)
	}
}

func TestProbedFactsSelectManifestArtifact(t *testing.T) {
	facts := mustProbe(t, androidDevice())
	channel := &manifest.ChannelManifest{
		SchemaVersion: 1,
		Sequence:      1,
		Channel:       "stable",
		Components: []manifest.Component{{
			ID: "project", Kind: "project", Version: "1.0.0", Required: true,
			InstallMode: "extract-versioned",
			Compatibility: manifest.Compatibility{
				Architectures: []string{TargetArchitecture},
				AndroidAPI:    manifest.AndroidAPIRange{Minimum: MinimumAndroidAPI, Maximum: 36},
				RootManagers:  []string{RootManagerMagisk},
			},
			Artifacts: []manifest.Artifact{{
				URL: "https://example.invalid/1.0.0/project.tar.gz", Size: 1,
				SHA256: strings.Repeat("a", 64), MediaType: "application/gzip", Compression: "tar.gz",
			}},
			HealthChecks: []string{"manager-api"},
		}},
	}
	artifact, err := channel.SelectArtifact("project", facts.DeviceFacts)
	if err != nil {
		t.Fatalf("SelectArtifact: %v", err)
	}
	if !strings.Contains(artifact.URL, "1.0.0") {
		t.Fatalf("artifact = %+v", artifact)
	}

	stale := facts.DeviceFacts
	stale.AndroidAPI = 29
	if _, err := channel.SelectArtifact("project", stale); err == nil {
		t.Fatal("manifest accepted an unsupported android api")
	}
}

func TestProbeRefusesForeignHost(t *testing.T) {
	if runtime.GOOS == "linux" {
		t.Skip("linux host runs the real read-only probe")
	}
	if _, err := Probe(); !errors.Is(err, ErrUnsupportedHost) {
		t.Fatalf("Probe on %s = %v, want ErrUnsupportedHost", runtime.GOOS, err)
	}
}
