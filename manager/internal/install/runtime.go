package install

// Runtime-format staging policies for the manager binary and the Debian rootfs.
//
// installer.go owns the project tar.gz policy, which rejects every link and
// every privileged mode. A Debian rootfs and the manager executable need
// different policies, so they live here:
//
//   - StageBinary stages an uncompressed executable image as
//     releaseDir/maa-managerd. Only "none" compression is accepted, so the
//     staged bytes are exactly the verified bytes.
//   - StageRootfs stages a Debian rootfs tar.gz. A real rootfs contains relative
//     symlinks, in-root absolute symlinks and hardlinks, so rejecting links
//     outright cannot produce a working runtime. Instead every link target is
//     resolved against the archive root and anything that could leave the root
//     is refused.
//
// Both stages verify the manifest byte size and SHA-256 on the same open handle
// they later extract from, bound the work they do, honour context cancellation,
// and write only inside a freshly created private staging directory next to the
// release directory. A release directory is published by one rename, or not at
// all.
//
// Out of scope on purpose: file ownership, setuid capability and device nodes.
// The policy strips special mode bits instead of executing privileged files,
// never creates devices or mounts, and leaves root ownership to the eventual
// root runtime.

import (
	"archive/tar"
	"compress/gzip"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"maa-phone-manager/manager/internal/manifest"
)

const (
	// managerBinaryName is the fixed executable name inside a release directory.
	managerBinaryName = "maa-managerd"
	// archivePaddingMax bounds the uncompressed bytes after the last tar entry.
	// Reading through the gzip trailer is what verifies its checksum.
	archivePaddingMax = int64(1 << 20)
	// archiveSlack bounds how far past the expansion limit the tar reader may
	// decompress before the limiter stops it.
	archiveSlack = int64(64 << 20)
	// rootfsDefaultRootMode is the release root ("/") mode when the archive has no
	// explicit "." entry, and rootfsImplicitDirMode is used for directories that the
	// archive never declared. The release root is the chroot root, so it must stay
	// traversable by non-root chroot processes.
	rootfsDefaultRootMode = 0755
	rootfsImplicitDirMode = 0755
)

// rootfsLimits bounds rootfs extraction work. It is a variable so tests can
// exercise every bound with small archives; production code never changes it.
//
// Not implemented here, deliberately: there is no free-space (statfs) precheck
// against the release parent, no reclamation of stale .staging-* directories
// left behind by a killed process, and no full-disk recovery beyond failing the
// stage cleanly. Those need the install lock and a storage policy the caller
// owns; this package only bounds what a single extraction may consume.
var rootfsLimits = struct {
	MaxArchive  int64
	MaxExpanded int64
	MaxEntries  int
	MaxTarget   int
}{
	MaxArchive:  8 << 30,
	MaxExpanded: 8 << 30,
	// A minimal Debian rootfs is roughly 20-30k entries. 100k keeps head-room for
	// larger images while bounding the extraction maps (name index, directories,
	// modes, links) that are all live at the same time.
	MaxEntries: 100_000,
	MaxTarget:  4096,
}

// binaryLimits bounds the uncompressed manager binary artifact.
var binaryLimits = struct {
	MaxBytes int64
}{
	MaxBytes: 256 << 20,
}

// ErrPublished reports that staging failed after the release directory name
// became visible in the release parent.
//
// The release is on disk and may be complete: a durability sync failed, or a
// handle could not be closed after the publication rename. Callers must not
// treat such an error as "nothing was produced" (the next attempt would then
// hit an existing release directory): test errors.Is(err, ErrPublished) and
// either adopt the release (verify it, then advance the transaction) or remove
// it explicitly.
var ErrPublished = errors.New("release published but durability not confirmed")

// RootfsStats reports what a verified rootfs archive produced. StrippedSpecial
// modes are counted rather than refused because a real Debian rootfs ships
// /usr/bin/su with setuid set, and the extraction policy clears that bit (the
// runtime is expected to already be root).
type RootfsStats struct {
	Dirs                int
	Files               int
	Symlinks            int
	Hardlinks           int
	SpecialBitsStripped int
	ExpandedBytes       int64
}

