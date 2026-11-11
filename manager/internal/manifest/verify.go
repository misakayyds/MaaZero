package manifest

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var (
	versionPattern  = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$`)
	sha256Pattern   = regexp.MustCompile(`^[a-f0-9]{64}$`)
	idPattern       = regexp.MustCompile(`^[a-z][a-z0-9-]{1,63}$`)
	noticeIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{2,63}$`)
)

const maxManifestBytes = 1 << 20

func LoadAndVerify(payload, signature []byte, publicKey ed25519.PublicKey) (*ChannelManifest, error) {
	if len(payload) > maxManifestBytes {
		return nil, fmt.Errorf("manifest payload too large: %d bytes", len(payload))
	}
	if len(publicKey) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("signature public key has invalid length")
	}
	if len(signature) != ed25519.SignatureSize || !ed25519.Verify(publicKey, payload, signature) {
		return nil, fmt.Errorf("signature verification failed")
	}

	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	var manifest ChannelManifest
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("decode manifest: %w", err)
	}
	if err := requireJSONEOF(decoder); err != nil {
		return nil, err
	}
	if err := manifest.validate(); err != nil {
		return nil, err
	}
	return &manifest, nil
}

func (manifest *ChannelManifest) SelectArtifact(componentID string, device DeviceFacts) (Artifact, error) {
	for _, component := range manifest.Components {
		if component.ID != componentID {
			continue
		}
		for _, blocked := range manifest.BlockedVersions {
			if blocked.Component == component.ID && blocked.Version == component.Version {
				return Artifact{}, fmt.Errorf("component %q version %q is blocked: %s", component.ID, component.Version, blocked.Reason)
			}
		}
		if !contains(component.Compatibility.Architectures, device.Architecture) {
			return Artifact{}, fmt.Errorf("component %q does not support architecture %q", componentID, device.Architecture)
		}
		if device.AndroidAPI < component.Compatibility.AndroidAPI.Minimum || device.AndroidAPI > component.Compatibility.AndroidAPI.Maximum {
			return Artifact{}, fmt.Errorf("component %q does not support android api %d", componentID, device.AndroidAPI)
		}
		if !contains(component.Compatibility.RootManagers, device.RootManager) {
			return Artifact{}, fmt.Errorf("component %q does not support root manager %q", componentID, device.RootManager)
		}
		return component.Artifacts[0], nil
	}
	return Artifact{}, fmt.Errorf("component %q not found", componentID)
}

func requireJSONEOF(decoder *json.Decoder) error {
	var trailing any
	err := decoder.Decode(&trailing)
	if errors.Is(err, io.EOF) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("decode trailing manifest data: %w", err)
	}
	return fmt.Errorf("manifest contains trailing json value")
}

func (manifest *ChannelManifest) validate() error {
	if manifest.SchemaVersion != 1 {
		return fmt.Errorf("unsupported schema version %d", manifest.SchemaVersion)
	}
	if manifest.Sequence == 0 {
		return fmt.Errorf("manifest sequence must be positive")
	}
	if manifest.Channel != "stable" && manifest.Channel != "beta" {
		return fmt.Errorf("invalid channel %q", manifest.Channel)
	}
	if _, err := time.Parse(time.RFC3339, manifest.GeneratedAt); err != nil {
		return fmt.Errorf("invalid generated_at: %w", err)
	}
	if !versionPattern.MatchString(manifest.MinimumBootstrapVersion) {
		return fmt.Errorf("invalid minimum bootstrap version %q", manifest.MinimumBootstrapVersion)
	}
	if len(manifest.Components) == 0 {
		return fmt.Errorf("manifest has no components")
	}
	for _, notice := range manifest.Notices {
		if err := notice.validate(); err != nil {
			return err
		}
	}

	seenComponents := make(map[string]struct{}, len(manifest.Components))
	for index := range manifest.Components {
		component := &manifest.Components[index]
		if _, exists := seenComponents[component.ID]; exists {
			return fmt.Errorf("duplicate component id %q", component.ID)
		}
		seenComponents[component.ID] = struct{}{}
		if err := component.validate(); err != nil {
			return fmt.Errorf("component %q: %w", component.ID, err)
		}
	}

	for _, blocked := range manifest.BlockedVersions {
		if !idPattern.MatchString(blocked.Component) || !versionPattern.MatchString(blocked.Version) || strings.TrimSpace(blocked.Reason) == "" {
			return fmt.Errorf("invalid blocked version entry for %q", blocked.Component)
		}
	}
	return validateUpstreamPolicy(manifest.UpstreamPolicy)
}

func (notice Notice) validate() error {
	if !noticeIDPattern.MatchString(notice.ID) {
		return fmt.Errorf("invalid notice id %q", notice.ID)
	}
	if notice.Severity != "info" && notice.Severity != "warning" && notice.Severity != "critical" {
		return fmt.Errorf("invalid notice severity %q", notice.Severity)
	}
	if len(notice.Title) == 0 || len(notice.Title) > 120 {
		return fmt.Errorf("invalid notice title length")
	}
	if len(notice.Message) == 0 || len(notice.Message) > 1000 {
		return fmt.Errorf("invalid notice message length")
	}
	if notice.URL != "" {
		if err := validateHTTPSURL(notice.URL); err != nil {
			return fmt.Errorf("invalid notice url: %w", err)
		}
	}
	return nil
}

