# 进度日志

<!-- agent-kit:progress-format:v2 -->
> 有实质进展或交接价值时才新增记录;普通问答和无新发现的只读检查不强制记录。
> 新条目插在本说明块之后、已有条目之前。旧日志仅作历史参考,不要重写。
> 标题格式:`YYYY-MM-DD HH:mm ±TZ — Agent / 环境`
> 示例:`2026-07-01 23:28 +0800 — Codex / Windows`
> 使用写入时的实际分钟级时间、时区、Agent 名称和环境,不要照抄示例。
> 正文建议字段:完成 / 改动 / 验证 / 风险 / 下一步;可合并或省略空项。
> 模型或会话 ID 如确有必要,写在正文里,不要放标题。
<!-- /agent-kit:progress-format:v2 -->

## 2026-11-12 09:45 +0800 — Antigravity / Windows
- 完成: 清理开发与多智能体测试阶段遗留的本地临时文档/测试报告及 `docs/` 下已过时的早期计划/盘点文档，同步更新残留文档引用并验证打包与合规测试全绿。
- 改动: 删除未跟踪/忽略的阶段性文件（`PROJECT.md`、`TEST_INFRA.md`、`TEST_READY.md`、`tests/e2e/{sample_report,test_challenger_report}.json`、`test-report-audit.json`、`.agents/teamwork/`）及已过时的入库过程文档（`docs/{implementation-plan,rootfs-build-plan,runtime-dependency-inventory,compatibility,user-flow}.md`、`docs/runtime-project-files.json`）；更新 `AGENTS.md`、`manager/README.md`、`packaging/project/README.md` 与 `packaging/project/source/README.md` 中的旧链接与过时快照声明。
- 验证: 已验证全仓零失效文档引用、纯 LF / UTF-8 无 BOM、`packaging/test-module.ps1`（65/65）、`test_build_project*.ps1`（83 + 12 项）及 `python -B tests/e2e/runner.py --feature 14`（24/24 项合规扫描全绿）。

## 2026-11-12 09:18 +0800 — Antigravity / Windows
- 完成: 完成 **MaaZero `v0.1.0`** 正式首发签名、纯净 `debian-rootfs-12.11.0-arm64.tar.gz` 构建、新设备首次自动拉取与部署运行时及 `MaaCore` 自举闭环、正式模块包 `MaaZero-v0.1.0.zip` 打包、GitHub Pages 自动验签部署工作流与正式 `README.md` / `docs/user-guide.md` / `docs/release-checklist.md` 编写，全仓单元、打包与 221 项 E2E 回归 100% 通过。
- 改动:
  - 空设备首装与自举闭环：`manager/internal/store/layout.go` 新增 `Store.Root()`，`manager/internal/control/control.go` 新增 `ensureRuntimeRootfs`（当通道清单包含 `required` 的 `runtime` 组件且本地 `runtime/rootfs` 尚未创建时，先下载并经 `install.StageRootfs` 原子解包部署再安装激活业务脚本包，不影响已有挂载环境）；`packaging/project/source/{maa-auto-update.sh,maa-run.sh}` 在首次未检测到 `libMaaCore.so` 时自动执行 `maa install --batch` 完成初始化。
  - 签名与正式打包工具链：新增 `manager/cmd/maa-release-sign/main.go` 与 `packaging/sign-manifests.py`（拒绝仓库内私钥路径，在仓库外生成并收紧 ACL 的 Ed25519 私钥种子，签发 `stable.json.sig` / `beta.json.sig` 与公开 `release.json`，支持 `--verify-only --verify-artifacts` 全组件解包验签）；`packaging/build-module.ps1` 新增 `-Release` 正式构建模式（保留默认预览门禁兼容全部既有测试，正式模式强制要求 `-ReleaseConfig` 并输出 `MaaZero-v0.1.0.zip`）；更新 `packaging/{generate-manifests.py,package-project-archive.py}` 默认指向 `misakayyds/MaaZero` 与 `0.1.0`；新增 `.github/workflows/pages.yml` 与 `LICENSE`。
  - 纯净产物与正式文档：构建纯净无个人数据的 Debian Bookworm ARM64 运行时归档 `dist/debian-rootfs-12.11.0-arm64.tar.gz`（预置上游 `maa-cli v0.7.8` 与 `MaaCore` 依赖库），连同 `dist/maa-managerd`、`dist/maa-phone-project-0.1.0.tar.gz` 写入真实大小与 SHA-256 并完成 Ed25519 签名，打包生成 `dist/MaaZero-v0.1.0.zip` 与 `dist/SHA256SUMS.txt`；重写正式 `README.md`、`docs/user-guide.md`、`docs/release-checklist.md` 与 `manifest/channels/README.md`；修复 `manager/internal/control/control_test.go` 在 Windows Defender 实时扫描下的 `TempDir` 清理偶发句柄竞态。
