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

func signAndVerifyPayload(t *testing.T, doc map[string]any) (*ChannelManifest, error) {
	t.Helper()
	payload, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("Marshal manifest: %v", err)
	}
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}
	signature := ed25519.Sign(privateKey, payload)
	return LoadAndVerify(payload, signature, publicKey)
}

func loadManifestMap(t *testing.T, relPath string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(relPath)
	if err != nil {
		t.Fatalf("ReadFile(%s): %v", relPath, err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("Unmarshal(%s): %v", relPath, err)
	}
	return doc
}

func TestProductionManifestsPassLoadAndVerify(t *testing.T) {
	paths := []string{
		filepath.Join("..", "..", "..", "manifest", "channels", "stable.json"),
		filepath.Join("..", "..", "..", "manifest", "channels", "beta.json"),
	}
	for _, distCandidate := range []string{
		filepath.Join("..", "..", "..", "dist", "channels", "stable.json"),
		filepath.Join("..", "..", "..", "dist", "channels", "beta.json"),
	} {
		if _, err := os.Stat(distCandidate); err == nil {
			paths = append(paths, distCandidate)
		}
	}

	for _, p := range paths {
		t.Run(filepath.Base(filepath.Dir(p))+"/"+filepath.Base(p), func(t *testing.T) {
			doc := loadManifestMap(t, p)
			m, err := signAndVerifyPayload(t, doc)
			if err != nil {
				t.Fatalf("Production manifest at %s rejected by LoadAndVerify: %v", p, err)
			}
			if len(m.Components) != 3 {
				t.Fatalf("Component count = %d, want 3", len(m.Components))
			}
			if m.Sequence <= 0 {
				t.Fatalf("Sequence = %d, must be positive", m.Sequence)
			}
		})
	}
}

func TestManifestAdversarialMutationsStrictlyRejected(t *testing.T) {
	manifestPaths := []string{
		filepath.Join("..", "..", "..", "manifest", "channels", "stable.json"),
		filepath.Join("..", "..", "..", "manifest", "channels", "beta.json"),
	}

	for _, mPath := range manifestPaths {
		channelName := filepath.Base(mPath)
		t.Run(channelName, func(t *testing.T) {

			// 1. Mutation: Uppercase SHA-256
			t.Run("Mutation_UppercaseSHA256", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				artifacts := firstComp["artifacts"].([]any)
				art := artifacts[0].(map[string]any)
				origSHA := art["sha256"].(string)
				art["sha256"] = strings.ToUpper(origSHA)

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "invalid sha256") {
					t.Fatalf("LoadAndVerify on uppercase SHA-256: err=%v, want 'invalid sha256'", err)
				}
			})

			// 2. Mutation: Truncated SHA-256 (63 chars)
			t.Run("Mutation_TruncatedSHA256", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				artifacts := firstComp["artifacts"].([]any)
				art := artifacts[0].(map[string]any)
				origSHA := art["sha256"].(string)
				art["sha256"] = origSHA[:63]

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "invalid sha256") {
					t.Fatalf("LoadAndVerify on truncated SHA-256: err=%v, want 'invalid sha256'", err)
				}
			})

			// 3. Mutation: Extended SHA-256 (65 chars)
			t.Run("Mutation_ExtendedSHA256", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				artifacts := firstComp["artifacts"].([]any)
				art := artifacts[0].(map[string]any)
				origSHA := art["sha256"].(string)
				art["sha256"] = origSHA + "f"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "invalid sha256") {
					t.Fatalf("LoadAndVerify on extended SHA-256: err=%v, want 'invalid sha256'", err)
				}
			})

			// 4. Mutation: Mutable URL with /latest/
			t.Run("Mutation_MutableURLLatest", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				artifacts := firstComp["artifacts"].([]any)
				art := artifacts[0].(map[string]any)
				art["url"] = "https://github.com/MaaXYZ/maa-phone-manager/releases/latest/download/0.1.0-preview/maa-managerd"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "invalid immutable https url") {
					t.Fatalf("LoadAndVerify on mutable URL (/latest/): err=%v, want 'invalid immutable https url'", err)
				}
			})

			// 5. Mutation: Mutable URL with /main/
			t.Run("Mutation_MutableURLMain", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				artifacts := firstComp["artifacts"].([]any)
				art := artifacts[0].(map[string]any)
				art["url"] = "https://github.com/MaaXYZ/maa-phone-manager/raw/main/0.1.0-preview/maa-managerd"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "invalid immutable https url") {
					t.Fatalf("LoadAndVerify on mutable URL (/main/): err=%v, want 'invalid immutable https url'", err)
				}
			})

			// 6. Mutation: URL missing component version in path
			t.Run("Mutation_URLMissingVersionInPath", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				artifacts := firstComp["artifacts"].([]any)
				art := artifacts[0].(map[string]any)
				art["url"] = "https://github.com/MaaXYZ/maa-phone-manager/releases/download/v9.9.9/maa-managerd"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "path does not contain version") {
					t.Fatalf("LoadAndVerify on URL missing version: err=%v, want 'path does not contain version'", err)
				}
			})

			// 7. Mutation: Missing upstream_policy
			t.Run("Mutation_MissingUpstreamPolicy", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				delete(doc, "upstream_policy")

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "upstream policy must contain") {
					t.Fatalf("LoadAndVerify on missing upstream_policy: err=%v, want 'upstream policy must contain'", err)
				}
			})

			// 8. Mutation: Altered upstream_policy strategy
			t.Run("Mutation_AlteredUpstreamStrategy", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				up := doc["upstream_policy"].(map[string]any)
				maaCli := up["maa-cli"].(map[string]any)
				maaCli["strategy"] = "git-checkout"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "has invalid strategy") {
					t.Fatalf("LoadAndVerify on invalid upstream strategy: err=%v, want 'has invalid strategy'", err)
				}
			})

			// 9. Mutation: Altered upstream_policy channel
			t.Run("Mutation_AlteredUpstreamChannel", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				up := doc["upstream_policy"].(map[string]any)
				maaCli := up["maa-cli"].(map[string]any)
				maaCli["channel"] = "nightly"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "has invalid channel") {
					t.Fatalf("LoadAndVerify on invalid upstream channel: err=%v, want 'has invalid channel'", err)
				}
			})

			// 10. Mutation: Non-positive sequence (0)
			t.Run("Mutation_NonPositiveSequenceZero", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				doc["sequence"] = 0

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "manifest sequence must be positive") {
					t.Fatalf("LoadAndVerify on sequence=0: err=%v, want 'manifest sequence must be positive'", err)
				}
			})

			// 11. Mutation: Non-positive sequence (-5)
			t.Run("Mutation_NonPositiveSequenceNegative", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				doc["sequence"] = -5

				_, err := signAndVerifyPayload(t, doc)
				if err == nil {
					t.Fatalf("LoadAndVerify on sequence=-5 unexpectedly succeeded")
				}
				// Rejection can happen at JSON unmarshal into uint64 or at validate()
				if !strings.Contains(err.Error(), "cannot unmarshal number -5 into Go struct field") &&
					!strings.Contains(err.Error(), "manifest sequence must be positive") {
					t.Fatalf("LoadAndVerify on sequence=-5: err=%v, want decode rejection or sequence positive", err)
				}
			})

			// 12. Mutation: Non-existent component kind
			t.Run("Mutation_NonExistentComponentKind", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				comps := doc["components"].([]any)
				firstComp := comps[0].(map[string]any)
				firstComp["kind"] = "sidecar"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "invalid kind") {
					t.Fatalf("LoadAndVerify on non-existent kind: err=%v, want 'invalid kind'", err)
				}
			})

			// 13. Mutation: Unknown top-level field (strict json unmarshal gate)
			t.Run("Mutation_UnknownTopLevelField", func(t *testing.T) {
				doc := loadManifestMap(t, mPath)
				doc["malicious_extra_field"] = "injection"

				_, err := signAndVerifyPayload(t, doc)
				if err == nil || !strings.Contains(err.Error(), "unknown field") {
					t.Fatalf("LoadAndVerify on unknown field: err=%v, want 'unknown field'", err)
				}
			})
		})
	}
}
