package control

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"maa-phone-manager/manager/internal/download"
	"maa-phone-manager/manager/internal/install"
	"maa-phone-manager/manager/internal/manifest"
	"maa-phone-manager/manager/internal/store"
)

type Config struct {
	ManifestURL string               `json:"manifest_url"`
	PublicKey   string               `json:"public_key"`
	Channel     string               `json:"channel"`
	Version     string               `json:"-"`
	Device      manifest.DeviceFacts `json:"-"`
}

// Driver is deliberately not satisfied by a chroot version probe alone. It must
// lock the live task inode, validate the candidate release, and activate or stop
// a worker with a real no-game health check. The preview CLI has no driver.
//
// The controller relies on this contract:
//   - Acquire blocks until the task lock is free or ctx ends. It must not
//     busy-spin, must honour ctx cancellation, and is called exactly once per
//     switch window (the controller never retries it); its release function is
//     always called before the update returns.
//   - Validate receives the manifest of the target version, or nil for an
//     offline rollback whose manifest is no longer available. nil means the
//     release directory must be validated on its own.
//   - Activate never receives an empty directory; Stop is used instead when a
//     failed update leaves no release to fall back to.
type Driver interface {
	Acquire(context.Context) (func(), error)
	Validate(context.Context, *manifest.ChannelManifest, string) error
	Activate(context.Context, string) error
	Stop(context.Context) error
}

const (
	updateTimeout  = 30 * time.Minute
	recoverTimeout = time.Minute
	// rollbackProbe bounds the best-effort blocked-version lookup. A rollback must
	// never depend on the release source, so this only ever shortens the wait.
	rollbackProbe = 5 * time.Second
)

// idleWaitLimit bounds how long a staged candidate waits for a running MAA task
// before the update is abandoned. It stays well below updateTimeout so a stuck
// task cannot consume the whole window. Tests shrink it.
var idleWaitLimit = 10 * time.Minute

// manifestTimeout and downloadTimeout are split on purpose: a signed manifest is
// small and must fail fast, while an artifact download is large and streamed.
// Sharing one client made downloads inherit the short manifest timeout.
var (
	manifestTimeout = 30 * time.Second
	downloadTimeout = 30 * time.Minute
)

type Status struct {
	ManagerVersion string                    `json:"manager_version"`
	Configured     bool                      `json:"configured"`
	CanInstall     bool                      `json:"can_install"`
	Reason         string                    `json:"reason"`
	Versions       map[string]store.Versions `json:"versions"`
	Pending        *store.Record             `json:"pending"`
	Busy           bool                      `json:"busy"`
	Channel        string                    `json:"channel"`
}

type Controller struct {
	mu        sync.Mutex
	cfg       Config
	store     *store.Store
	driver    Driver
	client    *http.Client
	publicKey ed25519.PublicKey
	candidate *manifest.ChannelManifest
	busy      bool
	reason    string
	ctx       context.Context
	cancel    context.CancelFunc
	wg        sync.WaitGroup
	closed    bool
}

func New(cfg Config, s *store.Store, driver Driver) (*Controller, error) {
	if s == nil {
		return nil, errors.New("store required")
	}
	if cfg.Channel == "" {
		cfg.Channel = "stable"
	}
	if cfg.Version == "" {
		cfg.Version = "0.1.0"
	}
	if cfg.Channel != "stable" && cfg.Channel != "beta" {
		return nil, errors.New("invalid channel")
	}
	c := &Controller{cfg: cfg, store: s, driver: driver, reason: "尚未配置可信签名发布源"}
	c.ctx, c.cancel = context.WithCancel(context.Background())
	c.client = &http.Client{Timeout: manifestTimeout, CheckRedirect: func(r *http.Request, via []*http.Request) error {
		if len(via) >= 5 {
			return errors.New("too many redirects")
		}
		return validURL(r.URL.String())
	}}
	if cfg.ManifestURL != "" || cfg.PublicKey != "" {
		if err := validURL(cfg.ManifestURL); err != nil {
			return nil, err
		}
		key, err := base64.StdEncoding.DecodeString(cfg.PublicKey)
		if err != nil || len(key) != ed25519.PublicKeySize {
			return nil, errors.New("invalid public signing key")
		}
		c.publicKey = key
		if driver == nil {
			c.reason = "请检查发布版本；完整运行适配器尚未接入"
		} else {
			c.reason = "发布源已配置，可检查或安装更新"
		}
	}
	return c, nil
}

