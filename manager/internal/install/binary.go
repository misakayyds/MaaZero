package install

// StageBinary stages the manager daemon executable. See runtime.go for how the
// shared verified-handle and private-staging helpers behave.

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"maa-phone-manager/manager/internal/manifest"
)

// StageBinary stages the verified manager binary as releaseDir/maa-managerd.
//
// Only an uncompressed artifact is accepted: the daemon image must be copied
// byte for byte, and a container format would add a decompression step without
// adding authenticity. The manifest byte size and SHA-256 are verified on the
// source handle, the copy is hashed again inside the staging directory, and only
// then is the release published by one rename. Hashing the staged bytes is what
// makes "the published bytes are the verified bytes" true even if the source was
// rewritten in place between verification and copy. A failure after publication
// wraps ErrPublished because the release directory exists.
//
// The staged file is executable; content authenticity comes from the signed
// manifest hash, not from inspecting the image.
func StageBinary(ctx context.Context, a manifest.Artifact, archivePath, releaseDir string) (err error) {
	if err := ctx.Err(); err != nil {
		return err
	}
	if a.Compression != "none" {
		return fmt.Errorf("manager binary must be uncompressed, artifact declares %q", a.Compression)
	}
	if a.Size <= 0 || a.Size > binaryLimits.MaxBytes {
		return fmt.Errorf("invalid manager binary size %d", a.Size)
	}
	f, err := openVerifiedArtifact(ctx, a, archivePath)
	if err != nil {
		return err
	}
	sourceClosed := false
	defer func() {
		if !sourceClosed {
			err = errors.Join(err, f.Close())
		}
	}()
	stg, err := newReleaseStaging(filepath.Dir(releaseDir), filepath.Base(releaseDir))
	if err != nil {
		return err
	}
	defer func() { err = errors.Join(err, stg.finish()) }()
	out, err := stg.dir.OpenFile(managerBinaryName, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return fmt.Errorf("create %s: %w", managerBinaryName, err)
	}
	n, copyErr := io.Copy(out, &contextReader{ctx: ctx, reader: f})
	if copyErr == nil && n != a.Size {
		copyErr = fmt.Errorf("copied %d of %d bytes", n, a.Size)
	}
	modeErr := out.Chmod(0755)
	syncErr := out.Sync()
	closeErr := out.Close()
	if err := errors.Join(copyErr, modeErr, syncErr, closeErr); err != nil {
		return fmt.Errorf("write %s: %w", managerBinaryName, err)
	}
	if err := verifyStagedFile(stg.dir, managerBinaryName, a.Size, a.SHA256); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	// Close the verified source before publishing: a close error after the rename
	// could not be told apart from "nothing was produced".
	if closeErr := f.Close(); closeErr != nil {
		// Close renders the file unusable even when it reports an error, so the
		// deferred path must not retry it; nothing was published.
		sourceClosed = true
		return fmt.Errorf("close artifact: %w", closeErr)
	}
	sourceClosed = true
	if _, err := stg.commit(ctx, nil); err != nil {
		return err
	}
	return nil
}

// verifyStagedFile re-reads a file inside the staging root and proves its byte
// size and SHA-256 match the signed artifact. Hashing the staged copy, not only
// the source, closes the gap between the bytes that were verified and the bytes
// that get published: an in-place rewrite of the source between verification and
// copy is caught here.
func verifyStagedFile(root *os.Root, name string, size int64, wantSHA256 string) error {
	f, err := root.Open(name)
	if err != nil {
		return fmt.Errorf("open staged %s: %w", name, err)
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return fmt.Errorf("stat staged %s: %w", name, err)
	}
	if !st.Mode().IsRegular() || st.Size() != size {
		return fmt.Errorf("staged %s is not the expected regular %d byte file", name, size)
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return fmt.Errorf("hash staged %s: %w", name, err)
	}
	if fmt.Sprintf("%x", h.Sum(nil)) != wantSHA256 {
		return fmt.Errorf("staged %s does not match the verified artifact hash", name)
	}
	return nil
}
