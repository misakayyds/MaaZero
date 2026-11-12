package main

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"maa-phone-manager/manager/internal/install"
	"maa-phone-manager/manager/internal/manifest"
)

type publicReleaseConfig struct {
	ManifestURL string `json:"manifest_url"`
	PublicKey   string `json:"public_key"`
	Channel     string `json:"channel"`
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "maa-release-sign: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	fs := flag.NewFlagSet("maa-release-sign", flag.ContinueOnError)
	keyPath := fs.String("key", "", "Path to external 32-byte base64 Ed25519 seed file (MUST be outside repo)")
	repoRoot := fs.String("repo-root", "", "Repository root path (used to forbid in-repo private keys)")
	initKey := fs.Bool("init-key", false, "Generate keypair if -key does not exist yet")
	verifyOnly := fs.Bool("verify-only", false, "Verify existing .sig files against -release-config without signing")
	artifactsRoot := fs.String("verify-artifacts", "", "Optional directory containing built release artifacts to stage-verify")
	distDir := fs.String("dist-dir", "", "Directory containing channel JSON manifests to sign (e.g. dist/channels)")
	mirrorDir := fs.String("mirror-dir", "", "Optional directory to mirror signed .sig files into (e.g. manifest/channels)")
	releaseOut := fs.String("release-out", "", "Output path for public release.json (e.g. dist/release.json)")
	releaseMirror := fs.String("release-mirror", "", "Optional mirror path for public release.json (e.g. manifest/release.json)")
	manifestURL := fs.String("manifest-url", "https://misakayyds.github.io/MaaZero/channels/stable.json", "Public HTTPS manifest_url for release.json")
	channel := fs.String("channel", "stable", "Default channel for release.json (stable or beta)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	if *verifyOnly {
		if *releaseOut == "" || *distDir == "" {
			return errors.New("-verify-only requires -release-out and -dist-dir")
		}
		if err := verifyDirectory(*distDir, *releaseOut); err != nil {
			return err
		}
		if *artifactsRoot != "" {
			return verifyArtifacts(*distDir, *releaseOut, *artifactsRoot)
		}
		return nil
	}

	if *keyPath == "" {
		return errors.New("-key is required")
	}
	absKey, err := filepath.Abs(*keyPath)
	if err != nil {
		return err
	}
	if *repoRoot != "" {
		absRepo, err := filepath.Abs(*repoRoot)
		if err != nil {
			return err
		}
		if isSubpath(absRepo, absKey) {
			return fmt.Errorf("refusing private signing key inside repository root (%s)", absRepo)
		}
	}

	priv, pub, err := loadOrCreateKey(absKey, *initKey)
	if err != nil {
		return err
	}
	pubB64 := base64.StdEncoding.EncodeToString(pub)

	if *distDir != "" {
		for _, ch := range []string{"stable", "beta"} {
			jsonPath := filepath.Join(*distDir, ch+".json")
			payload, err := os.ReadFile(jsonPath)
			if err != nil {
				return fmt.Errorf("read %s: %w", jsonPath, err)
			}
			sig := ed25519.Sign(priv, payload)
			if _, err := manifest.LoadAndVerify(payload, sig, pub); err != nil {
				return fmt.Errorf("self-verify %s failed: %w", jsonPath, err)
			}
			sigPath := jsonPath + ".sig"
			if err := os.WriteFile(sigPath, sig, 0644); err != nil {
				return fmt.Errorf("write %s: %w", sigPath, err)
			}
			if *mirrorDir != "" {
				if err := os.MkdirAll(*mirrorDir, 0755); err != nil {
					return err
				}
				mirrorJSON := filepath.Join(*mirrorDir, ch+".json")
				if err := os.WriteFile(mirrorJSON, payload, 0644); err != nil {
					return err
				}
				mirrorSig := filepath.Join(*mirrorDir, ch+".json.sig")
				if err := os.WriteFile(mirrorSig, sig, 0644); err != nil {
					return err
				}
			}
			fmt.Printf("SIGNED_MANIFEST_OK channel=%s sig=%s bytes=%d\n", ch, sigPath, len(sig))
		}
	}

	if *releaseOut != "" {
		cfg := publicReleaseConfig{
			ManifestURL: *manifestURL,
			PublicKey:   pubB64,
			Channel:     *channel,
		}
		rawCfg, err := json.MarshalIndent(cfg, "", "  ")
		if err != nil {
			return err
		}
		rawCfg = append(rawCfg, '\n')
		if err := os.MkdirAll(filepath.Dir(*releaseOut), 0755); err != nil {
			return err
		}
		if err := os.WriteFile(*releaseOut, rawCfg, 0644); err != nil {
			return err
		}
		if *releaseMirror != "" {
			if err := os.MkdirAll(filepath.Dir(*releaseMirror), 0755); err != nil {
				return err
			}
			if err := os.WriteFile(*releaseMirror, rawCfg, 0644); err != nil {
				return err
			}
		}
		fmt.Printf("RELEASE_CONFIG_OK path=%s public_key=%s channel=%s\n", *releaseOut, pubB64, *channel)
	}

	if *distDir != "" && *releaseOut != "" {
		if err := verifyDirectory(*distDir, *releaseOut); err != nil {
			return err
		}
		if *mirrorDir != "" {
			if err := verifyDirectory(*mirrorDir, *releaseOut); err != nil {
				return err
			}
		}
	}
	return nil
}

func isSubpath(parent, target string) bool {
	rel, err := filepath.Rel(parent, target)
	if err != nil {
		return false
	}
	rel = filepath.ToSlash(rel)
	return rel == "." || (!strings.HasPrefix(rel, "../") && rel != "..")
}