// StartBootstrap asynchronously activates the currently committed project release
// on daemon startup so the HTTP listener on 127.0.0.1:8088 comes up immediately
// while the chroot runtime and WebUI start in the background.
func (c *Controller) StartBootstrap() {
	c.mu.Lock()
	if c.closed || c.busy || c.driver == nil {
		c.mu.Unlock()
		return
	}
	component, version := c.bootstrapTargetLocked()
	if component == "" || version == "" {
		c.mu.Unlock()
		return
	}
	c.busy = true
	c.reason = "正在启动已安装的运行环境…"
	c.wg.Add(1)
	c.mu.Unlock()

	go func() {
		defer c.wg.Done()
		ctx, cancel := context.WithTimeout(c.ctx, recoverTimeout)
		defer cancel()
		if err := c.bootstrapRelease(ctx, component, version); err != nil {
			c.finish("运行环境启动失败：" + err.Error())
		} else {
			c.finish("运行环境已启动并就绪")
		}
	}()
}

func (c *Controller) bootstrapTargetLocked() (string, string) {
	versions := c.store.Versions()
	if v, ok := versions["maa-phone-project"]; ok && v.Current != "" {
		return "maa-phone-project", v.Current
	}
	component, current := "", ""
	for id, v := range versions {
		if id == "debian-rootfs" || id == "maa-managerd" || v.Current == "" {
			continue
		}
		if component != "" {
			return "", ""
		}
		component, current = id, v.Current
	}
	return component, current
}

func (c *Controller) bootstrapRelease(ctx context.Context, component, version string) error {
	dest, err := c.store.ReleasePath(component, version)
	if err != nil {
		return err
	}
	if _, err := os.Stat(dest); err != nil {
		return err
	}
	release, err := c.driver.Acquire(ctx)
	if err != nil {
		return err
	}
	defer release()
	if err := c.driver.Validate(ctx, nil, dest); err != nil {
		return err
	}
	return c.driver.Activate(ctx, dest)
}

func validURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("credential-free HTTPS manifest URL required")
	}
	return nil
}

func (c *Controller) Status() any {
	c.mu.Lock()
	defer c.mu.Unlock()
	// CanInstall reports capability, not readiness to switch: Apply re-checks the
	// signed manifest itself, so requiring a cached candidate would keep the local
	// UI closed on the very first check. It still requires a real runtime driver,
	// so a passing probe alone can never enable installation.
	return Status{ManagerVersion: c.cfg.Version, Configured: len(c.publicKey) > 0, CanInstall: c.driver != nil && !c.busy && !c.closed, Reason: c.reason, Versions: c.store.Versions(), Pending: c.store.Pending(), Busy: c.busy, Channel: c.cfg.Channel}
}

func (c *Controller) begin() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed || c.busy {
		return errors.New("controller busy or closed")
	}
	c.busy = true
	return nil
}
func (c *Controller) finish(reason string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.busy = false
	c.reason = reason
}

func (c *Controller) Check(ctx context.Context) error {
	if err := c.begin(); err != nil {
		return err
	}
	err := c.check(ctx)
	if err != nil {
		c.finish("发布清单检查失败；保留当前版本")
	} else if c.driver == nil {
		c.finish("发布签名有效；完整运行适配器尚未接入，不能安装")
	} else {
		c.finish("发布清单已验证，等待确认更新")
	}
	return err
}

func (c *Controller) check(ctx context.Context) error {
	c.mu.Lock()
	c.candidate = nil
	c.mu.Unlock()
	m, digest, err := c.verify(ctx)
	if err != nil {
		return err
	}
	for _, component := range m.Components {
		if component.Required {
			if _, err := m.SelectArtifact(component.ID, c.cfg.Device); err != nil {
				return err
			}
		}
	}
	if err := c.store.AcceptSequence(m.Channel, m.Sequence, digest); err != nil {
		return err
	}
	c.mu.Lock()
	c.candidate = m
	c.mu.Unlock()
	return nil
}