// StageRootfs verifies and extracts a Debian rootfs tar.gz into an immutable
// release directory. Only the caller-owned parent directory is touched, and a
// failure before publication never leaves a release candidate behind.
//
// The verified artifact is decompressed twice: an indexing pass refuses
// link-descendant entries before a single write, and an extraction pass builds
// the tree from the same handle. If the publication itself is fine but a later
// sync fails, the returned error wraps ErrPublished because the release
// directory exists.
func StageRootfs(ctx context.Context, a manifest.Artifact, archivePath, releaseDir string) (stats RootfsStats, err error) {
	if err := ctx.Err(); err != nil {
		return stats, err
	}
	if a.Compression != "tar.gz" {
		return stats, fmt.Errorf("rootfs installer supports tar.gz only, artifact declares %q", a.Compression)
	}
	if a.Size <= 0 || a.Size > rootfsLimits.MaxArchive {
		return stats, fmt.Errorf("invalid rootfs archive size %d", a.Size)
	}
	f, err := openVerifiedArtifact(ctx, a, archivePath)
	if err != nil {
		return stats, err
	}
	sourceClosed := false
	defer func() {
		if !sourceClosed {
			err = errors.Join(err, f.Close())
		}
	}()
	stg, err := newReleaseStaging(filepath.Dir(releaseDir), filepath.Base(releaseDir))
	if err != nil {
		return stats, err
	}
	defer func() { err = errors.Join(err, stg.finish()) }()
	gzIndex, err := gzip.NewReader(&contextReader{ctx: ctx, reader: f})
	if err != nil {
		return stats, fmt.Errorf("read gzip header: %w", err)
	}
	links, scanErr := scanRootfs(ctx, tar.NewReader(io.LimitReader(gzIndex, rootfsLimits.MaxExpanded+archiveSlack)))
	if err := errors.Join(scanErr, gzIndex.Close()); err != nil {
		return stats, err
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return stats, fmt.Errorf("rewind artifact: %w", err)
	}
	// Pass 2 reads through a tee, so the compressed bytes that were actually
	// consumed are hashed and counted: extraction only publishes when those bytes
	// are exactly the signed artifact.
	extractSum := sha256.New()
	consumed := &countingWriter{}
	gz, err := gzip.NewReader(io.TeeReader(&contextReader{ctx: ctx, reader: f}, io.MultiWriter(extractSum, consumed)))
	if err != nil {
		return stats, fmt.Errorf("read gzip header: %w", err)
	}
	plan := newRootfsPlan()
	stats, extractErr := extractRootfs(ctx, tar.NewReader(io.LimitReader(gz, rootfsLimits.MaxExpanded+archiveSlack)), stg.dir, links, plan)
	padding, padErr := io.Copy(io.Discard, io.LimitReader(gz, archivePaddingMax+1))
	if err := errors.Join(extractErr, padErr, gz.Close()); err != nil {
		return RootfsStats{}, err
	}
	if padding > archivePaddingMax {
		return RootfsStats{}, errors.New("excess archive padding")
	}
	if err := verifyConsumedArtifact(consumed.n, extractSum.Sum(nil), a); err != nil {
		return RootfsStats{}, err
	}
	if err := ctx.Err(); err != nil {
		return RootfsStats{}, err
	}
	// Close the verified source before publishing, so a close error can never
	// arrive after the release directory already exists. Close renders the file
	// unusable even when it reports an error, so the deferred path must not retry
	// it; nothing was published, so no statistics are returned either.
	if closeErr := f.Close(); closeErr != nil {
		sourceClosed = true
		return RootfsStats{}, fmt.Errorf("close artifact: %w", closeErr)
	}
	sourceClosed = true
	published, err := stg.commit(ctx, plan.applyModes)
	if err != nil {
		if published {
			// The release directory exists; keep the stats so the caller can adopt or
			// remove it instead of assuming nothing was produced.
			return stats, err
		}
		return RootfsStats{}, err
	}
	return stats, nil
}