- 验证:
  - 签名与产物级校验：`python -B packaging/sign-manifests.py --verify-only --verify-artifacts` 通过（`stable` / `beta` Ed25519 验签通过，`StageBinary`、`Stage`、`StageRootfs` 7596 文件 / 1115 目录 / 1071 符号链接全部通过）。
  - 全量测试套件：`go vet ./...` 与 `go test ./... -count=1`（全部包通过，含新增 `TestFirstInstallStagesRequiredRuntimeRootfsBeforeProject` 与 `TestPublishedChannelSignaturesVerifyAgainstReleasePublicKey`）、`python -B -m unittest discover -s packaging/tests -v`（16/16）、`python -B packaging/rootfs/test_recipe.py`（12/12）、`node --test webui/tests/module-settings.test.mjs`（24/24）、4 套 PowerShell 测试（150 + 65 + 83 + 12 项）及 `python -B tests/e2e/runner.py`（221/221 项全绿，0 CR 字节、0 BOM、0 密钥泄漏）。
- 风险: 远端 GitHub 仓库 `misakayyds/MaaZero` 尚未创建/推送（遵照协作规范未自动提交或推送），在将 `manifest/channels/*` 发布到 GitHub Pages 并上传 `v0.1.0` 与 `rootfs-12.11.0` Releases 之前，新设备在线拉取地址暂未生效；请务必妥善离线备份本机仓库外私钥 `~/.ssh/maa_phone_manager_release_ed25519`。
- 下一步: 确认无误后执行 Git 提交，创建远端 `misakayyds/MaaZero` 仓库开启 GitHub Pages（GitHub Actions 源），并按 `docs/release-checklist.md` 上传 `rootfs-12.11.0` 与 `v0.1.0` Release 产物。