func (component *Component) validate() error {
	if !idPattern.MatchString(component.ID) {
		return fmt.Errorf("invalid id")
	}
	if component.Kind != "manager" && component.Kind != "project" && component.Kind != "runtime" {
		return fmt.Errorf("invalid kind %q", component.Kind)
	}
	if !versionPattern.MatchString(component.Version) {
		return fmt.Errorf("invalid version %q", component.Version)
	}
	if component.InstallMode != "replace-file" && component.InstallMode != "extract-versioned" {
		return fmt.Errorf("invalid install mode %q", component.InstallMode)
	}
	if err := component.Compatibility.validate(); err != nil {
		return err
	}
	if len(component.Artifacts) == 0 {
		return fmt.Errorf("no artifacts")
	}
	for index := range component.Artifacts {
		if err := component.Artifacts[index].validate(component.Version); err != nil {
			return fmt.Errorf("artifact %d: %w", index, err)
		}
	}
	if len(component.HealthChecks) == 0 {
		return fmt.Errorf("no health checks")
	}
	if duplicate, ok := firstDuplicate(component.HealthChecks); ok {
		return fmt.Errorf("duplicate health check %q", duplicate)
	}
	allowedHealthChecks := map[string]bool{
		"manager-api": true, "webui-static": true, "debian-exec": true,
		"maa-version": true, "config-parse": true,
	}
	for _, check := range component.HealthChecks {
		if !allowedHealthChecks[check] {
			return fmt.Errorf("invalid health check %q", check)
		}
	}
	return nil
}

func (compatibility Compatibility) validate() error {
	if len(compatibility.Architectures) == 0 {
		return fmt.Errorf("no architectures")
	}
	if duplicate, ok := firstDuplicate(compatibility.Architectures); ok {
		return fmt.Errorf("duplicate architecture %q", duplicate)
	}
	for _, architecture := range compatibility.Architectures {
		if architecture != "arm64-v8a" {
			return fmt.Errorf("unsupported architecture %q", architecture)
		}
	}
	if compatibility.AndroidAPI.Minimum < 23 || compatibility.AndroidAPI.Maximum < compatibility.AndroidAPI.Minimum {
		return fmt.Errorf("invalid android api range %d-%d", compatibility.AndroidAPI.Minimum, compatibility.AndroidAPI.Maximum)
	}
	if len(compatibility.RootManagers) == 0 {
		return fmt.Errorf("no root managers")
	}
	if duplicate, ok := firstDuplicate(compatibility.RootManagers); ok {
		return fmt.Errorf("duplicate root manager %q", duplicate)
	}
	for _, manager := range compatibility.RootManagers {
		if manager != "magisk" && manager != "kernelsu" && manager != "apatch" {
			return fmt.Errorf("unsupported root manager %q", manager)
		}
	}
	return nil
}

func (artifact Artifact) validate(version string) error {
	if err := validateImmutableHTTPSURL(artifact.URL, version); err != nil {
		return err
	}
	if duplicate, ok := firstDuplicate(artifact.Mirrors); ok {
		return fmt.Errorf("duplicate mirror %q", duplicate)
	}
	for _, mirror := range artifact.Mirrors {
		if err := validateImmutableHTTPSURL(mirror, version); err != nil {
			return fmt.Errorf("mirror: %w", err)
		}
	}
	if !sha256Pattern.MatchString(artifact.SHA256) {
		return fmt.Errorf("invalid sha256")
	}
	if artifact.Size <= 0 {
		return fmt.Errorf("invalid size %d", artifact.Size)
	}
	if strings.TrimSpace(artifact.MediaType) == "" {
		return fmt.Errorf("empty media type")
	}
	if artifact.Compression != "none" && artifact.Compression != "zip" && artifact.Compression != "tar.gz" && artifact.Compression != "tar.zst" {
		return fmt.Errorf("invalid compression %q", artifact.Compression)
	}
	if strings.TrimSpace(artifact.Provenance.Publisher) == "" {
		return fmt.Errorf("empty provenance publisher")
	}
	if err := validateHTTPSURL(artifact.Provenance.SourceURL); err != nil {
		return fmt.Errorf("invalid provenance source url: %w", err)
	}
	if err := validateHTTPSURL(artifact.Provenance.LicenseURL); err != nil {
		return fmt.Errorf("invalid provenance license url: %w", err)
	}
	return nil
}

func validateImmutableHTTPSURL(raw, version string) error {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return fmt.Errorf("invalid immutable https url %q", raw)
	}
	for _, segment := range strings.Split(strings.ToLower(strings.Trim(parsed.Path, "/")), "/") {
		if segment == "latest" || segment == "main" || segment == "master" || segment == "head" {
			return fmt.Errorf("invalid immutable https url %q", raw)
		}
	}
	if !strings.Contains(strings.ToLower(parsed.Path), strings.ToLower(version)) {
		return fmt.Errorf("invalid immutable https url %q: path does not contain version %q", raw, version)
	}
	return nil
}

func validateHTTPSURL(raw string) error {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil {
		return fmt.Errorf("invalid https url %q", raw)
	}
	return nil
}

func validateUpstreamPolicy(policies map[string]UpstreamPolicy) error {
	expectedStrategies := map[string]string{
		"maa-cli":      "maa-self-update",
		"maacore":      "maa-update",
		"maa-resource": "resource-sync",
	}
	if len(policies) != len(expectedStrategies) {
		return fmt.Errorf("upstream policy must contain maa-cli, maacore, and maa-resource")
	}
	for name, strategy := range expectedStrategies {
		policy, ok := policies[name]
		if !ok {
			return fmt.Errorf("upstream policy %q is missing", name)
		}
		if policy.Strategy != strategy {
			return fmt.Errorf("upstream policy %q has invalid strategy %q", name, policy.Strategy)
		}
		if policy.Channel != "stable" && policy.Channel != "beta" {
			return fmt.Errorf("upstream policy %q has invalid channel %q", name, policy.Channel)
		}
	}
	return nil
}

func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func firstDuplicate(values []string) (string, bool) {
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if _, exists := seen[value]; exists {
			return value, true
		}
		seen[value] = struct{}{}
	}
	return "", false
}