// extractRootfs consumes the verified tar stream and builds the tree inside the
// private staging root. Directories are created with the private staging mode
// (0700) and their archived modes are applied later from plan, so a read-only
// directory cannot block its own children and the tree stays private while it is
// written.
//
// links is the complete link member set from the indexing pass, which is what
// makes the "no entry beneath a link" analysis complete even for links declared
// later in the archive. Every entry is checked against it, the link members seen
// in this pass must match it before any link is created, and links themselves are
// created in a post-pass after every regular file exists.
func extractRootfs(ctx context.Context, tr *tar.Reader, root *os.Root, links map[string]struct{}, plan *rootfsPlan) (RootfsStats, error) {
	var stats RootfsStats
	seen := make(map[string]struct{}, 1024)
	regular := make(map[string]struct{})
	links2 := make(map[string]struct{})
	type hardLink struct{ name, target string }
	type symLink struct{ name, target string }
	var hardlinks []hardLink
	var symlinks []symLink
	var total int64
	entries := 0
	for {
		if err := ctx.Err(); err != nil {
			return RootfsStats{}, err
		}
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return RootfsStats{}, fmt.Errorf("read archive entry: %w", err)
		}
		entries++
		if entries > rootfsLimits.MaxEntries {
			return RootfsStats{}, fmt.Errorf("too many archive entries (limit %d)", rootfsLimits.MaxEntries)
		}
		name, err := rootfsMemberName(hdr.Name)
		if err != nil {
			return RootfsStats{}, err
		}
		if _, dup := seen[name]; dup {
			return RootfsStats{}, fmt.Errorf("duplicate archive entry %q", hdr.Name)
		}
		seen[name] = struct{}{}
		if hdr.Size < 0 || hdr.Size > rootfsLimits.MaxExpanded-total {
			return RootfsStats{}, fmt.Errorf("archive expands beyond the %d byte limit", rootfsLimits.MaxExpanded)
		}
		total += hdr.Size
		mode := os.FileMode(hdr.Mode) & 0777
		if hdr.Mode&07000 != 0 {
			// Policy: strip special bits instead of executing a privileged file.
			stats.SpecialBitsStripped++
		}
		if name == "." {
			if hdr.Typeflag != tar.TypeDir {
				return RootfsStats{}, fmt.Errorf("archive member %q must be a directory", hdr.Name)
			}
			// The release directory is the chroot root ("/"), so its archived mode is
			// honored; it is applied once the tree is written.
			plan.rootMode = mode
			continue
		}
		if ancestor, ok := linkAncestor(links, name); ok {
			return RootfsStats{}, fmt.Errorf("archive entry %q is extracted beneath link %q", name, ancestor)
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := ensureRootfsDir(root, name, plan.dirs); err != nil {
				return RootfsStats{}, err
			}
			plan.dirModes[name] = mode
		case tar.TypeReg, tar.TypeRegA:
			if err := ensureRootfsDir(root, path.Dir(name), plan.dirs); err != nil {
				return RootfsStats{}, err
			}
			n, err := writeRootfsFile(ctx, root, name, mode, tr, hdr.Size)
			if err != nil {
				return RootfsStats{}, err
			}
			regular[name] = struct{}{}
			stats.Files++
			stats.ExpandedBytes += n
		case tar.TypeSymlink:
			target, err := rootfsLinkTarget(name, hdr.Linkname)
			if err != nil {
				return RootfsStats{}, err
			}
			links2[name] = struct{}{}
			symlinks = append(symlinks, symLink{name: name, target: target})
		case tar.TypeLink:
			target, err := rootfsMemberName(hdr.Linkname)
			if err != nil {
				return RootfsStats{}, fmt.Errorf("hardlink %q: %w", hdr.Name, err)
			}
			links2[name] = struct{}{}
			hardlinks = append(hardlinks, hardLink{name: name, target: target})
		default:
			return RootfsStats{}, fmt.Errorf("unsupported archive entry type %q for %q", hdr.Typeflag, hdr.Name)
		}
	}
	if len(seen) == 0 {
		return RootfsStats{}, errors.New("empty rootfs archive")
	}
	// The indexed link set is what made the ancestor analysis complete. If this
	// pass saw a different set, the archive changed between the two passes and that
	// analysis no longer describes these bytes, so nothing may be linked or
	// published.
	if err := requireSameLinks(links, links2); err != nil {
		return RootfsStats{}, err
	}
	hardNames := make(map[string]struct{}, len(hardlinks))
	for _, hl := range hardlinks {
		hardNames[hl.name] = struct{}{}
	}
	for _, hl := range hardlinks {
		if _, ok := regular[hl.target]; !ok {
			return RootfsStats{}, fmt.Errorf("hardlink %q target %q is not an extracted regular file", hl.name, hl.target)
		}
		if _, chained := hardNames[hl.target]; chained {
			// No link descendants: a hardlink may only point at a file that was
			// extracted from archive data, never at another hardlink.
			return RootfsStats{}, fmt.Errorf("hardlink %q chains onto hardlink %q", hl.name, hl.target)
		}
		if err := ensureRootfsDir(root, path.Dir(hl.name), plan.dirs); err != nil {
			return RootfsStats{}, err
		}
		info, err := root.Lstat(hl.target)
		if err != nil {
			return RootfsStats{}, fmt.Errorf("hardlink %q target %q: %w", hl.name, hl.target, err)
		}
		if !info.Mode().IsRegular() {
			return RootfsStats{}, fmt.Errorf("hardlink %q target %q is not a regular file", hl.name, hl.target)
		}
		if err := root.Link(hl.target, hl.name); err != nil {
			return RootfsStats{}, fmt.Errorf("hardlink %q: %w", hl.name, err)
		}
		stats.Hardlinks++
	}
	for _, sl := range symlinks {
		if err := ensureRootfsDir(root, path.Dir(sl.name), plan.dirs); err != nil {
			return RootfsStats{}, err
		}
		if err := root.Symlink(sl.target, sl.name); err != nil {
			return RootfsStats{}, fmt.Errorf("symlink %q: %w", sl.name, err)
		}
		stats.Symlinks++
	}
	stats.Dirs = len(plan.dirs)
	return stats, nil
}

