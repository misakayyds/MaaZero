# MaaZero

**「零电脑依赖、零命令行折腾、零系统接管」的手机端 MAA 独立沙箱模块**

[![Platform](https://img.shields.io/badge/Platform-Android%2011--16%20ARM64-3DDC84)](https://github.com/misakayyds/MaaZero)
[![Root](https://img.shields.io/badge/Root-KernelSU%20%7C%20Magisk%20%7C%20APatch-0F766E)](https://github.com/misakayyds/MaaZero)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**MaaZero** 是一套面向已 Root ARM64 Android 设备的 [MaaAssistantArknights (MAA)](https://github.com/MaaAssistantArknights/MaaAssistantArknights) 一体化环境编排器、最小 Root 引导模块与 Ed25519 分层签名分发系统。

你只需要一台已安装《明日方舟》并拥有 Root 权限的手机——无需电脑配环境、无需安装 Termux 敲命令、无需手动折腾无线调试配对。刷入模块重启后，直接从 Root 管理器打开本地管理面板即可完成运行环境拉取、资源热更新、日常挂机与定时排班。

---

## 核心特性

- **真·手机端零门槛开箱**
  刷入 `MaaZero-v0.1.0.zip` 重启后，直接通过 KernelSU / APatch 模块 WebUI 入口或 Magisk「操作」按钮进入面板，一键完成 Debian 运行时与业务脚本的签名校验与原子部署。
- **全功能自动化工作流**
  内置完整的日常任务编排、每周关卡排程（周计划）、基建三班轮换（自动同步干员基建技能门槛）、理智药到期自动使用策略、干员盒识别扫描与培养材料缺口规划，以及官服《明日方舟》客户端在线检查与一键更新。
- **独立沙箱与严苛安全边界**
  - **零系统分区污染**：不修改 `/system`、`/vendor`、`/product`，不关闭系统 SELinux。
  - **纯本机回环通信**：守护进程 `maa-managerd`（`127.0.0.1:8088`）与内部 WebUI（`127.0.0.1:8089`）仅绑定本地回环地址；本机回环 ADB（`127.0.0.1:5555`）通过 `iptables` 强制限制在 `lo` 接口，局域网与公网均无法触达。
  - **只读脚本层**：业务脚本目录以只读方式（`ro` bind mount）挂载进沙箱，杜绝运行期意外篡改。
- **Ed25519 签名通道与离线安全回滚**
  所有发布清单均经 Ed25519 私钥离线签名、组件产物强制校验大小与 SHA-256。升级仅在任务空闲窗口原子切换；新版本激活健康检查未通过时自动回退；即使远端发布源不可达，仍可在本地一键离线回滚到上一可用版本。
- **程序与用户数据彻底解耦**
  所有配置、排班计划、培养数据、MaaCore 资源与日志统一保存在 `/data/local/maa-platform`。升级或卸载 Root 模块时仅安全停止后台进程并解除挂载点，**绝不删除用户数据**。

---

## 环境要求

| 项目 | 要求说明 |
| :--- | :--- |
| **处理器架构** | `arm64-v8a`（64 位 ARM Android 手机 / 平板） |
| **Android 版本** | Android 11 – Android 16（API 30+；已完成 Android 16 + KernelSU 真机冷启动与任务验证） |
| **Root 管理器** | **KernelSU**（推荐，原生内嵌 WebUI）、**APatch** 或 **Magisk**（v24+） |
| **可用存储空间** | 建议 `/data` 分区剩余空间 **≥ 5 GB**（用于存放独立 Debian 运行时、MaaCore 资源及游戏更新包） |
| **游戏客户端** | 已安装官服（`com.hypergryph.arknights`）或 B 服（`com.hypergryph.arknights.bilibili`），并已在游戏内手动登录过账号 |

> [!NOTE]
> MaaZero 不托管、不保存你的游戏账号密码，也不会修改游戏客户端文件；游戏登录态由《明日方舟》客户端自身维护。

---

## 三步快速开始

### 第 1 步：下载并刷入模块

1. 前往 [GitHub Releases](https://github.com/misakayyds/MaaZero/releases) 下载最新正式版模块包 **`MaaZero-v0.1.0.zip`**（注意：请下载该模块 ZIP，**不要**下载 GitHub 自动生成的 `Source code` 压缩包）。
2. 打开手机上的 **KernelSU / APatch / Magisk**，进入「模块」页面选择从本地安装 `MaaZero-v0.1.0.zip`，刷入完成后点击 **重启设备**。

### 第 2 步：一键部署运行沙箱

1. 手机重启解锁后，打开 Root 管理器的「模块」页面：
   - **KernelSU / APatch**：点击 `MaaZero` 模块卡片上的 **WebUI** 按钮。
   - **Magisk**：点击 `MaaZero` 模块卡片上的 **操作（Action）** 按钮，系统浏览器将自动打开本地管理地址 `http://127.0.0.1:8088`。
2. 在「关于与模块设置」面板中点击 **检查更新**，确认版本信息后点击 **安装 / 应用更新**。
3. `maa-managerd` 将自动下载并校验签名的 `debian-rootfs` 与 `maa-phone-project` 组件，原子解压并拉起内部运行环境与定时守护。

### 第 3 步：初始化资源并开始使用

1. 环境启动完成后自动进入 MAA 管理主页，选择你的客户端类型（官服 / B 服）。
2. 首次使用时点击面板中的 **更新 MAA 资源**（首次检测到未安装 `libMaaCore.so` 时将自动完成 `MaaCore` 与官方资源包初始化）。
3. 如遇官服客户端大版本更新，可直接在面板点击 **更新客户端 APK** 完成下载安装；随后即可手动点击开始任务，或在「周计划」「基建」「培养」标签页配置全自动定时挂机。

详细使用说明与常见问题排查请参阅模块内置的离线手册：[docs/user-guide.md](docs/user-guide.md)。

---

## 架构与数据边界

```text
+-----------------------------------------------------------------------+
| Android Host (KernelSU / Magisk / APatch)                             |
|                                                                       |
|  /data/adb/modules/maa-phone-manager/                                 |
|    ├── service.sh  ──► 启动 maa-managerd (127.0.0.1:8088)             |
|    ├── action.sh / webroot/ ──► 唤起本地管理页                        |
|    └── release.json (内置 Ed25519 公钥与签名通道清单地址)             |
|                                                                       |
|  /data/local/maa-platform/              [升级与卸载模块均完整保留]     |
|    ├── releases/                        版本化不可变项目包与校验元数据 |
|    ├── transactions/                    原子更新与离线回滚事务日志     |
|    ├── data/                            用户配置、排班、培养与运行日志 |
|    └── runtime/rootfs/                  独立 Debian ARM64 Chroot 沙箱  |
|          ├── /usr/local/bin/maa         官方 maa-cli                  |
|          ├── /root/maa-scripts (ro)     只读挂载当前激活版业务脚本     |
|          ├── maa-webui.py               内部业务服务 (127.0.0.1:8089) |
|          └── cron -f                    自动定时调度守护               |
+-----------------------------------------------------------------------+
```

### 卸载与彻底清理说明

- **常规卸载（保留配置与环境）**：在 Root 管理器中卸载 `MaaZero` 模块并重启即可。`uninstall.sh` 会自动停止守护进程并干净卸载所有 Chroot 挂载点，同时保留 `/data/local/maa-platform` 下的全部配置与运行环境，方便后续重装无缝恢复。
- **彻底卸载并释放全部空间**：先在 Root 管理器中卸载模块并**完成重启**（确保没有任何挂载点指向沙箱），随后使用 Root 文件管理器或 Root 终端删除 `/data/local/maa-platform` 目录即可完全清理，不会在系统中留下任何残留。

---

## 分发拓扑与从源码构建

MaaZero 采用「静态签名清单（GitHub Pages）+ 不可变版本大文件（GitHub Releases）」的无服务器分发架构：

| 组件 / 文件 | 托管位置 | 用途说明 |
| :--- | :--- | :--- |
| `MaaZero-v0.1.0.zip` | GitHub Releases (`v0.1.0`) | 用户首次刷入的最小 Root 引导模块（含 `maa-managerd` 与 `release.json`） |
| `maa-managerd` | GitHub Releases (`v0.1.0`) | 静态编译 ARM64 编排守护进程二进制 |
| `maa-phone-project-0.1.0.tar.gz` | GitHub Releases (`v0.1.0`) | 28 个业务与 WebUI 脚本的确定性版本归档包 |
| `debian-rootfs-12.11.0-arm64.tar.gz` | GitHub Releases (`rootfs-12.11.0`) | 纯净构建的 Debian Bookworm ARM64 运行时归档（预置 `maa-cli` 与运行库） |
| `channels/{stable,beta}.json{,.sig}` | GitHub Pages (`manifest/channels/`) | 经 Ed25519 私钥签名的通道版本清单与 64 字节原始签名 |

### 本地构建与全量验证命令

环境要求：Go 1.24+、Python 3.11+、Node.js 20+、PowerShell 7+。

```powershell
# 1. 交叉编译静态 ARM64 守护进程
$env:GOOS = "linux"; $env:GOARCH = "arm64"; $env:CGO_ENABLED = "0"
Push-Location manager
go build -trimpath -ldflags="-s -w" -o ../bin/maa-managerd ./cmd/maa-managerd
Pop-Location
Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED

# 2. 构建白名单业务脚本包与确定性归档
pwsh -NoProfile -ExecutionPolicy Bypass -File packaging/project/build-project.ps1 -Out dist/project -Version 0.1.0
python -B packaging/package-project-archive.py --src dist/project/project --out dist/maa-phone-project-0.1.0.tar.gz

# 3. 生成通道清单并使用仓库外 Ed25519 私钥签名
python -B packaging/generate-manifests.py --version 0.1.0 --rootfs dist/debian-rootfs-12.11.0-arm64.tar.gz
python -B packaging/sign-manifests.py --key-file "$HOME/.ssh/maa_phone_manager_release_ed25519"

# 4. 打包正式版 Root 模块 ZIP
pwsh -NoProfile -ExecutionPolicy Bypass -File packaging/build-module.ps1 `
  -Release -ManagerBinary bin/maa-managerd -ReleaseConfig dist/release.json `
  -Version 0.1.0 -VersionCode 100 -Force

# 5. 运行全套单元、打包与端到端回归测试
Push-Location manager; go vet ./...; go test ./... -count=1; Pop-Location
python -B -m unittest discover -s packaging/tests -v
python -B packaging/rootfs/test_recipe.py
node --test webui/tests/module-settings.test.mjs
pwsh -NoProfile -ExecutionPolicy Bypass -File module/tests/test_module_layout.ps1
pwsh -NoProfile -ExecutionPolicy Bypass -File packaging/test-module.ps1
python -B tests/e2e/runner.py
```

---

## 文档索引

- [使用说明与排障手册](docs/user-guide.md)（同步内置于模块离线帮助）
- [总体架构说明](docs/architecture.md)
- [更新与离线回滚机制](docs/update-and-rollback.md)
- [云端静态清单维护说明](docs/cloud-maintenance.md)
- [发布与签名操作清单](docs/release-checklist.md)
- [通道清单 Schema 说明](manifest/README.md)

---

## 致谢与开源许可

- 核心游戏自动化能力由 [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights) 与 [maa-cli](https://github.com/MaaAssistantArknights/maa-cli) 提供。
- 容器内基础运行环境基于 [Debian GNU/Linux (Bookworm ARM64)](https://www.debian.org/) 构建。
- 本仓库自有编排器、模块引导与前端代码以 [MIT License](LICENSE) 开源分发；运行时按需拉取的上游组件遵循其各自开源许可证。
