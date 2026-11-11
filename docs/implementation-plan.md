# MAA Phone Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Root-manager-installed phone control plane that downloads, installs, supervises, updates, and rolls back the existing MAA Android chroot stack through a beginner-friendly local WebUI.

**Architecture:** A minimal Magisk-style module starts a static ARM64 `maa-managerd` binary and retains rescue capability. The daemon owns signed-manifest verification, versioned component storage, installation transactions, process supervision, and a localhost API; the WebUI is an unprivileged client of that API. Existing MAA scripts remain the execution worker during the first migration stages.

**Tech Stack:** Go static ARM64 binary, POSIX/BusyBox shell module adapters, JSON Schema 2020-12 manifests, embedded HTML/CSS/JavaScript WebUI, GitHub Actions and GitHub Releases.

## Global Constraints

- User-approved UI scope (2026-09-12): reuse the existing MAA WebUI and add only an "关于与模块设置" section. Do not rebuild navigation, move existing plans/cultivation into another console, or duplicate their configuration stores. A minimal standalone bootstrap/recovery page is only for unavailable runtime states.
- Include offline user help and a maintainer release guide; mark screenshots and installation instructions as unverified until the actual module is available.
- Initial target architecture is `arm64-v8a` only.
- Production manifests use immutable version URLs, SHA-256, exact byte size, and an Ed25519 signature.
- Stable is the default channel; beta requires explicit user opt-in.
- The daemon listens on loopback only and never accepts arbitrary shell commands or download URLs.
- User data under `/data/local/maa-platform/data/` is never replaced by module or component upgrades.
- No installation or update verification may start a complete MAA daily task without explicit user approval.
- Existing `.codex_remote_maa/` deployed files remain unchanged until a migration task explicitly names and verifies them.

---

### Task 1: Manifest contract and verifier

**Files:**
- Create: `maa-phone-manager/manager/go.mod`
- Create: `maa-phone-manager/manager/internal/manifest/model.go`
- Create: `maa-phone-manager/manager/internal/manifest/verify.go`
- Create: `maa-phone-manager/manager/internal/manifest/verify_test.go`
- Modify: `maa-phone-manager/manifest/manifest.schema.json`
- Test: `maa-phone-manager/manager/internal/manifest/verify_test.go`

**Interfaces:**
- Produces: `manifest.LoadAndVerify(payload []byte, signature []byte, publicKey ed25519.PublicKey) (*manifest.ChannelManifest, error)`.
- Produces: `(*manifest.ChannelManifest).SelectArtifact(component string, device manifest.DeviceFacts) (manifest.Artifact, error)`.
- Consumes: the version-1 fields documented by `manifest/manifest.schema.json`.

The executable entrypoint is intentionally deferred until the local API in Task 5; Task 1 is a dependency-free verification library and does not expose a partial daemon CLI.

- [x] **Step 1: Write table-driven tests for signature failure, SHA-256 format, duplicate component IDs, unsupported ABI, Android API boundaries, Root-manager boundaries, and immutable URL rejection.**
- [x] **Step 2: Run `go test ./internal/manifest -v` from `maa-phone-manager/manager` and verify the tests fail because the package is not implemented.** Red evidence: `undefined: LoadAndVerify`, `undefined: DeviceFacts`.
- [x] **Step 3: Implement the manifest structs, strict JSON decoding with unknown-field rejection, Ed25519 verification, semantic validation, and artifact selection.** The completed verifier also enforces a 1 MiB payload ceiling, signed monotonic `sequence`, duplicate-list rejection, exact component-version URLs, and emergency blocked versions.
- [x] **Step 4: Run `go test ./internal/manifest -v` and verify all manifest tests pass.** Completed with Go 1.26.5; the checked-in example manifest is included in the test suite.
- [x] **Step 5: Run `go vet ./...` and record the exact output in the task handoff.** Completed with exit code 0 and no findings.

### Task 2: Versioned storage and transaction state machine

**Files:**
- Create: `maa-phone-manager/manager/internal/store/layout.go`
- Create: `maa-phone-manager/manager/internal/store/transaction.go`
- Create: `maa-phone-manager/manager/internal/store/transaction_test.go`
- Create: `maa-phone-manager/manager/internal/store/testdata/`
- Test: `maa-phone-manager/manager/internal/store/transaction_test.go`

**Interfaces:**
- Consumes: verified `manifest.Artifact` values from Task 1.
- Produces: `store.Open(root)` and `(*store.Store).BeginUpdate(component, fromVersion, toVersion, sha256 string)`.
- Produces: `(*store.Transaction).Advance(stage store.Stage) error`, `Commit(health func() error) error`, and `Rollback(reasonCode string) error`.
- Produces: atomic logical current/previous pointers in one `transactions/state.json`, consumed through `Versions()`; these are not filesystem symlinks. Runtime adapters must resolve them, not assume a `current/` directory.
- Produces: persisted channel sequence high-water marks, exclusive process locking and interrupted-switch recovery. The caller still must hold the runtime task lock; store stage advancement does not verify downloaded bytes or perform data migrations.