// ensureRootfsDir creates name and its parents with the private staging mode.
// rootfsPlan carries what is applied to the staging tree once it is completely
// written and synced: the release root mode, the archived directory modes, and
// every directory the release contains. Keeping modes out of the write phase
// means the tree stays private (0700) until publication is imminent.
type rootfsPlan struct {
	rootMode os.FileMode
	dirModes map[string]os.FileMode
	dirs     map[string]struct{}
}

func newRootfsPlan() *rootfsPlan {
	return &rootfsPlan{
		rootMode: rootfsDefaultRootMode,
		dirModes: make(map[string]os.FileMode),
		dirs:     make(map[string]struct{}),
	}
}

// applyModes restores the archived directory modes, deepest first so a
// read-only parent cannot block a child, then the release root. Directories the
// archive never declared keep the default mode.
func (p *rootfsPlan) applyModes(root *os.Root) error {
	names := make([]string, 0, len(p.dirs))
	for name := range p.dirs {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool {
		if di, dj := strings.Count(names[i], "/"), strings.Count(names[j], "/"); di != dj {
			return di > dj
		}
		return names[i] < names[j]
	})
	for _, name := range names {
		mode, declared := p.dirModes[name]
		if !declared {
			mode = rootfsImplicitDirMode
		}
		if err := chmodAndSyncDir(root, name, mode); err != nil {
			return err
		}
	}
	return chmodAndSyncDir(root, ".", p.rootMode)
}

// chmodAndSyncDir applies a directory mode through the directory's own handle and
// syncs that handle, so the mode change is durable: fsync of a directory flushes
// the directory's own metadata. The handle is opened before the chmod, because an
// archived 0555 or 0111 directory could not be opened afterwards.
func chmodAndSyncDir(root *os.Root, name string, mode os.FileMode) error {
	f, err := root.Open(name)
	if err != nil {
		return fmt.Errorf("open directory %q: %w", name, err)
	}
	if err := f.Chmod(mode); err != nil {
		return errors.Join(fmt.Errorf("restore directory mode %q: %w", name, err), f.Close())
	}
	if err := syncDirFile(f); err != nil {
		return errors.Join(fmt.Errorf("sync directory %q: %w", name, err), f.Close())
	}
	return f.Close()
}

