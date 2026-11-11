// Package device performs the read-only device prerequisite probe used by the
// MAA Phone Manager module and daemon.
//
// A probe never writes to the device. It reads a fixed allowlist of Android
// system properties through a fixed `getprop` binary path, the fixed root
// manager marker paths, the caller uid, the SELinux mode and the free space of
// the platform data directory. Callers cannot inject property names, commands
// or paths, so the probe cannot be turned into an arbitrary command runner.
//
// Probe returns an error for host-level problems (a foreign host, a missing
// getprop, an unreadable property); Facts.Supported only checks the technical
// prerequisites this project needs before it will attempt an install. Passing
// those checks is NOT device acceptance: no root manager combination has been
// validated on real hardware yet, and this package claims no supported matrix.
// An unrecognised or ambiguous root manager never passes the gate.
package device

import (
	"errors"
	"fmt"
	"strconv"
	"strings"

	"maa-phone-manager/manager/internal/manifest"
)

const (
	// TargetArchitecture is the only Android ABI this project ships.
	TargetArchitecture = "arm64-v8a"
	// MinimumAndroidAPI is the lowest Android API the project will attempt:
	// Android 11. It is a technical prerequisite, not an acceptance claim.
	MinimumAndroidAPI = 30
	// DataRoot is the platform directory owned by the manager daemon.
	DataRoot = "/data/local/maa-platform"

	// RootManagerMagisk, RootManagerKernelSU and RootManagerAPatch are the only
	// root managers the probe can identify from fixed markers. Identifying one is
	// a prerequisite for trying an install, not a compatibility claim: none of
	// these managers has passed device acceptance yet. The empty string means the
	// probe could not identify exactly one manager.
	RootManagerMagisk   = "magisk"
	RootManagerKernelSU = "kernelsu"
	RootManagerAPatch   = "apatch"
)

// Fixed Android system properties. This allowlist is the whole probe surface.
const (
	propABI     = "ro.product.cpu.abi"
	propABIList = "ro.product.cpu.abilist"
	propSDK     = "ro.build.version.sdk"
	propRelease = "ro.build.version.release"
)

// requiredPaths must exist before the module scripts can run at all.
var requiredPaths = []string{"/system/bin/sh", "/system/bin/getprop"}

// rootManagerMarkers lists the fixed, read-only marker paths per root manager.
var rootManagerMarkers = []struct {
	manager string
	paths   []string
}{
	{RootManagerMagisk, []string{"/data/adb/magisk/magisk", "/data/adb/magisk"}},
	{RootManagerKernelSU, []string{"/data/adb/ksud", "/data/adb/ksu"}},
	{RootManagerAPatch, []string{"/data/adb/apd", "/data/adb/ap"}},
}

// ErrUnsupportedHost is returned by Probe when the current host is not Linux.
// Only ARM64 Android is targeted, so any other host is reported as unsupported
// instead of being guessed at.
var ErrUnsupportedHost = errors.New("device: probe requires a Linux Android host")

// ProbeSource supplies the read-only device data Probe inspects. The real
// implementation is the Android host itself; tests inject fixtures.
type ProbeSource interface {
	// Prop returns a fixed allowlisted Android system property. Unknown
	// properties are rejected by the real source.
	Prop(name string) (string, error)
	// UID reports the effective uid of the calling process.
	UID() int
	// PathExists reports whether a fixed path exists.
	PathExists(path string) bool
	// Writable reports write access to an existing path without modifying it.
	Writable(path string) bool
	// FreeBytes reports the free bytes of the filesystem holding path.
	FreeBytes(path string) (int64, error)
	// SELinuxMode reports "enforcing" or "permissive" when it is readable.
	SELinuxMode() (string, error)
}