// verify fetches and verifies the signed manifest and returns the digest of the
// raw payload. It touches no durable state and no cached candidate, so an
// offline rollback can use it as a best-effort probe without blocking recovery.
func (c *Controller) verify(ctx context.Context) (*manifest.ChannelManifest, string, error) {
	if len(c.publicKey) == 0 {
		return nil, "", errors.New("release source not configured")
	}
	payload, err := c.fetch(ctx, c.cfg.ManifestURL, 1<<20)
	if err != nil {
		return nil, "", err
	}
	sig, err := c.fetch(ctx, c.cfg.ManifestURL+".sig", ed25519.SignatureSize)
	if err != nil {
		return nil, "", err
	}
	m, err := manifest.LoadAndVerify(payload, sig, c.publicKey)
	if err != nil {
		return nil, "", err
	}
	if m.Channel != c.cfg.Channel || !supports(c.cfg.Version, m.MinimumBootstrapVersion) {
		return nil, "", errors.New("channel or bootstrap incompatible")
	}
	return m, fmt.Sprintf("%x", sha256.Sum256(payload)), nil
}

func (c *Controller) candidateManifest() *manifest.ChannelManifest {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.candidate
}

func supports(current, minimum string) bool {
	a, ok := parseVersion(current)
	if !ok {
		return false
	}
	b, ok := parseVersion(minimum)
	if !ok {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return a[i] > b[i]
		}
	}
	return true
}

// parseVersion reads the numeric major.minor.patch prefix of a version, so the
// packaged preview suffix ("0.1.0-preview") cannot break the bootstrap gate.
func parseVersion(v string) ([3]int, bool) {
	var out [3]int
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v = v[:i]
	}
	parts := strings.Split(v, ".")
	if len(parts) != 3 {
		return out, false
	}
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 {
			return out, false
		}
		out[i] = n
	}
	return out, true
}

func (c *Controller) fetch(ctx context.Context, raw string, limit int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", raw, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, errors.New("manifest HTTP error")
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, errors.New("manifest exceeds size limit")
	}
	return b, nil
}

func (c *Controller) Apply(_ context.Context) error    { return c.start(false) }
func (c *Controller) Rollback(_ context.Context) error { return c.start(true) }

func (c *Controller) start(rollback bool) error {
	c.mu.Lock()
	if c.closed || c.busy {
		c.mu.Unlock()
		return errors.New("controller busy or closed")
	}
	if c.driver == nil {
		c.reason = "完整运行适配器尚未就绪，操作未执行"
		c.mu.Unlock()
		return errors.New("runtime not ready")
	}
	// A rollback is deliberately allowed without a configured release source: it
	// only needs local state, so a broken or withdrawn manifest cannot trap the
	// device on the version the user is trying to leave.
	if !rollback && len(c.publicKey) == 0 {
		c.reason = "签名发布源尚未配置，操作未执行"
		c.mu.Unlock()
		return errors.New("release source not configured")
	}
	c.busy = true
	c.wg.Add(1)
	c.mu.Unlock()
	go func() {
		defer c.wg.Done()
		ctx, cancel := context.WithTimeout(c.ctx, updateTimeout)
		defer cancel()
		if err := c.update(ctx, rollback); err != nil {
			c.finish("更新未完成；请查看事务状态，勿清空用户数据")
		} else {
			c.finish("更新完成，健康检查通过")
		}
	}()
	return nil
}

func (c *Controller) Close() { c.mu.Lock(); c.closed = true; c.cancel(); c.mu.Unlock(); c.wg.Wait() }

