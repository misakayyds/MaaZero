package manifest

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadAndVerifyAcceptsValidSignedManifest(t *testing.T) {
	payload := signedPayload(t, validManifestMap(t), nil)
	got, err := LoadAndVerify(payload.payload, payload.signature, payload.publicKey)
	if err != nil {
		t.Fatalf("LoadAndVerify() error = %v", err)
	}
	if got.Channel != "stable" {
		t.Fatalf("Channel = %q, want stable", got.Channel)
	}
	if got.Sequence != 1 {
		t.Fatalf("Sequence = %d, want 1", got.Sequence)
	}
	if len(got.Components) != 1 || got.Components[0].ID != "maa-managerd" {
		t.Fatalf("Components = %#v, want maa-managerd", got.Components)
	}
}

func TestExampleManifestMatchesVerifier(t *testing.T) {
	payload, err := os.ReadFile(filepath.Join("..", "..", "..", "manifest", "example-manifest.json"))
	if err != nil {
		t.Fatalf("ReadFile(example-manifest.json) error = %v", err)
	}
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey() error = %v", err)
	}

	manifest, err := LoadAndVerify(payload, ed25519.Sign(privateKey, payload), publicKey)
	if err != nil {
		t.Fatalf("example manifest rejected: %v", err)
	}
	if manifest.Sequence != 1 || manifest.Channel != "stable" {
		t.Fatalf("example identity = sequence %d channel %q", manifest.Sequence, manifest.Channel)
	}
}

func TestGeneratedChannelManifestsMatchVerifier(t *testing.T) {
	paths := []string{
		filepath.Join("..", "..", "..", "manifest", "channels", "stable.json"),
		filepath.Join("..", "..", "..", "manifest", "channels", "beta.json"),
		filepath.Join("..", "..", "..", "dist", "channels", "stable.json"),
		filepath.Join("..", "..", "..", "dist", "channels", "beta.json"),
	}
	for _, manifestPath := range paths {
		payload, err := os.ReadFile(manifestPath)
		if err != nil {
			t.Fatalf("ReadFile(%s) error = %v", manifestPath, err)
		}
		publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatalf("GenerateKey() error = %v", err)
		}

		manifest, err := LoadAndVerify(payload, ed25519.Sign(privateKey, payload), publicKey)
		if err != nil {
			t.Fatalf("manifest at %s rejected: %v", manifestPath, err)
		}
		expectedChannel := "stable"
		if strings.HasSuffix(manifestPath, "beta.json") {
			expectedChannel = "beta"
		}
		if manifest.Sequence != 1 || manifest.Channel != expectedChannel {
			t.Fatalf("manifest at %s identity mismatch: sequence %d channel %q", manifestPath, manifest.Sequence, manifest.Channel)
		}
		if len(manifest.Components) != 3 {
			t.Fatalf("manifest at %s component count = %d, want 3", manifestPath, len(manifest.Components))
		}
	}
}

func TestLoadAndVerifyRejectsInvalidSignature(t *testing.T) {
	valid := validManifestMap(t)
	signed := signedPayload(t, valid, nil)
	signed.payload[0] ^= 1

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "signature")
}

func TestLoadAndVerifyRejectsOversizedPayload(t *testing.T) {
	manifest := validManifestMap(t)
	manifest["notices"] = []any{
		map[string]any{
			"id":       "oversized-notice",
			"severity": "info",
			"title":    "oversized",
			"message":  strings.Repeat("x", maxManifestBytes),
		},
	}
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "payload too large")
}

func TestLoadAndVerifyRejectsInvalidPublicKeyLength(t *testing.T) {
	signed := signedPayload(t, validManifestMap(t), nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, ed25519.PublicKey("short"))
	assertErrorContains(t, err, "public key")
}

func TestLoadAndVerifyRejectsUnknownField(t *testing.T) {
	manifest := validManifestMap(t)
	manifest["unexpected"] = true
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "unknown field")
}

func TestLoadAndVerifyRejectsTrailingJSONValue(t *testing.T) {
	signed := signedPayload(t, validManifestMap(t), nil)
	signed.payload = append(signed.payload, []byte(` {"schema_version":1}`)...)
	_, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey() error = %v", err)
	}
	signed.publicKey = privateKey.Public().(ed25519.PublicKey)
	signed.signature = ed25519.Sign(privateKey, signed.payload)

	_, err = LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "trailing json value")
}

func TestLoadAndVerifyRejectsDuplicateComponentID(t *testing.T) {
	manifest := validManifestMap(t)
	components := manifest["components"].([]any)
	components = append(components, cloneMap(t, components[0]))
	manifest["components"] = components
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "duplicate component")
}