// syncDirFile flushes a directory handle's metadata. Directory fsync has no
// Windows equivalent, where syncTree and syncParent are no-ops as well.
func syncDirFile(f *os.File) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	return f.Sync()
}

// requireSameLinks reports whether both passes saw exactly the same link members.
func requireSameLinks(indexed, extracted map[string]struct{}) error {
	if len(indexed) != len(extracted) {
		return fmt.Errorf("archive changed between indexing and extraction: %d link members indexed, %d extracted", len(indexed), len(extracted))
	}
	for name := range indexed {
		if _, ok := extracted[name]; !ok {
			return fmt.Errorf("archive changed between indexing and extraction: link member %q disappeared", name)
		}
	}
	return nil
}

// verifyConsumedArtifact proves that the compressed bytes one extraction pass
// consumed are exactly the signed artifact, so a source rewritten in place after
// the verified read cannot be published.
func verifyConsumedArtifact(consumed int64, sum []byte, a manifest.Artifact) error {
	if consumed != a.Size {
		return fmt.Errorf("artifact changed during extraction: consumed %d of %d bytes", consumed, a.Size)
	}
	if fmt.Sprintf("%x", sum) != a.SHA256 {
		return errors.New("artifact changed during extraction: hash mismatch")
	}
	return nil
}

// countingWriter counts the bytes written through it, which is how the extraction
// pass knows how much of the artifact it actually read.
type countingWriter struct{ n int64 }

func (c *countingWriter) Write(p []byte) (int, error) {
	c.n += int64(len(p))
	return len(p), nil
}

// scanRootfs indexes a verified archive without writing anything: member names,
// duplicates, size bounds, allowed entry types, link targets and the
// link-descendant rule.
//
// A link member (symlink or hardlink) must never contain another member beneath
// it. That child would be created physically under the resolved link, and if the
// child is itself a link its rewritten text points outside the release
// directory: "a -> /" plus "a/b -> /etc/passwd", at any nesting depth, would
// publish "<release>/b -> ../etc/passwd". The complete link set is known before
// extraction, so forward references are refused too. Recipes that need merged-usr
// alias entries (a "bin -> usr/bin" member plus "bin/..." members) must
// normalize those entries before staging.
func scanRootfs(ctx context.Context, tr *tar.Reader) (map[string]struct{}, error) {
	seen := make(map[string]struct{}, 1024)
	links := make(map[string]struct{})
	names := make([]string, 0, 1024)
	var total int64
	for count := 0; ; count++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("read archive entry: %w", err)
		}
		if count >= rootfsLimits.MaxEntries {
			return nil, fmt.Errorf("too many archive entries (limit %d)", rootfsLimits.MaxEntries)
		}
		name, err := rootfsMemberName(hdr.Name)
		if err != nil {
			return nil, err
		}
		if _, dup := seen[name]; dup {
			return nil, fmt.Errorf("duplicate archive entry %q", hdr.Name)
		}
		seen[name] = struct{}{}
		names = append(names, name)
		if hdr.Size < 0 || hdr.Size > rootfsLimits.MaxExpanded-total {
			return nil, fmt.Errorf("archive expands beyond the %d byte limit", rootfsLimits.MaxExpanded)
		}
		total += hdr.Size
		if name == "." {
			if hdr.Typeflag != tar.TypeDir {
				return nil, fmt.Errorf("archive member %q must be a directory", hdr.Name)
			}
			continue
		}
		switch hdr.Typeflag {
		case tar.TypeDir, tar.TypeReg, tar.TypeRegA:
		case tar.TypeSymlink:
			if _, err := rootfsLinkTarget(name, hdr.Linkname); err != nil {
				return nil, err
			}
			links[name] = struct{}{}
		case tar.TypeLink:
			if _, err := rootfsMemberName(hdr.Linkname); err != nil {
				return nil, fmt.Errorf("hardlink %q: %w", hdr.Name, err)
			}
			links[name] = struct{}{}
		default:
			return nil, fmt.Errorf("unsupported archive entry type %q for %q", hdr.Typeflag, hdr.Name)
		}
	}
	if len(names) == 0 {
		return nil, errors.New("empty rootfs archive")
	}
	for _, name := range names {
		if ancestor, ok := linkAncestor(links, name); ok {
			return nil, fmt.Errorf("archive entry %q is extracted beneath link %q", name, ancestor)
		}
	}
	return links, nil
}