func loadOrCreateKey(absKey string, allowCreate bool) (ed25519.PrivateKey, ed25519.PublicKey, error) {
	raw, err := os.ReadFile(absKey)
	if errors.Is(err, os.ErrNotExist) {
		if !allowCreate {
			return nil, nil, fmt.Errorf("signing key not found at %s (pass -init-key to generate)", absKey)
		}
		if err := os.MkdirAll(filepath.Dir(absKey), 0700); err != nil {
			return nil, nil, err
		}
		pub, priv, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			return nil, nil, err
		}
		seedB64 := base64.StdEncoding.EncodeToString(priv.Seed()) + "\n"
		if err := os.WriteFile(absKey, []byte(seedB64), 0600); err != nil {
			return nil, nil, err
		}
		pubB64 := base64.StdEncoding.EncodeToString(pub) + "\n"
		if err := os.WriteFile(absKey+".pub", []byte(pubB64), 0644); err != nil {
			return nil, nil, err
		}
		fmt.Printf("KEYPAIR_CREATED pub_file=%s.pub public_key=%s\n", absKey, strings.TrimSpace(pubB64))
		return priv, pub, nil
	}
	if err != nil {
		return nil, nil, err
	}
	seed, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, nil, errors.New("invalid Ed25519 seed file (expected 32-byte base64 seed)")
	}
	priv := ed25519.NewKeyFromSeed(seed)
	pub := priv.Public().(ed25519.PublicKey)
	pubB64 := base64.StdEncoding.EncodeToString(pub) + "\n"
	_ = os.WriteFile(absKey+".pub", []byte(pubB64), 0644)
	return priv, pub, nil
}

func verifyDirectory(dir, releaseConfigPath string) error {
	rawCfg, err := os.ReadFile(releaseConfigPath)
	if err != nil {
		return err
	}
	var cfg publicReleaseConfig
	if err := json.Unmarshal(rawCfg, &cfg); err != nil {
		return err
	}
	pub, err := base64.StdEncoding.DecodeString(cfg.PublicKey)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return errors.New("invalid public_key in release.json")
	}
	for _, ch := range []string{"stable", "beta"} {
		jsonPath := filepath.Join(dir, ch+".json")
		sigPath := jsonPath + ".sig"
		payload, err := os.ReadFile(jsonPath)
		if err != nil {
			return err
		}
		sig, err := os.ReadFile(sigPath)
		if err != nil {
			return err
		}
		m, err := manifest.LoadAndVerify(payload, sig, pub)
		if err != nil {
			return fmt.Errorf("verify %s: %w", jsonPath, err)
		}
		if m.Channel != ch {
			return fmt.Errorf("channel mismatch in %s: got %s", jsonPath, m.Channel)
		}
		fmt.Printf("VERIFIED_MANIFEST_OK dir=%s channel=%s sequence=%d components=%d\n", dir, ch, m.Sequence, len(m.Components))
	}
	return nil
}

func verifyArtifacts(manifestDir, releaseConfigPath, artifactsRoot string) error {
	rawCfg, err := os.ReadFile(releaseConfigPath)
	if err != nil {
		return err
	}
	var cfg publicReleaseConfig
	if err := json.Unmarshal(rawCfg, &cfg); err != nil {
		return err
	}
	pub, err := base64.StdEncoding.DecodeString(cfg.PublicKey)
	if err != nil || len(pub) != ed25519.PublicKeySize {
		return errors.New("invalid public_key in release.json")
	}
	payload, err := os.ReadFile(filepath.Join(manifestDir, "stable.json"))
	if err != nil {
		return err
	}
	sig, err := os.ReadFile(filepath.Join(manifestDir, "stable.json.sig"))
	if err != nil {
		return err
	}
	m, err := manifest.LoadAndVerify(payload, sig, pub)
	if err != nil {
		return err
	}
	tmpDir, err := os.MkdirTemp("", "maazero-stage-verify-*")
	if err != nil {
		return err
	}
	defer os.RemoveAll(tmpDir)

	facts := manifest.DeviceFacts{Architecture: "arm64-v8a", AndroidAPI: 35, RootManager: "kernelsu"}
	ctx := context.Background()
	for _, comp := range m.Components {
		art, err := m.SelectArtifact(comp.ID, facts)
		if err != nil {
			return err
		}
		baseName := filepath.Base(art.URL)
		artPath := filepath.Join(artifactsRoot, baseName)
		stageDest := filepath.Join(tmpDir, comp.ID)
		switch comp.Kind {
		case "manager":
			if err := install.StageBinary(ctx, art, artPath, stageDest); err != nil {
				return fmt.Errorf("StageBinary(%s): %w", artPath, err)
			}
			fmt.Printf("VERIFIED_ARTIFACT_OK component=%s kind=manager path=%s\n", comp.ID, artPath)
		case "project":
			if err := install.Stage(ctx, art, artPath, stageDest); err != nil {
				return fmt.Errorf("Stage(%s): %w", artPath, err)
			}
			fmt.Printf("VERIFIED_ARTIFACT_OK component=%s kind=project path=%s\n", comp.ID, artPath)
		case "runtime":
			stats, err := install.StageRootfs(ctx, art, artPath, stageDest)
			if err != nil {
				return fmt.Errorf("StageRootfs(%s): %w", artPath, err)
			}
			fmt.Printf("VERIFIED_ARTIFACT_OK component=%s kind=runtime files=%d dirs=%d symlinks=%d hardlinks=%d expanded=%d\n",
				comp.ID, stats.Files, stats.Dirs, stats.Symlinks, stats.Hardlinks, stats.ExpandedBytes)
		}
	}
	return nil
}