func (c *Controller) update(ctx context.Context, rollback bool) error {
	var (
		p   plan
		err error
	)
	if rollback {
		p, err = c.planRollback()
	} else {
		p, err = c.planUpdate(ctx)
	}
	if err != nil {
		return err
	}
	dest, err := c.store.ReleasePath(p.component, p.version)
	if err != nil {
		return err
	}
	if rollback {
		if err := c.blockedTarget(ctx, p.component, p.version); err != nil {
			return err
		}
	}
	if err := c.discardPending(); err != nil {
		return err
	}
	before := c.store.Versions()[p.component]
	tx, err := c.store.BeginUpdate(p.component, before.Current, p.version, p.artifact.SHA256)
	if err != nil {
		return err
	}
	succeeded := false
	defer func() {
		if !succeeded {
			_ = tx.Rollback("update_failed")
		}
	}()
	if err := tx.Advance(store.Downloading); err != nil {
		return err
	}
	if rollback {
		// The previous release is already staged and sealed, so only the journal
		// advances: a crash during the switch still stays recoverable.
		if err := tx.Advance(store.Verifying); err != nil {
			return err
		}
		if err := tx.Advance(store.Staging); err != nil {
			return err
		}
	} else if err := c.downloadAndStage(ctx, tx, p.artifact, dest); err != nil {
		return err
	}
	// The task lock is taken only for the switch window: a running MAA task must
	// not be interrupted for a download, and must never observe a half-switched
	// release.
	release, err := c.acquireTask(ctx, tx)
	if err != nil {
		return err
	}
	defer release()
	if err := c.driver.Validate(ctx, p.manifest, dest); err != nil {
		return err
	}
	if err := tx.Advance(store.Switching); err != nil {
		return err
	}
	if err := tx.Advance(store.HealthCheck); err != nil {
		return err
	}
	if err := tx.Commit(func() error { return c.driver.Activate(ctx, dest) }); err != nil {
		return errors.Join(err, c.recoverRelease(p.component, before.Current))
	}
	succeeded = true
	return nil
}

// plan is the resolved target of one update: the component and version that must
// become current, the artifact descriptor sealed with the release, and the
// manifest that authorized it (nil for an offline rollback).
type plan struct {
	component string
	version   string
	artifact  manifest.Artifact
	manifest  *manifest.ChannelManifest
}

// descriptorName is the sealed artifact descriptor inside every published release.
const descriptorName = ".maa-artifact.json"

// planUpdate resolves the target from the signed release source.
func (c *Controller) planUpdate(ctx context.Context) (plan, error) {
	if err := c.check(ctx); err != nil {
		return plan{}, err
	}
	m := c.candidateManifest()
	var project *manifest.Component
	for i := range m.Components {
		if m.Components[i].Kind != "project" {
			continue
		}
		if project != nil {
			return plan{}, errors.New("multiple project components unsupported")
		}
		project = &m.Components[i]
	}
	if project == nil {
		return plan{}, errors.New("project component missing")
	}
	a, err := m.SelectArtifact(project.ID, c.cfg.Device)
	if err != nil {
		return plan{}, err
	}
	if a.Compression != "tar.gz" {
		return plan{}, errors.New("project compression unsupported")
	}
	return plan{component: project.ID, version: project.Version, artifact: a, manifest: m}, nil
}

// planRollback resolves the target from local state only, so a withdrawn,
// corrupt or unreachable release source can never block recovery. The descriptor
// sealed with the release supplies the artifact metadata a manifest would
// otherwise provide.
func (c *Controller) planRollback() (plan, error) {
	component, version, err := c.rollbackTarget()
	if err != nil {
		return plan{}, err
	}
	dest, err := c.store.ReleasePath(component, version)
	if err != nil {
		return plan{}, err
	}
	a, err := readDescriptor(dest)
	if err != nil {
		return plan{}, err
	}
	return plan{component: component, version: version, artifact: a}, nil
}

// rollbackTarget picks the component and version to restore without any network
// access. The component id comes from the last recorded transaction; when no
// transaction is recorded it falls back to the only component with a previous
// version, and refuses an ambiguous store.
func (c *Controller) rollbackTarget() (string, string, error) {
	if id := c.store.LastComponent(); id != "" {
		if previous := c.store.Versions()[id].Previous; previous != "" {
			return id, previous, nil
		}
		return "", "", errors.New("no previous version")
	}
	component, previous := "", ""
	for id, v := range c.store.Versions() {
		if v.Previous == "" {
			continue
		}
		if component != "" {
			return "", "", errors.New("ambiguous rollback component")
		}
		component, previous = id, v.Previous
	}
	if component == "" {
		return "", "", errors.New("no previous version")
	}
	return component, previous, nil
}

// blockedTarget refuses a rollback target that a reachable release source still
// marks as emergency-blocked. It is strictly best effort: an unreachable or
// invalid manifest must never block an offline rollback.
func (c *Controller) blockedTarget(ctx context.Context, component, version string) error {
	if len(c.publicKey) == 0 || c.cfg.ManifestURL == "" {
		return nil
	}
	probeCtx, cancel := context.WithTimeout(ctx, rollbackProbe)
	defer cancel()
	m, _, err := c.verify(probeCtx)
	if err != nil {
		return nil
	}
	for _, blocked := range m.BlockedVersions {
		if blocked.Component == component && blocked.Version == version {
			return errors.New("rollback target is blocked by the release source")
		}
	}
	return nil
}