// Facts is the read-only device description used for the prerequisite check.
type Facts struct {
	// DeviceFacts is the compatibility triple consumed by the signed manifest:
	// channel.SelectArtifact(componentID, facts.DeviceFacts). It holds the
	// selected architecture (arm64-v8a or the raw ABI), the Android API level and
	// the identified root manager.
	DeviceFacts manifest.DeviceFacts
	// PrimaryABI is the raw ro.product.cpu.abi value, kept for diagnostics.
	PrimaryABI string
	// AndroidRelease is ro.build.version.release, for display only.
	AndroidRelease string
	// RootManagerMarkers records every matched marker as "manager:path".
	RootManagerMarkers []string
	// UID is the effective uid of the probing process; the daemon needs 0.
	UID int
	// SELinuxMode is best-effort and never blocks a decision.
	SELinuxMode string
	// DataRoot is the platform data directory the daemon must manage.
	DataRoot string
	// DataRootAnchor is the existing path that was inspected for writability.
	DataRootAnchor string
	// DataRootWritable reports whether DataRootAnchor is writable.
	DataRootWritable bool
	// FreeBytes is the free space of DataRootAnchor.
	FreeBytes int64
	// MissingPaths lists required device paths that do not exist.
	MissingPaths []string
}

// Probe reads the current device. It is unsupported outside Linux.
func Probe() (Facts, error) {
	source, err := newSystemSource()
	if err != nil {
		return Facts{}, err
	}
	return ProbeWith(source)
}

// ProbeWith reads device facts from an explicit source and returns an error for
// host-level failures. The prerequisite gate itself is Facts.Supported.
func ProbeWith(source ProbeSource) (Facts, error) {
	if source == nil {
		return Facts{}, errors.New("device: probe source is nil")
	}

	primaryABI, err := source.Prop(propABI)
	if err != nil {
		return Facts{}, fmt.Errorf("device: read property %s: %w", propABI, err)
	}
	primaryABI = strings.TrimSpace(primaryABI)
	architecture := primaryABI
	if architecture != TargetArchitecture {
		abiList, listErr := source.Prop(propABIList)
		if listErr != nil {
			return Facts{}, fmt.Errorf("device: read property %s: %w", propABIList, listErr)
		}
		abiList = strings.TrimSpace(abiList)
		switch {
		case hasABI(abiList, TargetArchitecture):
			architecture = TargetArchitecture
		case architecture == "":
			architecture = firstABI(abiList)
		}
	}
	if architecture == "" {
		return Facts{}, fmt.Errorf("device: %s and %s are both empty", propABI, propABIList)
	}

	sdkValue, err := source.Prop(propSDK)
	if err != nil {
		return Facts{}, fmt.Errorf("device: read property %s: %w", propSDK, err)
	}
	sdkValue = strings.TrimSpace(sdkValue)
	androidAPI, err := strconv.Atoi(sdkValue)
	if err != nil || androidAPI <= 0 {
		return Facts{}, fmt.Errorf("device: invalid property %s value %q", propSDK, sdkValue)
	}

	facts := Facts{
		DeviceFacts: manifest.DeviceFacts{
			Architecture: architecture,
			AndroidAPI:   androidAPI,
		},
		PrimaryABI:     primaryABI,
		UID:            source.UID(),
		DataRoot:       DataRoot,
		DataRootAnchor: DataRoot,
	}
	if release, releaseErr := source.Prop(propRelease); releaseErr == nil {
		facts.AndroidRelease = strings.TrimSpace(release)
	}
	facts.DeviceFacts.RootManager, facts.RootManagerMarkers = detectRootManager(source)

	for _, path := range requiredPaths {
		if !source.PathExists(path) {
			facts.MissingPaths = append(facts.MissingPaths, path)
		}
	}

	// The data root may not exist yet; fall back to its existing parent.
	if !source.PathExists(facts.DataRootAnchor) {
		facts.DataRootAnchor = parentDir(facts.DataRootAnchor)
	}
	if source.PathExists(facts.DataRootAnchor) {
		facts.DataRootWritable = source.Writable(facts.DataRootAnchor)
	}
	facts.FreeBytes, err = source.FreeBytes(facts.DataRootAnchor)
	if err != nil {
		return Facts{}, fmt.Errorf("device: free space of %s: %w", facts.DataRootAnchor, err)
	}
	if mode, modeErr := source.SELinuxMode(); modeErr == nil {
		facts.SELinuxMode = mode
	}
	return facts, nil
}

