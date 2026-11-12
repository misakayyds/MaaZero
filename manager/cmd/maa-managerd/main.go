package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"maa-phone-manager/manager/internal/api"
	"maa-phone-manager/manager/internal/control"
	"maa-phone-manager/manager/internal/device"
	"maa-phone-manager/manager/internal/driver"
	"maa-phone-manager/manager/internal/install"
	"maa-phone-manager/manager/internal/manifest"
	maaruntime "maa-phone-manager/manager/internal/runtime"
	"maa-phone-manager/manager/internal/store"
)

const version = "0.1.0"

// daemonVersion is the single source for both the CLI answer and the version the
// local API reports, so the WebUI card and the packaged module cannot drift.
func daemonVersion() string { return version }

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "maa-managerd:", err)
		os.Exit(1)
	}
}

func run(args []string, out io.Writer) error {
	if len(args) == 0 {
		return errors.New("expected version, serve, open-ui or stop")
	}
	if args[0] == "version" {
		if len(args) != 1 {
			return errors.New("unexpected arguments")
		}
		_, err := fmt.Fprintln(out, daemonVersion())
		return err
	}
	if args[0] == "seed-release" {
		return runSeedRelease(args[1:], out)
	}
	if args[0] != "serve" && args[0] != "open-ui" && args[0] != "stop" {
		return errors.New("unsupported command")
	}
	flags := flag.NewFlagSet(args[0], flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	dataRoot := flags.String("data-root", "/data/local/maa-platform", "private data root")
	listen := flags.String("listen", "127.0.0.1:8088", "loopback address")
	configPath := flags.String("config", "", "public release configuration")
	assets := flags.String("assets", "", "module web assets")
	upstream := flags.String("upstream", "", "managed loopback WebUI (explicit opt-in)")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected arguments")
	}
	host, _, err := net.SplitHostPort(*listen)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		return errors.New("listen must be a loopback IP")
	}
	if runtime.GOOS != "linux" || os.Geteuid() != 0 {
		return errors.New("runtime requires rooted Android; use Go tests on the development host")
	}
	if !filepath.IsAbs(*dataRoot) {
		return errors.New("data root must be absolute")
	}
	if args[0] != "serve" {
		return command(*dataRoot, args[0])
	}
	if err := prepareDataRoot(*dataRoot); err != nil {
		return err
	}
	s, err := store.Open(*dataRoot)
	if err != nil {
		return err
	}
	defer s.Close()
	var cfg control.Config
	if *configPath != "" {
		f, err := os.Open(*configPath)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err == nil {
			defer f.Close()
			d := json.NewDecoder(io.LimitReader(f, 8193))
			d.DisallowUnknownFields()
			if err := d.Decode(&cfg); err != nil {
				return errors.New("invalid public release configuration")
			}
			if d.Decode(new(any)) != io.EOF {
				return errors.New("trailing public configuration")
			}
		}
	}
	facts, probeErr := device.Probe()
	if probeErr == nil {
		cfg.Device = facts.ManifestDeviceFacts()
	}
	cfg.Version = daemonVersion()
	rootfsPath := filepath.Join(*dataRoot, "runtime", "rootfs")
	upstreamURL := *upstream
	if upstreamURL == "" {
		upstreamURL = "http://127.0.0.1:8089"
	}
	drv, err := driver.New(driver.Config{
		DataRoot:    *dataRoot,
		RootfsPath:  rootfsPath,
		BusyboxPath: driver.ResolveBusybox(),
		UpstreamURL: upstreamURL,
	})
	if err != nil {
		return fmt.Errorf("init driver: %w", err)
	}
	defer func() {
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer stopCancel()
		_ = drv.Stop(stopCtx)
	}()

	backend, err := control.New(cfg, s, drv)
	if err != nil {
		return err
	}
	defer backend.Close()
	l, err := net.Listen("tcp", *listen)
	if err != nil {
		return err
	}
	defer l.Close()
	apiCfg := api.Config{Host: l.Addr().String(), Backend: backend, Upstream: upstreamURL}
	if *assets != "" {
		apiCfg.Assets = os.DirFS(*assets)
	}
	handler, err := api.New(apiCfg)
	if err != nil {
		return err
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	ipc, err := listenControl(*dataRoot, apiCfg.Host, handler, cancel)
	if err != nil {
		return err
	}
	defer ipc.Close()
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	done := make(chan error, 1)
	go func() { done <- server.Serve(l) }()
	backend.StartBootstrap()
	_, _ = fmt.Fprintln(out, "MAA manager preview started; authenticated local control only")
	select {
	case <-ctx.Done():
		stopCtx, stop := context.WithTimeout(context.Background(), 20*time.Second)
		defer stop()
		err = server.Shutdown(stopCtx)
		if err != nil {
			_ = server.Close()
		}
		return err
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

// prepareDataRoot creates the private data root when it does not exist yet, and
// refuses to run when an existing directory is readable by anyone else. An
// existing directory is never chmod'ed: the manager must not silently widen or
// narrow the permissions of a directory it did not create.
func prepareDataRoot(path string) error { return createDataRoot(path, os.Stat) }

// createDataRoot takes the stat function so the decision can be tested on hosts
// whose filesystem synthesizes permission bits.
func createDataRoot(path string, stat func(string) (os.FileInfo, error)) error {
	if err := os.Mkdir(path, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return fmt.Errorf("create data root: %w", err)
	}
	info, err := stat(path)
	if err != nil {
		return err
	}
	if info == nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return errors.New("data root must be a private (0700) directory owned by the manager")
	}
	return nil
}

func listenControl(root, host string, handler *api.Server, stop context.CancelFunc) (net.Listener, error) {
	path := filepath.Join(root, "control.sock")
	if st, err := os.Lstat(path); err == nil {
		if st.Mode()&os.ModeSocket == 0 {
			return nil, errors.New("control path is not a socket")
		}
		if err := os.Remove(path); err != nil {
			return nil, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	l, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0600); err != nil {
		_ = l.Close()
		return nil, err
	}
	go func() {
		for {
			conn, err := l.Accept()
			if err != nil {
				return
			}
			func() {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
				line, err := bufio.NewReader(io.LimitReader(conn, 32)).ReadString('\n')
				if err != nil {
					return
				}
				switch strings.TrimSpace(line) {
				case "OPEN":
					ticket, err := handler.Ticket()
					if err == nil {
						_, _ = io.WriteString(conn, "http://"+host+"/#ticket="+ticket+"\n")
					}
				case "STOP":
					_, _ = io.WriteString(conn, "OK\n")
					stop()
				}
			}()
		}
	}()
	return l, nil
}

func command(root, action string) error {
	conn, err := net.DialTimeout("unix", filepath.Join(root, "control.sock"), 3*time.Second)
	if err != nil {
		return errors.New("manager unavailable; open the module recovery instructions")
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	verb := "OPEN\n"
	if action == "stop" {
		verb = "STOP\n"
	}
	if _, err := io.WriteString(conn, verb); err != nil {
		return err
	}
	line, err := bufio.NewReader(io.LimitReader(conn, 1024)).ReadString('\n')
	if err != nil {
		return err
	}
	line = strings.TrimSpace(line)
	if action == "stop" {
		if line != "OK" {
			return errors.New("stop not acknowledged")
		}
		return nil
	}
	u, err := url.Parse(line)
	if err != nil || u.Scheme != "http" || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || !strings.HasPrefix(u.Fragment, "ticket=") {
		return errors.New("invalid manager entry")
	}
	// Do not print or persist the short-lived URL. Only Android receives it.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "/system/bin/am", "start", "-a", "android.intent.action.VIEW", "-d", line).Run()
}

func runSeedRelease(args []string, out io.Writer) error {
	flags := flag.NewFlagSet("seed-release", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	dataRoot := flags.String("data-root", "/data/local/maa-platform", "private data root")
	projectArchive := flags.String("project-archive", "", "local tar.gz project archive")
	projectVersion := flags.String("project-version", daemonVersion(), "project release version")
	rootfsArchive := flags.String("rootfs-archive", "", "optional local tar.gz rootfs archive")
	rootfsVersion := flags.String("rootfs-version", "12.11.0", "rootfs release version")
	replace := flags.Bool("replace", false, "replace existing project release directory")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("unexpected arguments")
	}
	if *projectArchive == "" && *rootfsArchive == "" {
		return errors.New("at least one of --project-archive or --rootfs-archive is required")
	}
	if runtime.GOOS != "linux" || os.Geteuid() != 0 {
		return errors.New("runtime requires rooted Android; use Go tests on the development host")
	}
	if !filepath.IsAbs(*dataRoot) {
		return errors.New("data root must be absolute")
	}
	if err := prepareDataRoot(*dataRoot); err != nil {
		return err
	}
	s, err := store.Open(*dataRoot)
	if err != nil {
		return err
	}
	defer s.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	if *rootfsArchive != "" {
		art, err := localTarGzArtifact(*rootfsArchive, "debian-rootfs", *rootfsVersion)
		if err != nil {
			return err
		}
		runtimeDir := filepath.Join(*dataRoot, "runtime")
		if err := os.MkdirAll(runtimeDir, 0755); err != nil {
			return fmt.Errorf("create runtime dir: %w", err)
		}
		rootfsDest := filepath.Join(runtimeDir, "rootfs")
		if _, err := os.Stat(rootfsDest); errors.Is(err, os.ErrNotExist) {
			if _, err := install.StageRootfs(ctx, art, *rootfsArchive, rootfsDest); err != nil {
				return fmt.Errorf("stage rootfs: %w", err)
			}
		} else if err != nil {
			return err
		}
		rootfsRelDir, err := s.ReleasePath("debian-rootfs", *rootfsVersion)
		if err != nil {
			return err
		}
		if err := os.MkdirAll(rootfsRelDir, 0700); err != nil {
			return err
		}
		if rawDesc, err := json.Marshal(art); err == nil {
			_ = os.WriteFile(filepath.Join(rootfsRelDir, ".maa-artifact.json"), append(rawDesc, '\n'), 0600)
		}
		if err := commitLocalSeed(s, "debian-rootfs", *rootfsVersion, art.SHA256); err != nil {
			return err
		}
	}

	if *projectArchive != "" {
		art, err := localTarGzArtifact(*projectArchive, "maa-phone-project", *projectVersion)
		if err != nil {
			return err
		}
		dest, err := s.ReleasePath("maa-phone-project", *projectVersion)
		if err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0700); err != nil {
			return err
		}
		if *replace {
			_ = os.RemoveAll(dest)
		}
		if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) {
			rawDesc, err := json.Marshal(art)
			if err != nil {
				return err
			}
			seal := map[string][]byte{".maa-artifact.json": append(rawDesc, '\n')}
			if err := install.StageSealed(ctx, art, *projectArchive, dest, seal); err != nil {
				return fmt.Errorf("stage project: %w", err)
			}
		} else if err != nil {
			return err
		}
		if err := maaruntime.ScanProjectDir(dest); err != nil {
			return fmt.Errorf("validate project: %w", err)
		}
		if err := commitLocalSeed(s, "maa-phone-project", *projectVersion, art.SHA256); err != nil {
			return err
		}
	}

	_, err = fmt.Fprintln(out, "SEED_OK")
	return err
}

func localTarGzArtifact(archivePath, component, ver string) (manifest.Artifact, error) {
	f, err := os.Open(archivePath)
	if err != nil {
		return manifest.Artifact{}, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return manifest.Artifact{}, err
	}
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return manifest.Artifact{}, err
	}
	return manifest.Artifact{
		URL:         fmt.Sprintf("https://local.invalid/%s/%s/%s-%s.tar.gz", component, ver, component, ver),
		SHA256:      hex.EncodeToString(h.Sum(nil)),
		Size:        info.Size(),
		MediaType:   "application/gzip",
		Compression: "tar.gz",
	}, nil
}

func commitLocalSeed(s *store.Store, component, ver, digest string) error {
	if s.Pending() != nil {
		tx, err := s.Resume()
		if err != nil {
			return err
		}
		if err := tx.Rollback("seed_reset"); err != nil {
			return err
		}
	}
	before := s.Versions()[component]
	if before.Current == ver {
		return nil
	}
	tx, err := s.BeginUpdate(component, before.Current, ver, digest)
	if err != nil {
		return err
	}
	for _, st := range []store.Stage{
		store.Downloading,
		store.Verifying,
		store.Staging,
		store.WaitingIdle,
		store.Switching,
		store.HealthCheck,
	} {
		if err := tx.Advance(st); err != nil {
			_ = tx.Rollback("seed_failed")
			return err
		}
	}
	if err := tx.Commit(func() error { return nil }); err != nil {
		_ = tx.Rollback("seed_failed")
		return err
	}
	return nil
}