func TestLoadAndVerifyRejectsInvalidNotice(t *testing.T) {
	manifest := validManifestMap(t)
	manifest["notices"] = []any{
		map[string]any{
			"id":       "release-notice",
			"severity": "execute-shell",
			"title":    "bad severity",
			"message":  "must be rejected",
		},
	}
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "notice severity")
}

func TestLoadAndVerifyRejectsInvalidSemanticVersion(t *testing.T) {
	manifest := validManifestMap(t)
	firstComponent(manifest)["version"] = "0.1.0-."
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, `invalid version "0.1.0-."`)
}

func TestLoadAndVerifyRejectsDuplicateListValues(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(map[string]any)
	}{
		{
			name: "architecture",
			mutate: func(manifest map[string]any) {
				compatibility := firstComponent(manifest)["compatibility"].(map[string]any)
				compatibility["architectures"] = []any{"arm64-v8a", "arm64-v8a"}
			},
		},
		{
			name: "root manager",
			mutate: func(manifest map[string]any) {
				compatibility := firstComponent(manifest)["compatibility"].(map[string]any)
				compatibility["root_managers"] = []any{"magisk", "magisk"}
			},
		},
		{
			name: "mirror",
			mutate: func(manifest map[string]any) {
				firstArtifact(manifest)["mirrors"] = []any{
					"https://mirror.example.invalid/v0.1.0/manager",
					"https://mirror.example.invalid/v0.1.0/manager",
				}
			},
		},
		{
			name: "health check",
			mutate: func(manifest map[string]any) {
				firstComponent(manifest)["health_checks"] = []any{"manager-api", "manager-api"}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			manifest := validManifestMap(t)
			tt.mutate(manifest)
			signed := signedPayload(t, manifest, nil)

			_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
			assertErrorContains(t, err, "duplicate")
		})
	}
}

func TestLoadAndVerifyRejectsInvalidSHA256(t *testing.T) {
	manifest := validManifestMap(t)
	artifact := firstArtifact(manifest)
	artifact["sha256"] = "ABC123"
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "sha256")
}

func TestLoadAndVerifyRejectsInvalidAndroidAPIRange(t *testing.T) {
	manifest := validManifestMap(t)
	compatibility := firstComponent(manifest)["compatibility"].(map[string]any)
	compatibility["android_api"] = map[string]any{"minimum": 35, "maximum": 30}
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "android api")
}

func TestLoadAndVerifyRejectsNonPositiveSequence(t *testing.T) {
	manifest := validManifestMap(t)
	manifest["sequence"] = 0
	signed := signedPayload(t, manifest, nil)

	_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	assertErrorContains(t, err, "sequence")
}

func TestLoadAndVerifyRejectsMutableArtifactURLs(t *testing.T) {
	tests := []string{
		"https://github.com/example/project/releases/latest/download/manager",
		"https://raw.githubusercontent.com/example/project/main/manager",
		"https://downloads.example.invalid/manager-v1.0.0?token=temporary",
		"https://downloads.example.invalid/manager",
	}

	for _, artifactURL := range tests {
		t.Run(artifactURL, func(t *testing.T) {
			manifest := validManifestMap(t)
			firstArtifact(manifest)["url"] = artifactURL
			signed := signedPayload(t, manifest, nil)

			_, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
			assertErrorContains(t, err, "immutable https url")
		})
	}
}

func TestSelectArtifactReturnsCompatibleArtifact(t *testing.T) {
	signed := signedPayload(t, validManifestMap(t), nil)
	manifest, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	if err != nil {
		t.Fatalf("LoadAndVerify() error = %v", err)
	}

	artifact, err := manifest.SelectArtifact("maa-managerd", DeviceFacts{
		Architecture: "arm64-v8a",
		AndroidAPI:   30,
		RootManager:  "magisk",
	})
	if err != nil {
		t.Fatalf("SelectArtifact() error = %v", err)
	}
	if artifact.URL != "https://github.com/example/project/releases/download/v0.1.0/maa-managerd-linux-arm64" {
		t.Fatalf("URL = %q", artifact.URL)
	}
}

func TestSelectArtifactRejectsUnsupportedDevice(t *testing.T) {
	signed := signedPayload(t, validManifestMap(t), nil)
	manifest, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	if err != nil {
		t.Fatalf("LoadAndVerify() error = %v", err)
	}

	tests := []struct {
		name   string
		device DeviceFacts
		want   string
	}{
		{
			name:   "architecture",
			device: DeviceFacts{Architecture: "armeabi-v7a", AndroidAPI: 30, RootManager: "magisk"},
			want:   "architecture",
		},
		{
			name:   "android api",
			device: DeviceFacts{Architecture: "arm64-v8a", AndroidAPI: 29, RootManager: "magisk"},
			want:   "android api",
		},
		{
			name:   "root manager",
			device: DeviceFacts{Architecture: "arm64-v8a", AndroidAPI: 30, RootManager: "unknown"},
			want:   "root manager",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := manifest.SelectArtifact("maa-managerd", tt.device)
			assertErrorContains(t, err, tt.want)
		})
	}
}

