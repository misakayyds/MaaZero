package control

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"maa-phone-manager/manager/internal/manifest"
	"maa-phone-manager/manager/internal/store"
)

// runtimeFixture models the runtime adapter contract: Acquire blocks until the
// task lock is free or the context ends, and it is never busy-spun or retried.
// Every field is guarded because the controller drives it from the update
// goroutine while the test observes it.
type runtimeFixture struct {
	mu              sync.Mutex
	active          string
	locked          bool
	failVersion     string
	stops           int
	acquires        int
	acquireAttempts int
	acquireFailures int
	blockAcquire    bool
	onAcquire       func()
	activated       []string
	validateNil     bool
	activateStarted chan struct{}
	activateGate    chan struct{}
	activateOnce    sync.Once
}

func (d *runtimeFixture) Acquire(ctx context.Context) (func(), error) {
	d.mu.Lock()
	d.acquireAttempts++
	if d.acquireFailures > 0 {
		d.acquireFailures--
		d.mu.Unlock()
		return nil, errors.New("fixture acquire failure")
	}
	block := d.blockAcquire
	if !block {
		if d.locked {
			d.mu.Unlock()
			return nil, errors.New("fixture busy")
		}
		d.locked = true
		d.acquires++
	}
	hook := d.onAcquire
	d.mu.Unlock()
	if hook != nil {
		hook()
	}
	if block {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return func() {
		d.mu.Lock()
		d.locked = false
		d.mu.Unlock()
	}, nil
}

func (d *runtimeFixture) Validate(_ context.Context, m *manifest.ChannelManifest, path string) error {
	d.mu.Lock()
	d.validateNil = m == nil
	d.mu.Unlock()
	_, err := os.ReadFile(filepath.Join(path, "version.txt"))
	return err
}

func (d *runtimeFixture) Activate(ctx context.Context, path string) error {
	d.mu.Lock()
	locked, started, gate := d.locked, d.activateStarted, d.activateGate
	d.activated = append(d.activated, path)
	d.mu.Unlock()
	if !locked {
		return errors.New("task lock not held")
	}
	if started != nil {
		d.activateOnce.Do(func() { close(started) })
	}
	if gate != nil {
		select {
		case <-gate:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	b, err := os.ReadFile(filepath.Join(path, "version.txt"))
	if err != nil {
		return err
	}
	d.mu.Lock()
	fail := d.failVersion
	d.mu.Unlock()
	if string(b) == fail {
		return errors.New("fixture health failure")
	}
	d.mu.Lock()
	d.active = string(b)
	d.mu.Unlock()
	return nil
}

func (d *runtimeFixture) Stop(context.Context) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.stops++
	d.active = ""
	return nil
}

func (d *runtimeFixture) setFail(version string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.failVersion = version
}

func (d *runtimeFixture) setAcquireHook(hook func()) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.onAcquire = hook
}

func (d *runtimeFixture) blockAcquires() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.blockAcquire = true
}

func (d *runtimeFixture) failNextAcquires(n int) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.acquireFailures = n
}

func (d *runtimeFixture) gateActivation() (chan struct{}, chan struct{}) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.activateStarted = make(chan struct{})
	d.activateGate = make(chan struct{})
	return d.activateStarted, d.activateGate
}

func (d *runtimeFixture) activeVersion() string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.active
}

func (d *runtimeFixture) stopCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.stops
}

func (d *runtimeFixture) acquireCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.acquires
}

func (d *runtimeFixture) acquireAttemptCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.acquireAttempts
}

func (d *runtimeFixture) activatedPaths() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.activated...)
}

func (d *runtimeFixture) validateSawNilManifest() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.validateNil
}

// releaseFixture serves a signed channel manifest whose project artifact is a
// real tar.gz archive, so the controller runs the published download and staging
// path instead of a stub.
type releaseFixture struct {
	t             *testing.T
	mu            sync.Mutex
	manifest      manifest.ChannelManifest
	archive       bytes.Buffer
	priv          ed25519.PrivateKey
	pub           ed25519.PublicKey
	delay         time.Duration
	tampered      bool
	server        *httptest.Server
	serverRunning bool
}

