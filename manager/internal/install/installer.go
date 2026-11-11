// Package install stages project archives, not Debian rootfs images. Rootfs
// needs a separate policy for symlinks, ownership and special files.
package install

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"

	"maa-phone-manager/manager/internal/manifest"
)

const maxExpanded = int64(2 << 30)

// Stage verifies the same open archive handle before extracting. Only regular
// files and directories are permitted. A failed stage never exposes releaseDir.
// The caller owns the private parent directory and serializes installation.
func Stage(ctx context.Context, a manifest.Artifact, archivePath, releaseDir string) error {
	return stageRelease(ctx, a, archivePath, releaseDir, nil)
}

// StageSealed is Stage plus a sealed release: sealFiles are written into the
// private staging tree, fsynced, and only then published with the single rename
// that creates releaseDir. A published release therefore either carries every
// seal file or does not exist at all, so a crash can never leave a candidate
// whose descriptor is missing or half written.
//
// Seal names must be single safe components (no separators) and must not collide
// with archive entries. The whole seal set is limited to maxSealBytes.
func StageSealed(ctx context.Context, a manifest.Artifact, archivePath, releaseDir string, sealFiles map[string][]byte) error {
	return stageRelease(ctx, a, archivePath, releaseDir, sealFiles)
}

func stageRelease(ctx context.Context, a manifest.Artifact, archivePath, releaseDir string, sealFiles map[string][]byte) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if a.Compression != "tar.gz" {
		return errors.New("project installer supports tar.gz only")
	}
	if a.Size <= 0 || a.Size > 2<<30 {
		return errors.New("invalid archive size")
	}
	f, err := os.Open(archivePath)
	if err != nil {
		return err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return err
	}
	if !st.Mode().IsRegular() || st.Size() != a.Size {
		return errors.New("invalid archive file")
	}
	h := sha256.New()
	if _, err := io.Copy(h, &contextReader{ctx, f}); err != nil {
		return err
	}
	if fmt.Sprintf("%x", h.Sum(nil)) != a.SHA256 {
		return errors.New("archive hash mismatch")
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	parent, err := os.OpenRoot(filepath.Dir(releaseDir))
	if err != nil {
		return err
	}
	defer parent.Close()
	name := filepath.Base(releaseDir)
	if !safeName(name) {
		return errors.New("invalid release directory")
	}
	if _, err := parent.Lstat(name); !errors.Is(err, os.ErrNotExist) {
		return errors.New("release already exists or cannot be inspected")
	}
	tmp, err := os.MkdirTemp(parent.Name(), ".staging-")
	if err != nil {
		return err
	}
	tmpName := filepath.Base(tmp)
	// Only this freshly allocated staging directory may be removed on failure.
	defer parent.RemoveAll(tmpName)
	root, err := parent.OpenRoot(tmpName)
	if err != nil {
		return err
	}
	defer root.Close()
	gz, err := gzip.NewReader(&contextReader{ctx, f})
	if err != nil {
		return err
	}
	defer gz.Close()
	tr := tar.NewReader(io.LimitReader(gz, maxExpanded+(64<<20)))
	seen := map[string]bool{}
	var total int64
	for count := 0; ; count++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if count >= 100000 {
			return errors.New("too many archive entries")
		}
		n := strings.TrimSuffix(hdr.Name, "/")
		if !safeName(n) || seen[n] {
			return errors.New("unsafe or duplicate archive path")
		}
		seen[n] = true
		if hdr.Mode&07000 != 0 {
			return errors.New("privileged archive mode")
		}
		if hdr.Size < 0 || hdr.Size > maxExpanded-total {
			return errors.New("expanded archive exceeds limit")
		}
		total += hdr.Size
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := root.MkdirAll(n, 0700); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := root.MkdirAll(path.Dir(n), 0700); err != nil {
				return err
			}
			out, err := root.OpenFile(n, os.O_CREATE|os.O_EXCL|os.O_WRONLY, os.FileMode(hdr.Mode)&0755|0600)
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(out, &contextReader{ctx, tr})
			syncErr := out.Sync()
			closeErr := out.Close()
			if err := errors.Join(copyErr, syncErr, closeErr); err != nil {
				return err
			}
		default:
			return errors.New("archive links and special entries are forbidden")
		}
	}
	// Read through gzip EOF so its trailer checksum is checked. Bound padding too.
	padding, err := io.Copy(io.Discard, io.LimitReader(gz, (1<<20)+1))
	if err != nil {
		return err
	}
	if padding > 1<<20 {
		return errors.New("excess archive padding")
	}
	if len(seen) == 0 {
		return errors.New("empty project archive")
	}
	if err := writeSealFiles(root, seen, sealFiles); err != nil {
		return err
	}
	if err := syncTree(root); err != nil {
		return err
	}
	if err := root.Close(); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if _, err := parent.Lstat(name); !errors.Is(err, os.ErrNotExist) {
		return errors.New("release appeared during staging")
	}
	if err := parent.Rename(tmpName, name); err != nil {
		return err
	}
	return syncParent(parent)
}

func safeName(n string) bool {
	if n == "" || n == "." || strings.ContainsAny(n, "\\:\x00") || path.IsAbs(n) || path.Clean(n) != n {
		return false
	}
	for _, part := range strings.Split(n, "/") {
		if part == ".." || strings.HasSuffix(part, ".") || strings.HasSuffix(part, " ") {
			return false
		}
		base := strings.ToUpper(strings.SplitN(part, ".", 2)[0])
		if base == "NUL" || base == "CON" || base == "PRN" || base == "AUX" || (len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) && base[3] >= '0' && base[3] <= '9') {
			return false
		}
	}
	return true
}

type contextReader struct {
	ctx    context.Context
	reader io.Reader
}

// maxSealBytes bounds the descriptor files that may be sealed into one release.
const maxSealBytes = 1 << 20

// writeSealFiles writes the seal set into the unpublished staging tree. Names
// are sorted so a rejected set fails deterministically, and every file is
// fsynced before the caller publishes the directory.
func writeSealFiles(root *os.Root, seen map[string]bool, sealFiles map[string][]byte) error {
	if len(sealFiles) == 0 {
		return nil
	}
	names := make([]string, 0, len(sealFiles))
	for name := range sealFiles {
		names = append(names, name)
	}
	sort.Strings(names)
	var total int64
	for _, name := range names {
		b := sealFiles[name]
		if !safeName(name) || strings.Contains(name, "/") || seen[name] {
			return fmt.Errorf("invalid or colliding seal file name %q", name)
		}
		total += int64(len(b))
		if total > maxSealBytes {
			return errors.New("seal files exceed size limit")
		}
		f, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return err
		}
		if _, err := f.Write(b); err != nil {
			return errors.Join(err, f.Close())
		}
		if err := f.Sync(); err != nil {
			return errors.Join(err, f.Close())
		}
		if err := f.Close(); err != nil {
			return err
		}
		seen[name] = true
	}
	return nil
}

func (r *contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.reader.Read(p)
}