## 2026-11-12 07:33 +0800 — Antigravity / Windows
- 完成: 在 ARM64 Android 16（KernelSU）真机上完成 `MAA Phone Manager` 模块刷入、整机冷启动自愈验证、`maa-cli` / `MaaCore` / `MaaResource` 初始化、通过 `:8088` 受控代理将明日方舟官服包从 `2.7.71` 在线更新至 `2.7.91` 并完成游戏内热更新资源加载，以及 `maa-run.sh --startup-health-check --stop-at-login` 冷启动拉起游戏、点击 `START` 进入登录界面即安全停靠（未尝试登录）；测试后清理真机临时文件与端口转发并保留运行环境在线，仓库全量单元、打包与 Tier 1–5 E2E 回归 100% 通过。
- 改动:
  - 修复引导与守护冷启动链路：`module/service.sh` 修正 `:8088` 十六进制端口探测（`0100007F:1F98`）与 `nohup` `execve` 过渡态保护；`manager/cmd/maa-managerd/main.go` 与 `manager/internal/control/control.go` 新增 `seed-release` 离线归档导入及 `StartBootstrap()` 开机异步激活；`manager/internal/driver/{mount.go,mount_linux.go,driver.go,supervisor_linux.go}` 将 `/root/maa-scripts` 设为只读绑定挂载（`MS_BIND|MS_REMOUNT|MS_RDONLY`）并启用 `python3 -B` / `PYTHONDONTWRITEBYTECODE=1`，防止 `__pycache__` 污染不可变发布目录；`manager/internal/driver/{host_linux.go,host_other.go}` 新增回环 ADB 保活看门狗（`127.0.0.1:5555` + `lo` 防火墙门禁），抵御厂商 ROM 启动后自动关闭 `adbd`。
  - 补齐 APK 在线更新与真机运行脚本修复：新增并接入 `packaging/project/source/arknights-apk-update.sh`（官服 API 版本检查、流式下载与 `adb install -r`），同步更新 `packaging/project/project-files.json`、`packaging/project/source/maa-webui.py` 与相关打包校验；修正 `manager/internal/driver/seed.go` 默认 `cli.toml`（移除导致全量克隆阻塞的 `backend = "git"`）；修复 `packaging/project/source/maa-run.sh` 自杀竞态（`RUNNER_PID_FILE` 写入时机与 `$$` 自身排除）、Android ZRAM 虚高 `SwapUsed` 误判内存压力，以及新增 `--stop-at-login` / `--keep-game-foreground`；修复 `packaging/project/source/maa_run_session.py` 上游 `api.maa.plus` 瞬时 TLS 异常本地缓存回退及 `with_stop_at_login` 自引用 `baseTask` 循环问题。
  - 重新生成静态 ARM64 `bin/maa-managerd`、`dist/maa-phone-project-0.1.0-preview.tar.gz`、`dist/MAA-Phone-Manager-preview-0.1.0-preview.zip` 与 `manifest/channels/{stable,beta}.json`，并将一致产物同步至真机保留环境。
- 验证:
  - 真机实测通过：整机重启零人工干预拉起 `maa-managerd`（`127.0.0.1:8088`）、`maa-webui.py`（`127.0.0.1:8089`）、`cron -f` 与回环 ADB（`127.0.0.1:5555`）；通过 `:8088/api/apk-update` 将 `com.hypergryph.arknights` 升级至 `versionName=2.7.91 (versionCode=210)`；`maa-run.sh --startup-health-check --stop-at-login --keep-game-foreground` 经 `RawWithGzip` + `MaaTouch` 完成横屏预热、点击 `START` 并匹配登录页 `StartToWakeUp.png` 后执行 `Action: Stop`（`StartUp Completed`，屏幕停留在登录界面未登录），退出后分辨率与旋转恢复原始值。
  - 本地自动化全量通过：`go vet ./...` 与 `go test ./... -count=1`（10/10 包）、`python -B -m unittest discover -s packaging/tests -v`（16/16）、`python -B packaging/rootfs/test_recipe.py`（12/12）、`node --test webui/tests/module-settings.test.mjs`（24/24）、4 套 PowerShell 布局与打包测试（150 + 55 + 83 + 12 项）及 `python -B tests/e2e/runner.py`（221/221 项通过）。
- 风险: 当前模块仍以无内置 `release.json` 的本地预览构建运行（`configured=false`），在线签名通道自动升级仍需正式离线 Ed25519 私钥签发与 HTTPS 托管清单后启用；厂商系统大版本升级后仍建议复核后台省电策略对长时日常任务的影响。
- 下一步: 如需正式对外分发，按 `docs/release-checklist.md` 使用离线私钥签发渠道清单并注入公开 `release.json`，同时可在提交前按需清理本地未跟踪的阶段性协作记录目录（如 `.agents/teamwork/`）。