// linkAncestor reports the nearest link member that is a parent path of name.
func linkAncestor(links map[string]struct{}, name string) (string, bool) {
	for p := path.Dir(name); p != "." && p != ""; p = path.Dir(p) {
		if _, ok := links[p]; ok {
			return p, true
		}
	}
	return "", false
}

// Implicitly created parents are recorded so stats report every directory the
// release contains.
func ensureRootfsDir(root *os.Root, name string, dirs map[string]struct{}) error {
	if name == "." || name == "" {
		return nil
	}
	if err := root.MkdirAll(name, 0700); err != nil {
		return fmt.Errorf("create directory %q: %w", name, err)
	}
	for p := name; p != "." && p != ""; p = path.Dir(p) {
		dirs[p] = struct{}{}
	}
	return nil
}

func writeRootfsFile(ctx context.Context, root *os.Root, name string, mode os.FileMode, tr *tar.Reader, size int64) (int64, error) {
	out, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return 0, fmt.Errorf("create %q: %w", name, err)
	}
	n, copyErr := io.Copy(out, &contextReader{ctx: ctx, reader: tr})
	if copyErr == nil && n != size {
		copyErr = fmt.Errorf("entry %q is truncated: %d of %d bytes", name, n, size)
	}
	modeErr := out.Chmod(mode)
	syncErr := out.Sync()
	closeErr := out.Close()
	if err := errors.Join(copyErr, modeErr, syncErr, closeErr); err != nil {
		return n, fmt.Errorf("write %q: %w", name, err)
	}
	return n, nil
}

// hasUnsafePathBytes reports whether a member name or link target carries
// bytes that are unsafe or ambiguous in a path component.
func hasUnsafePathBytes(s string) bool {
	return strings.ContainsAny(s, `\:`) || strings.IndexByte(s, 0) >= 0
}

// rootfsMemberName normalises one tar member name into a cleaned, root-relative
// slash path. Absolute names, Windows drive/UNC/backslash ambiguity, parent
// traversal and non-canonical spellings are refused. "." (the usual "./" tar
// root marker) denotes the archive root itself.
func rootfsMemberName(raw string) (string, error) {
	if raw == "" {
		return "", errors.New("empty archive member name")
	}
	if hasUnsafePathBytes(raw) {
		return "", fmt.Errorf("unsafe archive member name %q", raw)
	}
	if strings.HasPrefix(raw, "/") {
		return "", fmt.Errorf("absolute archive member name %q", raw)
	}
	name := raw
	for strings.HasPrefix(name, "./") {
		name = name[2:]
	}
	name = strings.TrimRight(name, "/")
	if name == "" || name == "." {
		return ".", nil
	}
	if !safeName(name) {
		return "", fmt.Errorf("unsafe archive member name %q", raw)
	}
	return name, nil
}

