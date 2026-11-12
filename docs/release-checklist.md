# 维护者：MaaZero 签名构建与发布检查清单

本文档记录 **MaaZero**（GitHub 仓库：`misakayyds/MaaZero`）正式版本与预览版本的完整构建、Ed25519 外部密钥签名、GitHub Releases 上传及 GitHub Pages 通道清单发布流程。

---

## 一、安全红线（发布前必查）

1. **私钥严格隔离**：Ed25519 发布签名私钥仅允许保存在维护者本机仓库外路径（默认 `~/.ssh/maa_phone_manager_release_ed25519`，Windows 下仅授予当前用户读写 ACL，POSIX 下权限 `0600`）。`packaging/sign-manifests.py` 与 `maa-release-sign` 会主动拒绝任何位于仓库目录内的私钥路径。
2. **零敏感信息入库**：任何仓库源码、`manifest/`、`dist/` 构建产物中仅允许包含 32 字节 Base64 **公钥**（`release.json`），严禁写入私钥种子、个人 Token、SSH 密钥或测试机局域网信息。
3. **纯 LF 与不可变地址**：全仓文本文件强制 UTF-8 无 BOM + 纯 LF（`\n`）；通道清单中的所有组件 `url` 必须指向包含明确版本号的不可变 HTTPS 地址，禁止使用 `/latest/` 或分支 `raw/main`。

---

## 二、正式版（如 `v0.1.0`）一键构建与签名流程

在仓库根目录使用 PowerShell 7 执行：

```powershell
# 1. 编译静态 ARM64 maa-managerd 守护进程
$env:GOOS = 'linux'
$env:GOARCH = 'arm64'
$env:CGO_ENABLED = '0'
Push-Location manager
go build -trimpath -ldflags='-s -w' -o ../bin/maa-managerd ./cmd/maa-managerd
if ($LASTEXITCODE -ne 0) { throw 'ARM64 build failed' }
Pop-Location
Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED -ErrorAction SilentlyContinue

# 2. 构建白名单项目脚本与确定性归档包
pwsh -NoProfile -ExecutionPolicy Bypass -File packaging/project/build-project.ps1 -Out dist/project -Version 0.1.0
python -B packaging/package-project-archive.py --src dist/project/project --out dist/maa-phone-project-0.1.0.tar.gz

# 3. 确认纯净 Debian Bookworm ARM64 rootfs 归档就位后生成通道清单
python -B packaging/generate-manifests.py `
  --version 0.1.0 `
  --sequence 1 `
  --repo misakayyds/MaaZero `
  --rootfs dist/debian-rootfs-12.11.0-arm64.tar.gz

# 4. 使用仓库外 Ed25519 私钥签名 stable/beta 清单并输出公开 release.json
# （首次生成密钥对时追加 --init-key）
python -B packaging/sign-manifests.py `
  --key-file "$HOME/.ssh/maa_phone_manager_release_ed25519" `
  --manifest-url "https://misakayyds.github.io/MaaZero/channels/stable.json" `
  --channel stable

# 5. 打包正式版 Root 模块 MaaZero-v0.1.0.zip
pwsh -NoProfile -ExecutionPolicy Bypass -File packaging/build-module.ps1 `
  -Release `
  -ManagerBinary bin/maa-managerd `
  -ReleaseConfig dist/release.json `
  -Version 0.1.0 `
  -VersionCode 100 `
  -Force
```

> 如需本地开发调试预览模块（不含正式发布标识），去掉 `-Release` 并传入 `-Version 0.1.0-preview` 即可输出 `dist/MAA-Phone-Manager-preview-0.1.0-preview.zip`。

---

## 三、GitHub 远端发布与托管核对步骤

1. **推送代码并启用 GitHub Pages**：
   - 将仓库推送至 `https://github.com/misakayyds/MaaZero`。
   - 在 GitHub 仓库 `Settings -> Pages` 中将 `Build and deployment -> Source` 设为 **GitHub Actions**。
   - `.github/workflows/pages.yml` 会先调用 `sign-manifests.py --verify-only` 校验 Ed25519 签名，通过后自动将 `manifest/channels/{stable,beta}.json{,.sig}` 发布到：
     - `https://misakayyds.github.io/MaaZero/channels/stable.json`
     - `https://misakayyds.github.io/MaaZero/channels/stable.json.sig`
     - `https://misakayyds.github.io/MaaZero/channels/beta.json`
     - `https://misakayyds.github.io/MaaZero/channels/beta.json.sig`
2. **上传基础运行时 Release（Tag: `rootfs-12.11.0`）**：
   - 创建 GitHub Release tag `rootfs-12.11.0`，上传 `dist/debian-rootfs-12.11.0-arm64.tar.gz`（基础环境未变动时后续业务小版本无需重复上传此包）。
   - 示例命令：`gh release create rootfs-12.11.0 dist/debian-rootfs-12.11.0-arm64.tar.gz --title "Debian Bookworm ARM64 Runtime 12.11.0"`
3. **上传正式版模块与组件 Release（Tag: `v0.1.0`）**：
   - 创建 GitHub Release tag `v0.1.0`，上传以下产物：
     - `dist/MaaZero-v0.1.0.zip`（供普通用户下载刷入）
     - `bin/maa-managerd`（通道清单 `maa-managerd` 组件源）
     - `dist/maa-phone-project-0.1.0.tar.gz`（通道清单 `maa-phone-project` 组件源）
     - `dist/SHA256SUMS.txt`（公开校验值汇总）
   - 示例命令：`gh release create v0.1.0 dist/MaaZero-v0.1.0.zip bin/maa-managerd dist/maa-phone-project-0.1.0.tar.gz dist/SHA256SUMS.txt --title "MaaZero v0.1.0"`
4. **后续迭代升级规约**：
   - 每次更新 `stable.json` 或 `beta.json` 时，**必须递增 `--sequence` 序号**（如 `1 -> 2`），设备端 `store.AcceptSequence` 会拒绝序号回退的重放清单。

---

## 四、首发就绪核对表

- [x] 纯净可重建的 Debian Bookworm ARM64 `debian-rootfs-12.11.0-arm64.tar.gz`（预置 `maa-cli v0.7.8` 与 `MaaCore` 运行依赖库，通过 `install.StageRootfs` 校验）。
- [x] 接通完整 Linux/Android 运行驱动（挂载、启动、停止、无游戏健康检查、任务锁、开机自启 `StartBootstrap` 与首次安装自动部署 `runtime/rootfs`）。
- [x] 完成真机（Android 16 + KernelSU）冷启动重启拉起、官服客户端 APK 在线更新与明日方舟唤醒界面健康检测验收。
- [x] 完成卸载脚本保留 `/data/local/maa-platform` 用户数据与安全卸载挂载点验证。
- [x] 生成仓库外独立 Ed25519 签名密钥对，完成 `stable` / `beta` 通道清单签名与验签测试。
- [x] 通过 Go、Python、Node、PowerShell 与 221 项 E2E 测试（含纯 LF、无 BOM 与零密钥扫描）。