- [x] **Step 1: Write tests covering fresh install, interrupted download, interrupted switch, failed health check, rollback, replay after restart, and preservation of the data directory.**
- [x] **Step 2: Run `go test ./internal/store -v` and verify the tests fail before implementation.** Red: undefined Store/Open/Transaction/Stage.
- [x] **Step 3: Implement the directory layout, transaction journal, legal stage transitions and atomic logical pointer replacement.** Linux uses file sync + rename + directory sync; Windows exercises file sync/rename and locking only. Old release directories are retained without garbage collection. Latest transaction result is retained, not a full historical event ledger.
- [x] **Step 4: Run `go test ./internal/store -v` and verify all transaction tests pass on a temporary data root.** Includes child-process exit without Close. Windows tests do not prove Linux/Android power-loss durability.
- [x] **Step 5: Run `go test ./...` to verify Task 1 remains green.**

### Task 3: Downloader and component installer

2026-09-12 partial implementation: HTTPS downloader (Range resume, mirror fallback, full-byte hash/size verification, cancellation, redirect policy), project tar.gz staging, raw manager binary staging and rootfs tar.gz extraction policy have local tests. Full-file hash validates reused partial bytes; explicit resume identity metadata is not implemented. ZIP/tar.zst, a clean Debian recipe, disk budgeting and actual rootfs activation remain unimplemented. The example manifest remains a schema fixture, not an installable production release.

**Files:**
- Create: `maa-phone-manager/manager/internal/download/client.go`
- Create: `maa-phone-manager/manager/internal/download/client_test.go`
- Create: `maa-phone-manager/manager/internal/install/installer.go`
- Create: `maa-phone-manager/manager/internal/install/installer_test.go`
- Test: `maa-phone-manager/manager/internal/download/client_test.go`
- Test: `maa-phone-manager/manager/internal/install/installer_test.go`

**Interfaces:**
- Consumes: selected artifacts and store transactions.
- Produces: `download.Fetch(ctx context.Context, artifact manifest.Artifact, destination string, progress func(download.Progress)) error`.
- Produces: `install.Stage(ctx context.Context, artifact manifest.Artifact, archivePath, releaseDir string) error`.
- Guarantees: every mirror is accepted only when byte size and SHA-256 match the same manifest artifact.

- [ ] **Step 1: Write HTTP fixture tests for resume, primary-source failure, mirror fallback, truncated response, wrong size, wrong hash, and cancelled context.**
- [ ] **Step 2: Write archive tests rejecting absolute paths, parent traversal, symlink escape, duplicate paths, and unsupported compression.**
- [ ] **Step 3: Run `go test ./internal/download ./internal/install -v` and verify the tests fail before implementation.**
- [ ] **Step 4: Implement bounded downloads, `.part` resume metadata, mirror iteration, streaming SHA-256, safe extraction, and staged-directory cleanup.**
- [ ] **Step 5: Run `go test ./...` and `go vet ./...` and verify both pass.**

### Task 4: Device probe, Root adapters, and rescue module

**Files:**
- Create: `maa-phone-manager/module/module.prop`
- Create: `maa-phone-manager/module/customize.sh`
- Create: `maa-phone-manager/module/service.sh`
- Create: `maa-phone-manager/module/action.sh`
- Create: `maa-phone-manager/module/uninstall.sh`
- Create: `maa-phone-manager/module/webroot/index.html`
- Create: `maa-phone-manager/manager/internal/device/probe.go`
- Create: `maa-phone-manager/manager/internal/device/probe_test.go`
- Create: `maa-phone-manager/module/tests/test_module_layout.ps1`

**Interfaces:**
- Produces: `device.Probe() (device.Facts, error)`. `Facts.DeviceFacts` (a `manifest.DeviceFacts` field, not a method) is the compatibility input for `SelectArtifact`; `Facts.Supported()` reports technical prerequisites only and is explicitly not device acceptance. `Facts` carries ABI, Android API, root manager markers, free bytes, SELinux mode, uid and required path availability.
- Consumes: the released rescue `maa-managerd` binary at module packaging time. `packaging/build-module.ps1` refuses anything that is not a static 64-bit AArch64 ELF and refuses any artifact name that is not an explicit preview.
- Starts: `maa-managerd serve --data-root /data/local/maa-platform --listen 127.0.0.1:8088 --config "$MODDIR/release.json" --assets "$MODDIR/webroot/assets"` from the non-blocking service stage. A missing `release.json` is the daemon's recovery-only mode, not a script decision.