// rootfsLinkTarget converts one archive symlink target into the relative target
// written to disk.
//
// A rootfs link may be relative ("usr/bin/awk -> gawk") or absolute but in-root
// ("bin/sh -> /usr/bin/sh"). Rewriting an absolute target to the equivalent
// relative target is a deliberate choice, not a limitation of os.Root: Symlink
// stores the text without validating it, so an absolute target would be kept and
// would resolve against the host root whenever the release directory is read by
// path before it is mounted as "/". The relative form keeps the tree
// self-contained and means the same thing after mounting.
//
// Every target is resolved component by component against the link's own
// directory. A target that would traverse above the archive root is refused
// even when a naive path.Clean would fold it back inside the archive, so a
// crafted link cannot pivot through the root or through the extraction parent.
func rootfsLinkTarget(name, raw string) (string, error) {
	if raw == "" {
		return "", fmt.Errorf("symlink %q has an empty target", name)
	}
	if len(raw) > rootfsLimits.MaxTarget {
		return "", fmt.Errorf("symlink %q target exceeds %d bytes", name, rootfsLimits.MaxTarget)
	}
	if hasUnsafePathBytes(raw) {
		return "", fmt.Errorf("symlink %q has an unsafe target %q", name, raw)
	}
	base, target := path.Dir(name), raw
	if root := strings.TrimPrefix(raw, "/"); root != raw {
		// Absolute target: resolved from the archive root, never from the host
		// filesystem.
		base, target = "", root
	}
	ref, err := joinArchivePath(base, target)
	if err != nil {
		return "", fmt.Errorf("symlink %q target %q: %w", name, raw, err)
	}
	return relativeArchivePath(path.Dir(name), ref), nil
}

// joinArchivePath folds target onto base, both root-relative slash paths, while
// refusing to traverse above the archive root.
func joinArchivePath(base, target string) (string, error) {
	var parts []string
	if base != "" && base != "." {
		parts = append(parts, strings.Split(base, "/")...)
	}
	for _, part := range strings.Split(target, "/") {
		switch part {
		case "", ".":
			continue
		case "..":
			if len(parts) == 0 {
				return "", errors.New("target escapes the archive root")
			}
			parts = parts[:len(parts)-1]
		default:
			parts = append(parts, part)
		}
	}
	if len(parts) == 0 {
		return ".", nil
	}
	return strings.Join(parts, "/"), nil
}

// relativeArchivePath returns the slash path reaching ref from dir, both
// root-relative and already validated by joinArchivePath.
func relativeArchivePath(dir, ref string) string {
	if dir == "" || dir == "." {
		return ref
	}
	from := strings.Split(dir, "/")
	var to []string
	if ref != "." {
		to = strings.Split(ref, "/")
	}
	i := 0
	for i < len(from) && i < len(to) && from[i] == to[i] {
		i++
	}
	parts := make([]string, 0, len(from)-i+len(to)-i)
	for j := i; j < len(from); j++ {
		parts = append(parts, "..")
	}
	parts = append(parts, to[i:]...)
	if len(parts) == 0 {
		return "."
	}
	return strings.Join(parts, "/")
}

// openVerifiedArtifact opens archivePath, proves it is a regular file whose byte
// size and SHA-256 match the signed artifact, and rewinds the same handle so
// extraction reads exactly the bytes that were verified.
func openVerifiedArtifact(ctx context.Context, a manifest.Artifact, archivePath string) (*os.File, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	f, err := os.Open(archivePath)
	if err != nil {
		return nil, fmt.Errorf("open artifact: %w", err)
	}
	keep := false
	defer func() {
		if !keep {
			f.Close()
		}
	}()
	st, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("stat artifact: %w", err)
	}
	if !st.Mode().IsRegular() {
		return nil, errors.New("artifact is not a regular file")
	}
	if st.Size() != a.Size {
		return nil, fmt.Errorf("artifact size mismatch: have %d bytes, want %d", st.Size(), a.Size)
	}
	h := sha256.New()
	if _, err := io.Copy(h, &contextReader{ctx: ctx, reader: f}); err != nil {
		return nil, fmt.Errorf("hash artifact: %w", err)
	}
	if fmt.Sprintf("%x", h.Sum(nil)) != a.SHA256 {
		return nil, errors.New("artifact hash mismatch")
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("rewind artifact: %w", err)
	}
	keep = true
	return f, nil
}

// releaseStaging is a private directory created inside the release parent. Every
// write happens there, and commit publishes it under its final name. Any other
// outcome removes it, so a failed stage never leaves a candidate release.
type releaseStaging struct {
	parent    *os.Root
	dir       *os.Root
	name      string
	tmp       string
	live      bool
	published bool
}

