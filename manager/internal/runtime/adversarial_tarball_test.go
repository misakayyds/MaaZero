package runtime

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

func buildCleanCheckoutProjectTarball(t *testing.T) string {
	t.Helper()
	sourceDir := filepath.Join("..", "..", "..", "packaging", "project", "source")
	entries, err := os.ReadDir(sourceDir)
	if err != nil {
		t.Fatalf("ReadDir(%s): %v", sourceDir, err)
	}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		ext := strings.ToLower(filepath.Ext(entry.Name()))
		if allowedProjectExtensions[ext] {
			names = append(names, entry.Name())
		}
	}
	sort.Strings(names)

	outPath := filepath.Join(t.TempDir(), "maa-phone-project-0.1.0-preview.tar.gz")
	outFile, err := os.Create(outPath)
	if err != nil {
		t.Fatalf("Create temp tarball: %v", err)
	}
	defer outFile.Close()

	mtime := time.Unix(978307200, 0).UTC()
	gzw := gzip.NewWriter(outFile)
	gzw.ModTime = mtime
	tw := tar.NewWriter(gzw)

	for _, name := range names {
		raw, err := os.ReadFile(filepath.Join(sourceDir, name))
		if err != nil {
			t.Fatalf("ReadFile(%s): %v", name, err)
		}
		if name == "maa-webui.py" {
			raw = bytes.Replace(raw, []byte(`HOST = "0.0.0.0"`), []byte(`HOST = "127.0.0.1"`), 1)
			raw = bytes.Replace(raw, []byte("PORT = 8080"), []byte("PORT = 8089"), 1)
		}
		mode := int64(0o644)
		if strings.HasSuffix(strings.ToLower(name), ".sh") {
			mode = 0o755
		}
		hdr := &tar.Header{
			Typeflag: tar.TypeReg,
			Name:     name,
			Size:     int64(len(raw)),
			Mode:     mode,
			ModTime:  mtime,
			Uid:      0,
			Gid:      0,
			Uname:    "root",
			Gname:    "root",
			Format:   tar.FormatGNU,
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatalf("WriteHeader(%s): %v", name, err)
		}
		if _, err := tw.Write(raw); err != nil {
			t.Fatalf("Write(%s): %v", name, err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tar Close: %v", err)
	}
	if err := gzw.Close(); err != nil {
		t.Fatalf("gzip Close: %v", err)
	}
	return outPath
}

func TestProjectTarballFlawlessAndAdversarialMutations(t *testing.T) {
	tarballPath := filepath.Join("..", "..", "..", "dist", "maa-phone-project-0.1.0-preview.tar.gz")
	if _, err := os.Stat(tarballPath); os.IsNotExist(err) {
		tarballPath = buildCleanCheckoutProjectTarball(t)
	}
	f, err := os.Open(tarballPath)
	if err != nil {
		t.Fatalf("Open project tarball: %v", err)
	}
	defer f.Close()

	gzr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatalf("New gzip reader: %v", err)
	}
	defer gzr.Close()

	tr := tar.NewReader(gzr)
	tempDir := t.TempDir()

	memberCount := 0
	const expectedMtime = int64(978307200)

	for {
		header, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatalf("Tar read next: %v", err)
		}

		memberCount++

		// 1. Invariant: flatness (0 directories, 0 subdirectories)
		if header.Typeflag != tar.TypeReg {
			t.Errorf("Member %q has non-regular typeflag %d", header.Name, header.Typeflag)
		}
		if strings.Contains(header.Name, "/") || strings.Contains(header.Name, "\\") {
			t.Errorf("Member %q is not flat (contains path separator)", header.Name)
		}

		// 2. Invariant: deterministic mtime (978307200)
		if header.ModTime.Unix() != expectedMtime {
			t.Errorf("Member %q has mtime %d, want %d", header.Name, header.ModTime.Unix(), expectedMtime)
		}

		// 3. Invariant: root ownership (uid=0, gid=0, uname="root", gname="root")
		if header.Uid != 0 || header.Gid != 0 || header.Uname != "root" || header.Gname != "root" {
			t.Errorf("Member %q has non-root ownership: uid=%d gid=%d uname=%q gname=%q",
				header.Name, header.Uid, header.Gid, header.Uname, header.Gname)
		}

		// 4. Invariant: allowlisted extensions
		ext := strings.ToLower(filepath.Ext(header.Name))
		if !allowedProjectExtensions[ext] {
			t.Errorf("Member %q has non-allowlisted extension %q", header.Name, ext)
		}

		// Extract to tempDir
		destPath := filepath.Join(tempDir, header.Name)
		outFile, err := os.OpenFile(destPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(header.Mode))
		if err != nil {
			t.Fatalf("Create extracted file %s: %v", destPath, err)
		}
		if _, err := io.Copy(outFile, tr); err != nil {
			outFile.Close()
			t.Fatalf("Extract file %s: %v", destPath, err)
		}
		outFile.Close()
	}

	if memberCount != 29 {
		t.Fatalf("Tarball member count = %d, want exactly 29", memberCount)
	}

	// Step 1: ScanProjectDir on clean unpacked archive must pass with 100% acceptance
	if err := ScanProjectDir(tempDir); err != nil {
		t.Fatalf("ScanProjectDir on unpacked clean tarball failed: %v", err)
	}

	// Step 2: Adversarial Mutations - confirm ScanProjectDir strictly rejects them
	t.Run("Mutation_AddDirectory", func(t *testing.T) {
		subDir := filepath.Join(tempDir, "adversarial_subfolder")
		if err := os.Mkdir(subDir, 0755); err != nil {
			t.Fatal(err)
		}
		defer os.RemoveAll(subDir)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "must be flat") {
			t.Fatalf("ScanProjectDir on directory added: err=%v, want 'must be flat'", err)
		}
	})

	t.Run("Mutation_NonAllowlistedExtensionExe", func(t *testing.T) {
		badFile := filepath.Join(tempDir, "exploit.exe")
		if err := os.WriteFile(badFile, []byte("binary payload\n"), 0755); err != nil {
			t.Fatal(err)
		}
		defer os.Remove(badFile)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "not an allowlisted project file") {
			t.Fatalf("ScanProjectDir on .exe file: err=%v, want 'not an allowlisted project file'", err)
		}
	})

	t.Run("Mutation_NonAllowlistedExtensionMarkdown", func(t *testing.T) {
		badFile := filepath.Join(tempDir, "README.md")
		if err := os.WriteFile(badFile, []byte("# Docs\n"), 0644); err != nil {
			t.Fatal(err)
		}
		defer os.Remove(badFile)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "not an allowlisted project file") {
			t.Fatalf("ScanProjectDir on .md file: err=%v, want 'not an allowlisted project file'", err)
		}
	})

	t.Run("Mutation_MissingRequiredMaaRun", func(t *testing.T) {
		target := filepath.Join(tempDir, "maa-run.sh")
		bakDir := t.TempDir()
		bak := filepath.Join(bakDir, "maa-run.sh")
		if err := os.Rename(target, bak); err != nil {
			t.Fatal(err)
		}
		defer os.Rename(bak, target)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "missing maa-run.sh") {
			t.Fatalf("ScanProjectDir without maa-run.sh: err=%v, want 'missing maa-run.sh'", err)
		}
	})

	t.Run("Mutation_MissingRequiredAutoUpdate", func(t *testing.T) {
		target := filepath.Join(tempDir, "maa-auto-update.sh")
		bakDir := t.TempDir()
		bak := filepath.Join(bakDir, "maa-auto-update.sh")
		if err := os.Rename(target, bak); err != nil {
			t.Fatal(err)
		}
		defer os.Rename(bak, target)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "missing maa-auto-update.sh") {
			t.Fatalf("ScanProjectDir without maa-auto-update.sh: err=%v, want 'missing maa-auto-update.sh'", err)
		}
	})

	t.Run("Mutation_MissingRequiredWebUI", func(t *testing.T) {
		target := filepath.Join(tempDir, "maa-webui.py")
		bakDir := t.TempDir()
		bak := filepath.Join(bakDir, "maa-webui.py")
		if err := os.Rename(target, bak); err != nil {
			t.Fatal(err)
		}
		defer os.Rename(bak, target)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "missing maa-webui.py") {
			t.Fatalf("ScanProjectDir without maa-webui.py: err=%v, want 'missing maa-webui.py'", err)
		}
	})

	t.Run("Mutation_ForbiddenDeviceHelper", func(t *testing.T) {
		badFile := filepath.Join(tempDir, "ssh_watchdog.sh")
		if err := os.WriteFile(badFile, []byte("#!/bin/sh\nexit 0\n"), 0755); err != nil {
			t.Fatal(err)
		}
		defer os.Remove(badFile)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "must not be part of a distributed project") {
			t.Fatalf("ScanProjectDir on forbidden device helper: err=%v, want 'must not be part of a distributed project'", err)
		}
	})

	t.Run("Mutation_ForbiddenCredential", func(t *testing.T) {
		badFile := filepath.Join(tempDir, "tg.txt")
		if err := os.WriteFile(badFile, []byte("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11\n"), 0600); err != nil {
			t.Fatal(err)
		}
		defer os.Remove(badFile)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "must not be part of a distributed project") {
			t.Fatalf("ScanProjectDir on forbidden credential: err=%v, want 'must not be part of a distributed project'", err)
		}
	})

	t.Run("Mutation_CRByteInScript", func(t *testing.T) {
		target := filepath.Join(tempDir, "maa-run.sh")
		orig, err := os.ReadFile(target)
		if err != nil {
			t.Fatal(err)
		}
		corrupted := append([]byte("\r\n"), orig...)
		if err := os.WriteFile(target, corrupted, 0755); err != nil {
			t.Fatal(err)
		}
		defer os.WriteFile(target, orig, 0755)

		err = ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "contains CR bytes") {
			t.Fatalf("ScanProjectDir on CR byte corruption: err=%v, want 'contains CR bytes'", err)
		}
	})

	t.Run("Mutation_EmptyAllowlistedFile", func(t *testing.T) {
		badFile := filepath.Join(tempDir, "empty.py")
		if err := os.WriteFile(badFile, []byte(""), 0644); err != nil {
			t.Fatal(err)
		}
		defer os.Remove(badFile)

		err := ScanProjectDir(tempDir)
		if err == nil || !strings.Contains(err.Error(), "is empty") {
			t.Fatalf("ScanProjectDir on empty file: err=%v, want 'is empty'", err)
		}
	})
}