func newReleaseFixture(t *testing.T) *releaseFixture {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile("../../../manifest/example-manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var m manifest.ChannelManifest
	if json.Unmarshal(raw, &m) != nil {
		t.Fatal("fixture manifest")
	}
	m.Components = []manifest.Component{m.Components[1]}
	m.Sequence = 5
	f := &releaseFixture{t: t, manifest: m, priv: priv, pub: pub}
	f.server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/stable.json":
			_, _ = w.Write(f.payload())
		case "/stable.json.sig":
			sig := ed25519.Sign(f.priv, f.payload())
			if f.tamperSignature() {
				sig[0] ^= 1
			}
			_, _ = w.Write(sig)
		default:
			if delay := f.artifactDelay(); delay > 0 {
				time.Sleep(delay)
			}
			_, _ = w.Write(f.archiveBytes())
		}
	}))
	f.serverRunning = true
	t.Cleanup(func() {
		if f.serverRunning {
			f.server.Close()
		}
	})
	f.publish("0.1.0")
	return f
}

func (f *releaseFixture) payload() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	b, err := json.Marshal(f.manifest)
	if err != nil {
		f.t.Fatal(err)
	}
	return b
}

func (f *releaseFixture) archiveBytes() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]byte(nil), f.archive.Bytes()...)
}

func (f *releaseFixture) artifactDelay() time.Duration {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.delay
}

func (f *releaseFixture) tamperSignature() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.tampered
}

func (f *releaseFixture) setArtifactDelay(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.delay = d
}

func (f *releaseFixture) setTampered(tampered bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.tampered = tampered
}

func (f *releaseFixture) setSequence(sequence uint64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.manifest.Sequence = sequence
}

// publish rebuilds the served archive for one version and points the manifest at
// it, bumping the sequence the store tracks.
func (f *releaseFixture) publish(version string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.archive.Reset()
	gz := gzip.NewWriter(&f.archive)
	tw := tar.NewWriter(gz)
	if err := tw.WriteHeader(&tar.Header{Name: "version.txt", Mode: 0600, Size: int64(len(version))}); err != nil {
		f.t.Fatal(err)
	}
	if _, err := tw.Write([]byte(version)); err != nil {
		f.t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		f.t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		f.t.Fatal(err)
	}
	f.manifest.Sequence++
	f.manifest.Components[0].Version = version
	f.manifest.Components[0].Artifacts[0].SHA256 = fmt.Sprintf("%x", sha256.Sum256(f.archive.Bytes()))
	f.manifest.Components[0].Artifacts[0].Size = int64(f.archive.Len())
	f.manifest.Components[0].Artifacts[0].Compression = "tar.gz"
	f.manifest.Components[0].Artifacts[0].MediaType = "application/gzip"
	f.manifest.Components[0].Artifacts[0].URL = f.server.URL + "/" + version + "/project.tar.gz"
}

func (f *releaseFixture) block(version string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.manifest.BlockedVersions = append(f.manifest.BlockedVersions, manifest.BlockedVersion{Component: f.projectIDLocked(), Version: version, Reason: "fixture"})
}

func (f *releaseFixture) projectID() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.projectIDLocked()
}

func (f *releaseFixture) projectIDLocked() string { return f.manifest.Components[0].ID }

func (f *releaseFixture) stopServer() {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.serverRunning {
		f.server.Close()
		f.serverRunning = false
	}
}

func (f *releaseFixture) start(d *runtimeFixture) (*Controller, *store.Store) {
	f.t.Helper()
	s, err := store.Open(f.t.TempDir())
	if err != nil {
		f.t.Fatal(err)
	}
	f.t.Cleanup(func() { _ = s.Close() })
	c, err := New(f.config(), s, d)
	if err != nil {
		f.t.Fatal(err)
	}
	f.t.Cleanup(c.Close)
	c.client = f.server.Client()
	return c, s
}

func (f *releaseFixture) config() Config {
	return Config{
		ManifestURL: f.server.URL + "/stable.json",
		PublicKey:   base64.StdEncoding.EncodeToString(f.pub),
		Device:      manifest.DeviceFacts{Architecture: "arm64-v8a", AndroidAPI: 30, RootManager: "magisk"},
	}
}