- [x] **Step 1: Write device-probe fixture tests for Magisk, KernelSU, APatch, unsupported ABI, insufficient space, and missing writable data root.** Completed 2026-09-12: `internal/device/probe_test.go` covers the six named cases plus prop/statfs failures, ABI-list fallback, ambiguous markers and non-Linux hosts.
- [x] **Step 2: Write the PowerShell module-layout test to assert LF endings, executable-script declarations, stable module ID, required files, loopback listen address, and absence of embedded credentials.** Completed 2026-09-12 as `module/tests/test_module_layout.ps1` (117 checks), which also asserts the fixed `serve`/`open-ui`/`stop` invocations, the mode/format of `release.json`, and that webroot exposes no input control.
- [x] **Step 3: Implement strict device probing and Root-manager detection without modifying the device.** Completed 2026-09-12: fixed property allowlist through `/system/bin/getprop`, fixed marker paths, uid, `statfs` free space and SELinux mode; no write path exists in the probe.
- [x] **Step 4: Implement module install/start/action/uninstall scripts; uninstall stops services but preserves `/data/local/maa-platform/data/`.** Completed 2026-09-12. Uninstall calls the binary `stop`, keeps `/data/local/maa-platform` and contains no `pkill`, `killall` or `rm`; its fallback kill requires `/proc/<pid>/cmdline` argv[0] to equal the module binary **and** argv[1] to be `serve`.
- [x] **Step 5: Run `go test ./internal/device -v` and `powershell -ExecutionPolicy Bypass -File module/tests/test_module_layout.ps1`.** Completed 2026-09-12: device tests PASS (8 cases), `MODULE_LAYOUT_OK checks=117 files=8`, `MODULE_PACKAGING_OK passed=49` for `packaging/test-module.ps1`, `sh -n` clean on all four scripts.

`packaging/build-module.ps1` also ships `webui/module-settings.js|.css` (parallel WebUI lane) plus `docs/user-guide.md` as `webroot/assets/*`; missing WebUI assets are refused unless `-AllowMissingWebuiAssets` is passed for a local preview, in which case `module-metadata.json` records `webui_assets=missing`.

### Task 5: Local API and beginner WebUI

**Files:**
- Create: `maa-phone-manager/manager/internal/api/server.go`
- Create: `maa-phone-manager/manager/internal/api/server_test.go`
- Adapt: existing `maa-webui.py` navigation with one "关于与模块设置" section, only after verifying the structured runtime source baseline.
- Create: `maa-phone-manager/webui/module-settings.js`, `module-settings.css`, `webui/tests/`, and `packaging/webui_overlay.py` (injection reference + fixture generator) following the existing WebUI styling and APIs.
- Create: minimal bootstrap/recovery page embedded in the manager, not a second daily-use console.
- Create: `maa-phone-manager/webui/tests/first-install.spec.ts`

**Interfaces:**
- Produces: `GET /api/v1/status`, `GET /api/v1/versions`, `POST /api/v1/install`, `POST /api/v1/update/check`, `POST /api/v1/update/apply`, `POST /api/v1/rollback`, and `GET /api/v1/events`.
- Consumes: device facts, transactions, download progress, installed-component state, and health-check results from Tasks 1-4.
- Rejects: concurrent mutating operations, arbitrary URLs, arbitrary commands, and update requests while a MAA task owns the execution lock.

2026-09-12 接线状态：WebUI 扩展资产已本地实现（24 Node 行为测试、16 真实页面测试，主线复跑 46 条 Chromium 断言）；反代注入、`/module-assets` 静态资源、`X-MAA-Request` 与 JSON 强校验已在 `manager/internal/api/server.go` 就位，`/api/v1/status` 的字段与 `control.Status` 一致。上面 Interfaces 里的 `/api/v1/versions`、`/api/v1/install`、`/api/v1/events` 未实现，实际固定接口是 `/api/v1/status`、`/api/v1/help`、`/api/v1/update/check`、`/api/v1/update/apply`、`/api/v1/rollback`（外加 `/api/v1/session` 入口票据）。模块打包器和 CLI 的 `--assets` 已接入扩展与离线帮助；浏览器验证仍为模拟后台，不是真实 Android daemon/runtime 联调。详见 `webui/README.md`。

- [ ] **Step 1: Write API tests for first-install status, confirmation requirements, conflict responses, progress events, rollback, and sanitized user-facing errors.**
- [ ] **Step 2: Run `go test ./internal/api -v` and verify the tests fail before implementation.**
- [ ] **Step 3: Implement the fixed API, single-operation coordinator, event stream, and embedded recovery page.**
- [ ] **Step 4: Reuse the existing WebUI and add only 关于与模块设置 with version/environment status, install/update/rollback, stable/beta, offline help and diagnostic export. Keep homepage, weekly plans, infrast and cultivation unchanged. Show bootstrap/recovery UI only when the normal runtime UI is unavailable.**
- [ ] **Step 5: Run the API tests and browser test; verify the first-install flow never invokes a full daily MAA task.**