// discardPending rolls back a transaction left behind by an earlier run so a new
// one can start. The download cache and the staged release are kept for reuse.
func (c *Controller) discardPending() error {
	if c.store.Pending() == nil {
		return nil
	}
	tx, err := c.store.Resume()
	if err != nil {
		return err
	}
	return tx.Rollback("restart_retry")
}

// downloadHTTPClient copies the configured manifest client so artifact downloads
// keep its transport and redirect policy, then replaces the timeout with the long
// download budget: a large artifact must not inherit the short manifest timeout.
func (c *Controller) downloadHTTPClient() *http.Client {
	client := *c.client
	client.Timeout = downloadTimeout
	return &client
}

// downloadAndStage fetches and publishes the candidate release. The release is
// sealed with its artifact descriptor inside the private staging tree, so a
// published release directory is always complete.
func (c *Controller) downloadAndStage(ctx context.Context, tx *store.Transaction, a manifest.Artifact, dest string) error {
	parent := filepath.Dir(dest)
	if err := os.MkdirAll(parent, 0700); err != nil {
		return err
	}
	cache := filepath.Join(parent, a.SHA256+".download")
	if _, err := os.Stat(cache); errors.Is(err, os.ErrNotExist) {
		if err := (download.Client{HTTP: c.downloadHTTPClient()}).Fetch(ctx, a, cache, nil); err != nil {
			return err
		}
	} else if err != nil {
		return err
	}
	if err := tx.Advance(store.Verifying); err != nil {
		return err
	}
	if err := tx.Advance(store.Staging); err != nil {
		return err
	}
	if _, err := os.Stat(dest); errors.Is(err, os.ErrNotExist) {
		seal, err := sealedDescriptor(a)
		if err != nil {
			return err
		}
		return install.StageSealed(ctx, a, cache, dest, seal)
	} else if err != nil {
		return err
	}
	// A release of the requested version is already published from an earlier
	// attempt. Releases are immutable, so it is reused only when it carries the
	// exact artifact being installed.
	old, err := readDescriptor(dest)
	if err != nil {
		return err
	}
	if old.SHA256 != a.SHA256 {
		return errors.New("existing candidate differs")
	}
	return nil
}

func sealedDescriptor(a manifest.Artifact) (map[string][]byte, error) {
	b, err := json.Marshal(a)
	if err != nil {
		return nil, err
	}
	return map[string][]byte{descriptorName: append(b, '\n')}, nil
}

func readDescriptor(releaseDir string) (manifest.Artifact, error) {
	b, err := os.ReadFile(filepath.Join(releaseDir, descriptorName))
	if err != nil {
		return manifest.Artifact{}, err
	}
	var a manifest.Artifact
	if err := json.Unmarshal(b, &a); err != nil {
		return manifest.Artifact{}, err
	}
	return a, nil
}

// acquireTask takes the task lock for the switch window. A running task is waited
// out once, bounded by idleWaitLimit, instead of failing the whole update after
// the download. The journal records waiting_idle before the blocking call, so a
// device that never goes idle is visible in the transaction record, and a
// permanent error is returned to the caller rather than retried.
func (c *Controller) acquireTask(ctx context.Context, tx *store.Transaction) (func(), error) {
	if err := tx.Advance(store.WaitingIdle); err != nil {
		return nil, err
	}
	waitCtx, cancel := context.WithTimeout(ctx, idleWaitLimit)
	defer cancel()
	release, err := c.driver.Acquire(waitCtx)
	if err != nil {
		return nil, fmt.Errorf("task lock not acquired: %w", err)
	}
	return release, nil
}

// recoverRelease puts the runtime back on a runnable release after a failed
// activation: the previous version when one exists, otherwise the worker is
// stopped rather than left pointing at an unproven candidate.
func (c *Controller) recoverRelease(component, previous string) error {
	ctx, cancel := context.WithTimeout(context.Background(), recoverTimeout)
	defer cancel()
	if previous == "" {
		return c.driver.Stop(ctx)
	}
	path, err := c.store.ReleasePath(component, previous)
	if err != nil {
		return err
	}
	return c.driver.Activate(ctx, path)
}