func newReleaseStaging(parentDir, name string) (*releaseStaging, error) {
	if !safeName(name) {
		return nil, fmt.Errorf("invalid release directory name %q", name)
	}
	parent, err := os.OpenRoot(parentDir)
	if err != nil {
		return nil, fmt.Errorf("open release parent: %w", err)
	}
	s := &releaseStaging{parent: parent, name: name, live: true}
	if _, err := parent.Lstat(name); !errors.Is(err, os.ErrNotExist) {
		s.finish()
		return nil, errors.New("release directory already exists or cannot be inspected")
	}
	var raw [12]byte
	for attempt := 0; attempt < 64 && s.tmp == ""; attempt++ {
		if _, err := rand.Read(raw[:]); err != nil {
			s.finish()
			return nil, fmt.Errorf("allocate staging name: %w", err)
		}
		tmp := ".staging-" + hex.EncodeToString(raw[:])
		switch err := parent.Mkdir(tmp, 0700); {
		case err == nil:
			s.tmp = tmp
		case errors.Is(err, os.ErrExist):
			continue
		default:
			s.finish()
			return nil, fmt.Errorf("create staging directory: %w", err)
		}
	}
	if s.tmp == "" {
		s.finish()
		return nil, errors.New("cannot allocate a staging directory")
	}
	dir, err := parent.OpenRoot(s.tmp)
	if err != nil {
		s.finish()
		return nil, fmt.Errorf("open staging directory: %w", err)
	}
	s.dir = dir
	return s, nil
}

// commit syncs the staged tree, closes the staging root and publishes it as the
// release directory. The root must be closed first because Windows cannot
// rename a directory that is still open. finalize, when set, runs after the sync
// and before the rename, so a rootfs can apply directory modes while the tree is
// still private.
//
// It reports whether the release directory name became visible. A failure after
// that point wraps ErrPublished instead of pretending nothing was produced.
func (s *releaseStaging) commit(ctx context.Context, finalize func(*os.Root) error) (published bool, err error) {
	if err := syncTree(s.dir); err != nil {
		return false, fmt.Errorf("sync staging tree: %w", err)
	}
	if finalize != nil {
		if err := finalize(s.dir); err != nil {
			return false, err
		}
	}
	if err := s.dir.Close(); err != nil {
		return false, fmt.Errorf("close staging root: %w", err)
	}
	s.dir = nil
	if err := ctx.Err(); err != nil {
		return false, err
	}
	if _, err := s.parent.Lstat(s.name); !errors.Is(err, os.ErrNotExist) {
		return false, errors.New("release directory appeared during staging")
	}
	if err := s.parent.Rename(s.tmp, s.name); err != nil {
		return false, fmt.Errorf("publish release: %w", err)
	}
	s.published = true
	if err := syncParent(s.parent); err != nil {
		// The name is visible but its durability is unproven. The release is kept:
		// deleting a complete tree because a sync failed would be worse, and the
		// caller is told that the directory exists.
		return true, fmt.Errorf("%w: sync release parent: %w", ErrPublished, err)
	}
	return true, nil
}

// finish closes the staging handles and removes the staging directory when it
// has not been published. It is idempotent and is the only cleanup path.
//
// An error after publication is reported as ErrPublished: the release directory
// already exists, so the caller must not read the failure as "nothing was
// produced".
func (s *releaseStaging) finish() error {
	if s == nil || !s.live {
		return nil
	}
	s.live = false
	var errs []error
	if s.dir != nil {
		errs = append(errs, s.dir.Close())
		s.dir = nil
	}
	if s.parent == nil {
		return errors.Join(errs...)
	}
	if s.published {
		if err := s.parent.Close(); err != nil {
			errs = append(errs, fmt.Errorf("%w: close release parent: %w", ErrPublished, err))
		}
		return errors.Join(errs...)
	}
	if s.tmp != "" {
		if err := s.parent.RemoveAll(s.tmp); err != nil {
			errs = append(errs, fmt.Errorf("remove staging directory %s: %w", s.tmp, err))
		}
	}
	errs = append(errs, s.parent.Close())
	return errors.Join(errs...)
}
