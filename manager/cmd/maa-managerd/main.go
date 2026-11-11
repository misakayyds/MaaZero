package main

import (
	"bufio"
	"context"
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
	"maa-phone-manager/manager/internal/store"
)

const version = "0.1.0-preview"

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
	// Full unattended-runtime activation is not yet verified. Do not enable
	// installation merely because Probe or a chroot version command succeeded.
	backend, err := control.New(cfg, s, nil)
	if err != nil {
		return err
	}
	defer backend.Close()
	l, err := net.Listen("tcp", *listen)
	if err != nil {
		return err
	}
	defer l.Close()
	apiCfg := api.Config{Host: l.Addr().String(), Backend: backend, Upstream: *upstream}
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