## 2026-11-11 22:50 +0800 — Antigravity / Windows
- 完成: 通过 Teamwork 多智能体协作完成 `packaging/project/source/` 下 28 个业务脚本通用化路径与进程边界解耦（消解 R01–R09 阻断项）、`manager/internal/driver/` Debian chroot 生命周期与受控进程组监督驱动实现、静态 ARM64 `maa-managerd` 编译、Magisk/KernelSU/APatch 模块及项目归档包构建、静态通道清单生成，并通过 221 项 Tier 1–5 E2E 黑盒与对抗测试及独立终审。
- 改动: 更新 `packaging/project/source/*` 15 个脚本以环境变量驱动并移除 `nsenter` 越界与全局 `pkill -f`；新增 `manager/internal/driver/*` 并在 `manager/cmd/maa-managerd/main.go` 接入驱动；加固 `module/uninstall.sh` 保留用户数据契约；新增 `packaging/{package-project-archive.py,generate-manifests.py}`、`manifest/channels/{stable,beta}.json`、`tests/e2e/*`（Tier 1–5 共 221 项测试）及构建产物 `bin/maa-managerd`、`dist/MAA-Phone-Manager-preview-0.1.0-preview.zip`、`dist/maa-phone-project-0.1.0-preview.tar.gz`。
- 验证: 已验证 `manager/` 下 `go test ./... -count=1` 与 `go vet ./...`（10/10 包通过）、`python -B -m unittest discover -s packaging/tests -v`（16/16）、`node --test webui/tests/module-settings.test.mjs`（24/24）、`python -B packaging/rootfs/test_recipe.py`（12/12）、4 项 PowerShell 模块布局与打包脚本、Tier 1–5 E2E 测试（221/221）及全仓纯 LF / UTF-8 无 BOM / 零凭据检查；未验证真机 ARM64 Android Root 宿主实机刷入与隔离联调。
- 风险: 当前本地构建已完成离线、模拟与打包全量回归，但尚未在真实 ARM64 Android Root 设备上完成刷入与真机隔离验收，正式面向普通用户分发前仍需按 `AGENTS.md` 红线完成真机验证与正式签名私钥离线签发。
- 下一步: 将 `dist/MAA-Phone-Manager-preview-0.1.0-preview.zip` 导入测试用 ARM64 Root Android 设备验证 Magisk/KernelSU/APatch 引导、Debian chroot 挂载与 8088/8089 服务闭环，确认无误后使用离线发布密钥签发正式渠道清单并清理临时调研产物（如 `.agents/teamwork/`）。

## 2026-11-11 17:06 +0800 — Antigravity / Windows
- 完成: 从原单机维护仓库将 `maa-phone-manager` 独立拆分至 `E:\01_Projects\Active\maa-phone-manager`，迁入手动预览 CI 工作流 `.github/workflows/maa-phone-manager-preview.yml`，将 `project-files.json` 白名单内的 28 个脚本纯 LF 归一化后内置于 `packaging/project/source/`，完成全仓跨目录路径解耦、协作规范初始化及内置脚本待审查规范声明。
- 改动: 新增 `packaging/project/source/*`（28 个白名单脚本快照，无凭据/无内网 IP/无设备改动脚本）、`packaging/project/{README.md,source/README.md}`、`.gitattributes`、`.gitignore`、`.github/workflows/maa-phone-manager-preview.yml` 及 `AGENTS.md` / `CLAUDE.md` / `.agents/rules/00-shared.md` / `docs/agent/*`；更新 `build-project.ps1`、`test_build_project*.ps1`、`webui_overlay.py`、`test_webui_overlay.py`、`module-settings.browser.mjs` 及多处 README 命令路径。
- 验证: Go 单元测试与 `go vet`、Python `test_recipe.py` 与 `test_webui_overlay.py`、Node `module-settings.test.mjs`、PowerShell 模块布局/模块打包/项目白名单打包测试及 `linux/arm64` 预览构建全量通过。
- 风险: `packaging/project/source/` 内置脚本为单机原型初始快照，仍含硬编码 chroot 路径、`nsenter`/`pkill` 与上游直连自更新逻辑（R01–R09），对外正式发布前必须逐项完成规范与安全重构。
- 下一步: 按 `packaging/project/README.md` 与 `docs/release-checklist.md` 对 `packaging/project/source/` 开展规范化审查与参数化改造，并推进真实 Debian rootfs 锁与运行驱动实现。