func TestSelectArtifactRejectsBlockedComponentVersion(t *testing.T) {
	manifestMap := validManifestMap(t)
	manifestMap["blocked_versions"] = []any{
		map[string]any{
			"component": "maa-managerd",
			"version":   "0.1.0",
			"reason":    "known bad release",
		},
	}
	signed := signedPayload(t, manifestMap, nil)
	manifest, err := LoadAndVerify(signed.payload, signed.signature, signed.publicKey)
	if err != nil {
		t.Fatalf("LoadAndVerify() error = %v", err)
	}

	_, err = manifest.SelectArtifact("maa-managerd", DeviceFacts{
		Architecture: "arm64-v8a",
		AndroidAPI:   30,
		RootManager:  "magisk",
	})
	assertErrorContains(t, err, "blocked")
}

type signedManifest struct {
	payload   []byte
	signature []byte
	publicKey ed25519.PublicKey
}

func signedPayload(t *testing.T, manifest map[string]any, privateKey ed25519.PrivateKey) signedManifest {
	t.Helper()
	if privateKey == nil {
		publicKey, generatedPrivateKey, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			t.Fatalf("GenerateKey() error = %v", err)
		}
		privateKey = generatedPrivateKey
		payload, err := json.Marshal(manifest)
		if err != nil {
			t.Fatalf("Marshal() error = %v", err)
		}
		return signedManifest{
			payload:   payload,
			signature: ed25519.Sign(privateKey, payload),
			publicKey: publicKey,
		}
	}

	payload, err := json.Marshal(manifest)
	if err != nil {
		t.Fatalf("Marshal() error = %v", err)
	}
	return signedManifest{
		payload:   payload,
		signature: ed25519.Sign(privateKey, payload),
		publicKey: privateKey.Public().(ed25519.PublicKey),
	}
}

func validManifestMap(t *testing.T) map[string]any {
	t.Helper()
	const document = `{
		"schema_version": 1,
		"sequence": 1,
		"channel": "stable",
		"generated_at": "2026-08-12T07:44:00Z",
		"minimum_bootstrap_version": "0.1.0",
		"notices": [],
		"blocked_versions": [],
		"components": [{
			"id": "maa-managerd",
			"kind": "manager",
			"version": "0.1.0",
			"required": true,
			"install_mode": "replace-file",
			"compatibility": {
				"architectures": ["arm64-v8a"],
				"android_api": {"minimum": 30, "maximum": 35},
				"root_managers": ["magisk", "kernelsu", "apatch"]
			},
			"artifacts": [{
				"url": "https://github.com/example/project/releases/download/v0.1.0/maa-managerd-linux-arm64",
				"mirrors": [],
				"sha256": "1111111111111111111111111111111111111111111111111111111111111111",
				"size": 8388608,
				"media_type": "application/octet-stream",
				"compression": "none",
				"provenance": {
					"publisher": "MAA Phone Manager",
					"source_url": "https://github.com/example/project/tree/v0.1.0",
					"license_url": "https://github.com/example/project/blob/v0.1.0/LICENSE"
				}
			}],
			"health_checks": ["manager-api"]
		}],
		"upstream_policy": {
			"maa-cli": {"strategy": "maa-self-update", "channel": "stable", "automatic": false, "allowed_versions": ">=0.5.0 <1.0.0"},
			"maacore": {"strategy": "maa-update", "channel": "stable", "automatic": true},
			"maa-resource": {"strategy": "resource-sync", "channel": "stable", "automatic": true}
		}
	}`

	var manifest map[string]any
	if err := json.Unmarshal([]byte(document), &manifest); err != nil {
		t.Fatalf("Unmarshal test manifest: %v", err)
	}
	return manifest
}

func firstComponent(manifest map[string]any) map[string]any {
	return manifest["components"].([]any)[0].(map[string]any)
}

func firstArtifact(manifest map[string]any) map[string]any {
	return firstComponent(manifest)["artifacts"].([]any)[0].(map[string]any)
}

func cloneMap(t *testing.T, value any) map[string]any {
	t.Helper()
	payload, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("Marshal clone: %v", err)
	}
	var cloned map[string]any
	if err := json.Unmarshal(payload, &cloned); err != nil {
		t.Fatalf("Unmarshal clone: %v", err)
	}
	return cloned
}

func assertErrorContains(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("error = nil, want substring %q", want)
	}
	if !strings.Contains(strings.ToLower(err.Error()), strings.ToLower(want)) {
		t.Fatalf("error = %q, want substring %q", err, want)
	}
}