// ManifestDeviceFacts returns the manifest compatibility triple. The canonical
// access is the Facts.DeviceFacts field; this thin alias keeps callers that use
// the function spelling working, and a test asserts both stay in sync.
func (facts Facts) ManifestDeviceFacts() manifest.DeviceFacts {
	return facts.DeviceFacts
}

// Supported reports whether the device meets the technical prerequisites for a
// first install attempt: arm64, Android 11+, exactly one identifiable root
// manager, root uid, a writable data root and the required device paths.
//
// It is a gate for trying, not an acceptance result. No root manager has passed
// device acceptance yet, so a nil error says nothing beyond these prerequisites
// being present on this device right now.
func (facts Facts) Supported() error {
	var reasons []string
	if facts.DeviceFacts.Architecture != TargetArchitecture {
		reasons = append(reasons, fmt.Sprintf("architecture %q is not one this project builds for (only %s)", facts.DeviceFacts.Architecture, TargetArchitecture))
	}
	if facts.DeviceFacts.AndroidAPI < MinimumAndroidAPI {
		reasons = append(reasons, fmt.Sprintf("android api %d is below the minimum %d", facts.DeviceFacts.AndroidAPI, MinimumAndroidAPI))
	}
	switch facts.DeviceFacts.RootManager {
	case RootManagerMagisk, RootManagerKernelSU, RootManagerAPatch:
	default:
		if len(facts.RootManagerMarkers) > 1 {
			reasons = append(reasons, fmt.Sprintf("root manager is ambiguous: matched %s", strings.Join(facts.RootManagerMarkers, ", ")))
		} else if len(facts.RootManagerMarkers) == 0 {
			reasons = append(reasons, "root manager could not be identified; the probe only recognises magisk, kernelsu and apatch markers")
		} else {
			reasons = append(reasons, fmt.Sprintf("root manager could not be identified from %s", strings.Join(facts.RootManagerMarkers, ", ")))
		}
	}
	if facts.UID != 0 {
		reasons = append(reasons, fmt.Sprintf("uid %d is not root", facts.UID))
	}
	if !facts.DataRootWritable {
		reasons = append(reasons, fmt.Sprintf("data root %s (checked %s) is not writable", facts.DataRoot, facts.DataRootAnchor))
	}
	if len(facts.MissingPaths) > 0 {
		reasons = append(reasons, fmt.Sprintf("missing required paths: %s", strings.Join(facts.MissingPaths, ", ")))
	}
	if len(reasons) == 0 {
		return nil
	}
	return fmt.Errorf("device does not meet the technical prerequisites: %s", strings.Join(reasons, "; "))
}

// HasFreeBytes reports whether the data filesystem can hold the required bytes.
func (facts Facts) HasFreeBytes(required int64) error {
	if facts.FreeBytes < required {
		return fmt.Errorf("free space %d bytes is less than the required %d bytes", facts.FreeBytes, required)
	}
	return nil
}

// detectRootManager returns the single known manager whose fixed marker paths
// exist, or "" when none or several managers match.
func detectRootManager(source ProbeSource) (string, []string) {
	var managers []string
	var markers []string
	for _, candidate := range rootManagerMarkers {
		var hits []string
		for _, path := range candidate.paths {
			if source.PathExists(path) {
				hits = append(hits, candidate.manager+":"+path)
			}
		}
		if len(hits) > 0 {
			managers = append(managers, candidate.manager)
			markers = append(markers, hits...)
		}
	}
	if len(managers) == 1 {
		return managers[0], markers
	}
	return "", markers
}

func hasABI(list, abi string) bool {
	for _, candidate := range splitABIList(list) {
		if candidate == abi {
			return true
		}
	}
	return false
}

func firstABI(list string) string {
	abis := splitABIList(list)
	if len(abis) == 0 {
		return ""
	}
	return abis[0]
}

func splitABIList(list string) []string {
	var abis []string
	for _, candidate := range strings.Split(list, ",") {
		if candidate = strings.TrimSpace(candidate); candidate != "" {
			abis = append(abis, candidate)
		}
	}
	return abis
}

func parentDir(path string) string {
	if index := strings.LastIndex(path, "/"); index > 0 {
		return path[:index]
	}
	return "/"
}