func (f *releaseFixture) await(c *Controller) {
	f.t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for c.Status().(Status).Busy {
		if time.Now().After(deadline) {
			f.t.Fatal("update worker stuck")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func (f *releaseFixture) apply(c *Controller) {
	f.t.Helper()
	if err := c.Apply(context.Background()); err != nil {
		f.t.Fatal(err)
	}
	f.await(c)
}

func (f *releaseFixture) rollback(c *Controller) {
	f.t.Helper()
	if err := c.Rollback(context.Background()); err != nil {
		f.t.Fatal(err)
	}
	f.await(c)
}

func (f *releaseFixture) releaseDir(s *store.Store, version string) string {
	f.t.Helper()
	path, err := s.ReleasePath(f.projectID(), version)
	if err != nil {
		f.t.Fatal(err)
	}
	return path
}

func TestSignedInstallThenFailedActivationRollsBack(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	f.apply(c)
	if got := s.Versions()[f.projectID()].Current; got != "0.1.0" || d.activeVersion() != "0.1.0" {
		t.Fatalf("initial install failed: %+v", c.Status())
	}
	f.publish("0.2.0")
	d.setFail("0.2.0")
	f.apply(c)
	if got := s.Versions()[f.projectID()].Current; got != "0.1.0" || d.activeVersion() != "0.1.0" {
		t.Fatalf("rollback failed: %+v", c.Status())
	}
	if s.Pending() != nil {
		t.Fatal("failed update left a pending transaction")
	}
}

func TestStatusStaysResponsiveDuringActivation(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, _ := f.start(d)
	f.apply(c)
	f.publish("0.2.0")
	started, gate := d.gateActivation()
	if err := c.Apply(context.Background()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-time.After(10 * time.Second):
		t.Fatal("activation never started")
	}
	status := make(chan Status, 1)
	go func() { status <- c.Status().(Status) }()
	select {
	case st := <-status:
		if !st.Busy {
			t.Fatal("status lost the busy flag")
		}
		if st.CanInstall {
			t.Fatal("busy manager offered installation")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("status blocked while the runtime health check was running")
	}
	close(gate)
	f.await(c)
	if d.activeVersion() != "0.2.0" {
		t.Fatalf("activation did not complete: %+v", c.Status())
	}
}

func TestRollbackWorksOfflineAndUsesTheSealedDescriptor(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	f.apply(c)
	f.publish("0.2.0")
	f.apply(c)
	raw, err := os.ReadFile(filepath.Join(f.releaseDir(s, "0.1.0"), ".maa-artifact.json"))
	if err != nil {
		t.Fatalf("sealed descriptor missing: %v", err)
	}
	var sealed manifest.Artifact
	if err := json.Unmarshal(raw, &sealed); err != nil || sealed.SHA256 == "" {
		t.Fatalf("sealed descriptor unusable: %q %v", raw, err)
	}
	// The release source disappears before the rollback.
	f.stopServer()
	f.rollback(c)
	if got := s.Versions()[f.projectID()]; got.Current != "0.1.0" || got.Previous != "0.2.0" {
		t.Fatalf("offline rollback did not switch: %+v", got)
	}
	if d.activeVersion() != "0.1.0" {
		t.Fatalf("offline rollback did not activate: %q", d.activeVersion())
	}
	if !d.validateSawNilManifest() {
		t.Fatal("offline rollback still depended on a manifest")
	}
}

func TestRollbackRefusesAnEmergencyBlockedTarget(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	f.apply(c)
	f.publish("0.2.0")
	f.apply(c)
	f.block("0.1.0")
	f.rollback(c)
	if got := s.Versions()[f.projectID()].Current; got != "0.2.0" {
		t.Fatalf("blocked rollback target was restored: %q", got)
	}
	if d.activeVersion() != "0.2.0" {
		t.Fatalf("blocked rollback activated the target: %q", d.activeVersion())
	}
}

// acquireObservation records what the store looked like at the moment the task
// lock was requested, which is what proves the journal was written first and the
// task lock was not taken before the candidate existed.
type acquireObservation struct {
	staged  bool
	stage   store.Stage
	current string
	pending bool
}

func TestTaskLockIsTakenOnlyAfterStagingAndJournal(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	f.apply(c)
	f.publish("0.2.0")
	var mu sync.Mutex
	var seen []acquireObservation
	f.publish("0.2.0")
	dest := f.releaseDir(s, "0.2.0")
	d.setAcquireHook(func() {
		_, err := os.Stat(dest)
		pending := s.Pending()
		observation := acquireObservation{staged: err == nil, current: s.Versions()[f.projectID()].Current, pending: pending != nil}
		if pending != nil {
			observation.stage = pending.Stage
		}
		mu.Lock()
		seen = append(seen, observation)
		mu.Unlock()
	})
	f.apply(c)
	mu.Lock()
	defer mu.Unlock()
	if len(seen) == 0 {
		t.Fatal("task lock was never requested")
	}
	for _, observation := range seen {
		if !observation.staged {
			t.Fatal("task lock was requested before the candidate release was published")
		}
		if !observation.pending || observation.stage != store.WaitingIdle {
			t.Fatalf("task lock was requested before journalling %s: %+v", store.WaitingIdle, observation)
		}
		if observation.current != "0.1.0" {
			t.Fatalf("version pointers already switched before the lock: %q", observation.current)
		}
	}
}

func TestBusyTaskWaitsOnceThenAbandonsWithoutSwitching(t *testing.T) {
	oldLimit := idleWaitLimit
	idleWaitLimit = 300 * time.Millisecond
	defer func() { idleWaitLimit = oldLimit }()
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	f.apply(c)
	f.publish("0.2.0")
	d.blockAcquires()
	observed := make(chan acquireObservation, 1)
	d.setAcquireHook(func() {
		pending := s.Pending()
		observation := acquireObservation{pending: pending != nil, current: s.Versions()[f.projectID()].Current}
		if pending != nil {
			observation.stage = pending.Stage
		}
		select {
		case observed <- observation:
		default:
		}
	})
	started := time.Now()
	f.apply(c)
	elapsed := time.Since(started)
	select {
	case observation := <-observed:
		if observation.stage != store.WaitingIdle {
			t.Fatalf("wait was not journalled as %s: %+v", store.WaitingIdle, observation)
		}
	default:
		t.Fatal("task lock was never requested")
	}
	if elapsed < idleWaitLimit {
		t.Fatalf("busy task was not waited out: %v", elapsed)
	}
	if elapsed > idleWaitLimit+10*time.Second {
		t.Fatalf("wait was not bounded by the idle limit: %v", elapsed)
	}
	if got := s.Versions()[f.projectID()].Current; got != "0.1.0" || d.activeVersion() != "0.1.0" {
		t.Fatalf("busy device was switched anyway: %+v", c.Status())
	}
	if s.Pending() != nil {
		t.Fatal("abandoned update left a pending transaction")
	}
}

func TestPermanentAcquireErrorIsNotRetried(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	f.apply(c)
	f.publish("0.2.0")
	// The adapter reports a permanent failure instead of waiting; the controller
	// must surface it once instead of retrying the same error.
	d.failNextAcquires(5)
	f.apply(c)
	if got := d.acquireAttemptCount(); got != 2 {
		t.Fatalf("acquire attempts = %d, want 2 (one per update)", got)
	}
	if got := s.Versions()[f.projectID()].Current; got != "0.1.0" || d.activeVersion() != "0.1.0" {
		t.Fatalf("permanent error switched the release: %+v", c.Status())
	}
}

func TestFailedFirstInstallStopsTheWorker(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, s := f.start(d)
	d.setFail("0.1.0")
	f.apply(c)
	if got := s.Versions()[f.projectID()].Current; got != "" {
		t.Fatalf("failed first install became current: %q", got)
	}
	if d.stopCount() != 1 || d.activeVersion() != "" {
		t.Fatalf("worker was not stopped: stops=%d active=%q", d.stopCount(), d.activeVersion())
	}
	for _, path := range d.activatedPaths() {
		if path == "" {
			t.Fatal("activation was called with an empty release path")
		}
	}
}

func TestRollbackWithoutAnyInstalledVersionFails(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, _ := f.start(d)
	f.rollback(c)
	if d.acquireCount() != 0 {
		t.Fatal("rollback without a previous version took the task lock")
	}
	if d.stopCount() != 0 {
		t.Fatal("rollback without a previous version touched the runtime")
	}
}

func TestArtifactDownloadKeepsItsOwnTimeout(t *testing.T) {
	oldManifest, oldDownload := manifestTimeout, downloadTimeout
	manifestTimeout, downloadTimeout = 150*time.Millisecond, 10*time.Second
	defer func() {
		manifestTimeout, downloadTimeout = oldManifest, oldDownload
	}()
	f := newReleaseFixture(t)
	f.setArtifactDelay(400 * time.Millisecond)
	d := &runtimeFixture{}
	c, s := f.start(d)
	// Production shape: the manifest client carries the short timeout.
	c.client.Timeout = manifestTimeout
	if got := c.downloadHTTPClient().Timeout; got != downloadTimeout {
		t.Fatalf("download timeout = %v, want %v", got, downloadTimeout)
	}
	if c.downloadHTTPClient() == c.client {
		t.Fatal("downloads must not reuse the manifest client")
	}
	f.apply(c)
	if got := s.Versions()[f.projectID()].Current; got != "0.1.0" {
		t.Fatalf("artifact slower than the manifest timeout was not installed: %q", got)
	}
}

func TestUnconfiguredCannotApply(t *testing.T) {
	s, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	c, err := New(Config{}, s, nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.Status().(Status).CanInstall {
		t.Fatal("unconfigured install enabled")
	}
	if c.Apply(context.Background()) == nil {
		t.Fatal("unconfigured apply")
	}
	if c.Check(context.Background()) == nil {
		t.Fatal("unconfigured check")
	}
	if c.Rollback(context.Background()) == nil {
		t.Fatal("rollback without a runtime driver")
	}
}

func TestSignedCheckRejectsReplayAndTampering(t *testing.T) {
	f := newReleaseFixture(t)
	c, _ := f.start(&runtimeFixture{})
	if err := c.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !c.Status().(Status).Configured {
		t.Fatal("not configured")
	}
	// A verified manifest alone must not enable installation: the CLI runs with a
	// nil driver and has to stay in recovery-only mode.
	s2, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	driverless, err := New(f.config(), s2, nil)
	if err != nil {
		t.Fatal(err)
	}
	driverless.client = f.server.Client()
	if err := driverless.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	if driverless.Status().(Status).CanInstall {
		t.Fatal("verified manifest alone enabled install")
	}
	f.setSequence(1)
	if err := c.Check(context.Background()); err == nil {
		t.Fatal("replay accepted")
	}
	f.setSequence(99)
	f.setTampered(true)
	if err := c.Check(context.Background()); err == nil {
		t.Fatal("invalid signature accepted")
	}
}

func TestCanInstallReportsCapabilityBeforeTheFirstCheck(t *testing.T) {
	f := newReleaseFixture(t)
	d := &runtimeFixture{}
	c, _ := f.start(d)
	status := c.Status().(Status)
	if !status.Configured {
		t.Fatal("configured release source not reported")
	}
	// The local UI gates every button on configured && can_install, and Apply runs
	// its own check, so a verified candidate must not be a precondition here.
	if !status.CanInstall {
		t.Fatal("configured manager with a runtime driver did not expose install capability")
	}
	if err := c.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !c.Status().(Status).CanInstall {
		t.Fatal("check cleared install capability")
	}
}

func TestCanInstallRequiresARuntimeDriverNotAProbe(t *testing.T) {
	f := newReleaseFixture(t)
	driverlessStore, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer driverlessStore.Close()
	driverless, err := New(f.config(), driverlessStore, nil)
	if err != nil {
		t.Fatal(err)
	}
	driverless.client = f.server.Client()
	if err := driverless.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
	status := driverless.Status().(Status)
	if !status.Configured {
		t.Fatal("configured release source not reported")
	}
	if status.CanInstall {
		t.Fatal("a verified manifest without a runtime driver enabled installation")
	}
	if err := driverless.Apply(context.Background()); err == nil {
		t.Fatal("driverless apply accepted")
	}

	// An unconfigured manager keeps the local UI closed through Configured even
	// though it has a runtime driver; distinguishing that from an offline
	// capability gap is still an open UI contract.
	unconfiguredStore, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer unconfiguredStore.Close()
	unconfigured, err := New(Config{}, unconfiguredStore, &runtimeFixture{})
	if err != nil {
		t.Fatal(err)
	}
	bare := unconfigured.Status().(Status)
	if bare.Configured {
		t.Fatal("unconfigured manager reported a release source")
	}
	if !bare.CanInstall {
		t.Fatal("capability flag must not depend on the release source")
	}
	if err := unconfigured.Apply(context.Background()); err == nil {
		t.Fatal("apply without a release source accepted")
	}
}

func TestVersionGateAcceptsPackagedPreview(t *testing.T) {
	for _, tc := range []struct {
		current, minimum string
		want             bool
	}{
		{"0.1.0-preview", "0.1.0", true},
		{"0.1.0-preview.3", "0.1.0", true},
		{"0.1.0+build.7", "0.1.0", true},
		{"0.1.0", "0.1.0", true},
		{"0.1.0-preview", "0.2.0", false},
		{"0.1", "0.1.0", false},
		{"", "0.1.0", false},
		{"x.y.z", "0.1.0", false},
	} {
		if got := supports(tc.current, tc.minimum); got != tc.want {
			t.Errorf("supports(%q, %q) = %v, want %v", tc.current, tc.minimum, got, tc.want)
		}
	}
}
