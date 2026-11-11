// Package download fetches already-authorized artifacts. It does not establish
// trust: the caller must verify the signed manifest and select compatibility.
package download

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"maa-phone-manager/manager/internal/manifest"
)

type Progress struct{ Bytes, Total int64 }
type Client struct{ HTTP *http.Client }

var validHash = regexp.MustCompile(`^[a-f0-9]{64}$`)

func secureURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" {
		return errors.New("artifact URL must be credential-free HTTPS")
	}
	return nil
}

func Fetch(ctx context.Context, a manifest.Artifact, destination string, progress func(Progress)) error {
	return (Client{}).Fetch(ctx, a, destination, progress)
}

// Fetch never overwrites an existing destination. A .part may contain bytes from
// an older attempt: all bytes are hashed before promotion, including the prefix.
// The caller serializes access and owns a private destination parent directory.
func (c Client) Fetch(ctx context.Context, a manifest.Artifact, destination string, progress func(Progress)) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if a.Size <= 0 || a.Size > 2<<30 || !validHash.MatchString(a.SHA256) {
		return errors.New("invalid artifact size or hash")
	}
	urls := append([]string{a.URL}, a.Mirrors...)
	for _, raw := range urls {
		if err := secureURL(raw); err != nil {
			return err
		}
	}
	if len(urls) > 9 {
		return errors.New("too many mirrors")
	}
	if _, err := os.Lstat(destination); err == nil {
		return errors.New("destination already exists")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	root, err := os.OpenRoot(filepath.Dir(destination))
	if err != nil {
		return err
	}
	defer root.Close()
	name := filepath.Base(destination)
	part := name + ".part"
	if st, err := root.Lstat(part); err == nil {
		if !st.Mode().IsRegular() {
			return errors.New("partial file is not regular")
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	f, err := root.OpenFile(part, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	client := http.Client{Timeout: 30 * time.Minute}
	if c.HTTP != nil {
		client = *c.HTTP
		if client.Timeout == 0 {
			client.Timeout = 30 * time.Minute
		}
	}
	previousRedirect := client.CheckRedirect
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 5 {
			return errors.New("too many redirects")
		}
		if err := secureURL(req.URL.String()); err != nil {
			return err
		}
		if previousRedirect != nil {
			return previousRedirect(req, via)
		}
		return nil
	}
	var last error
	for _, raw := range urls {
		if err := ctx.Err(); err != nil {
			return err
		}
		last = transfer(ctx, &client, raw, f, a, progress)
		if last != nil {
			continue
		}
		if err := f.Sync(); err != nil {
			return err
		}
		if err := f.Close(); err != nil {
			return err
		}
		// The private-directory/single-operation contract excludes a writer race.
		if _, err := root.Lstat(name); !errors.Is(err, os.ErrNotExist) {
			return errors.New("destination appeared during download")
		}
		return root.Rename(part, name)
	}
	return fmt.Errorf("all artifact sources failed: %w", last)
}

func transfer(ctx context.Context, client *http.Client, raw string, f *os.File, a manifest.Artifact, progress func(Progress)) error {
	st, err := f.Stat()
	if err != nil {
		return err
	}
	offset := st.Size()
	if offset >= a.Size {
		if offset == a.Size && verify(ctx, f, a) == nil {
			if progress != nil {
				progress(Progress{a.Size, a.Size})
			}
			return nil
		}
		if err := f.Truncate(0); err != nil {
			return err
		}
		offset = 0
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept-Encoding", "identity")
	if offset > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", offset))
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.Header.Get("Content-Encoding") != "" && resp.Header.Get("Content-Encoding") != "identity" {
		return errors.New("unexpected content encoding")
	}
	switch resp.StatusCode {
	case http.StatusOK:
		if err := f.Truncate(0); err != nil {
			return err
		}
		offset = 0
	case http.StatusPartialContent:
		if resp.Header.Get("Content-Range") != fmt.Sprintf("bytes %d-%d/%d", offset, a.Size-1, a.Size) {
			return errors.New("unexpected content range")
		}
	default:
		return fmt.Errorf("artifact HTTP status %d", resp.StatusCode)
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return err
	}
	r := io.LimitReader(resp.Body, a.Size-offset+1)
	buf := make([]byte, 64<<10)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		n, readErr := r.Read(buf)
		if n > 0 {
			if offset+int64(n) > a.Size {
				_ = f.Truncate(0)
				return errors.New("artifact exceeds declared size")
			}
			if _, err := f.Write(buf[:n]); err != nil {
				return err
			}
			offset += int64(n)
			if progress != nil {
				progress(Progress{offset, a.Size})
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return readErr
		}
	}
	if offset != a.Size {
		return errors.New("truncated artifact")
	}
	if err := verify(ctx, f, a); err != nil {
		_ = f.Truncate(0)
		return err
	}
	return nil
}

func verify(ctx context.Context, f *os.File, a manifest.Artifact) error {
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return err
	}
	h := sha256.New()
	buf := make([]byte, 64<<10)
	var count int64
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		n, err := f.Read(buf)
		if n > 0 {
			_, _ = h.Write(buf[:n])
			count += int64(n)
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
	}
	if count != a.Size || fmt.Sprintf("%x", h.Sum(nil)) != a.SHA256 {
		return errors.New("artifact hash mismatch")
	}
	return nil
}