### Task 6: Existing MAA runtime adapter

**Files:**
- Create: `maa-phone-manager/manager/internal/runtime/runtime.go`
- Create: `maa-phone-manager/manager/internal/runtime/maa_adapter.go`
- Create: `maa-phone-manager/manager/internal/runtime/maa_adapter_test.go`
- Create: `maa-phone-manager/packaging/project/include.txt`
- Create: `maa-phone-manager/packaging/project/build.ps1`

**Interfaces:**
- Produces: `runtime.InstallProject`, `StartDebian`, `StopDebian`, `ReadStatus`, `RunStartupHealth`, and `RunDaily`.
- Consumes: an explicit project-package inventory generated from the structured `.codex_remote_maa/` source tree.
- Guarantees: `RunStartupHealth` and `RunDaily` remain separate user actions; packaging never includes `tg.txt`, private keys, logs, screenshots, backups, or `_legacy_flat`.

- [ ] **Step 1: Write adapter tests using fixture scripts for success, timeout, task-lock conflict, malformed status, and cleanup after cancellation.**
- [ ] **Step 2: Write packaging tests asserting the allowlist includes required structured scripts and excludes credentials, evidence, backups, and upstream source trees.**
- [ ] **Step 3: Implement the runtime adapter around fixed command identifiers rather than caller-provided shell strings.**
- [ ] **Step 4: Implement deterministic project packaging with a file inventory, version file, SHA-256 output, and LF validation.**
- [ ] **Step 5: Run all Go tests, the packaging dry run, the existing local structure check, and a package-content credential scan.**

### Task 7: Release automation and device acceptance

**Files:**
- Create: `.github/workflows/maa-phone-manager-release.yml`
- Create: `maa-phone-manager/release/build-manifest.go`
- Create: `maa-phone-manager/release/build-manifest_test.go`
- Create: `maa-phone-manager/docs/release-checklist.md`
- Create: `maa-phone-manager/docs/compatibility.md`

**Interfaces:**
- Consumes: tested manager, module, WebUI, project package, rootfs recipe output, release notes, and protected signing credentials.
- Produces: immutable release assets, SHA-256 files, signed beta/stable manifests, and a recorded compatibility matrix.
- Promotion rule: beta manifest is published first; stable changes only after install, update, reboot, rollback, and no-game health checks pass on every declared Root-manager combination.

- [ ] **Step 1: Write manifest-builder tests that reject mutable URLs, missing hashes, missing byte sizes, unsupported compatibility claims, and unsigned output.**
- [ ] **Step 2: Implement reproducible ARM64 builds, SBOM generation, release-asset upload, manifest generation, and Ed25519 signing through protected CI secrets.**
- [ ] **Step 3: Run CI in dry-run mode and compare two builds from the same source for identical file inventories and checksums.**
- [ ] **Step 4: Install the beta module on an approved spare/test device, verify first install, reboot persistence, WebUI recovery, component update, interrupted download, failed health-check rollback, module disable, and uninstall-with-data-preservation.**
- [ ] **Step 5: Record exact device/Android/Root-manager versions and evidence in `docs/compatibility.md`; publish stable only for combinations that passed every gate.**

## Plan self-review

- Spec coverage: module bootstrap, manager daemon, cloud-only metadata, third-party downloads, WebUI, layered updates, compatibility gates, user-data separation, rescue, rollback, packaging and release acceptance are each assigned to a task.
- Placeholder scan: implementation tasks name concrete files, interfaces, checks and expected safety properties; no task delegates behavior to an unspecified command or arbitrary script.
- Type consistency: Tasks 2-7 consume `manifest.ChannelManifest`, `manifest.DeviceFacts`, `manifest.Artifact`, store transactions and fixed runtime operations produced by earlier tasks.

## Execution handoff

Tasks 1-2 and the local portion of Task 4 are implemented. Task 3 has extraction policies but no production rootfs recipe. Task 5 has CLI/API/controller, packaged UI assets/help and single-WebUI extension; no events, diagnostics export, channel switching or real install driver. Task 6 has checks, task locks and a 23-file allowlisted project directory builder (not an installable runtime). Task 7 has a manual preview-only CI workflow, not a signed release pipeline. Continue with the explicit blockers in `release-checklist.md`; do not enable updates by replacing a missing driver with a no-op. Do not treat Windows unit tests or Linux/ARM64 cross-compilation as module installation acceptance. Production phone changes, module flashing and external publication require separate approval. Preserve the user-approved single-WebUI scope.
