# 维护者：预览构建与后续更新分发

## 当前可以做的事

只构建明确带 `preview` 的本地模块 ZIP，不创建 GitHub Release、不推送分支、不上传文件、不使用签名私钥。打包器拒绝非 ARM64 静态 ELF、非预览版本、未知文件和不合法公开配置；默认不带发布源，后台保持恢复模式。

在仓库根目录使用 PowerShell 7：

```powershell
$env:GOOS = 'linux'
$env:GOARCH = 'arm64'
$env:CGO_ENABLED = '0'
Push-Location manager
go build -trimpath -o ../.codex_diagnostics/maa-managerd-preview-linux-arm64 ./cmd/maa-managerd
if ($LASTEXITCODE -ne 0) { throw 'ARM64 build failed' }
Pop-Location
pwsh -File packaging/build-module.ps1 -ManagerBinary .codex_diagnostics/maa-managerd-preview-linux-arm64 -Version 0.1.0-preview -OutputDirectory .codex_diagnostics/module-preview
if ($LASTEXITCODE -ne 0) { throw 'Module packaging failed' }
```

脚本会拒绝覆盖已有 ZIP；复测时换新的输出目录。结束交叉编译后，在新的终端运行本机测试，避免残留 `GOOS/GOARCH` 导致测试无法执行。ZIP 自带离线教程和文件清单，不包含游戏、Debian、用户数据或现有手机配置。

仓库提供手动触发的 `maa-phone-manager-preview` 工作流，仅运行测试、构建预览 ZIP 并保留 CI artifact；没有发布写权限、签名或正式通道推广。工作流需用户决定推送后才可能执行，本地存在 YAML 不代表远端 CI 已运行。

## 未来怎么方便推送

维护名单入口：`packaging/project/project-files.json` 是项目文件生产白名单（增删文件才改）；`packaging/rootfs/recipe.json` 是基础环境候选输入（安全/兼容更新时升级）；最终版本/大小/哈希/签名清单应由发布构建生成，当前发布锁流水线未完成。`docs/runtime-project-files.json` 是历史审计快照，不作为另一份手工生产名单维护。

采用“不可变版本文件 + 小型签名通道清单”，无需常驻服务器。普通更新主要替换后台/项目组件，模块本身只在引导层变更时升级。用户在原 WebUI 的“关于与模块设置”查看版本并确认更新，数据目录不跟着程序版本替换。

维护者顺序：构建并测试 → 上传带具体版本的产物 → 生成大小和 SHA-256 → 受保护环境签名 beta 清单 → 备用设备验收 → 签名发布 stable 清单。安装器只信任内置公钥，不接受用户页面传入任意下载地址或命令。不要把 `example-manifest.json` 当成真实下载源。

当前尚未实现生产清单生成/签名发布流水线、自动检查周期、通知推送和正式推广；不能承诺现在点一下即可给用户推送更新。私钥只由受保护发布环境提供，任何仓库文件、教程、构建产物或日志中都不得保存私钥。

## 正式发布阻断项

基础环境候选构建入口及维护教程见 `../packaging/rootfs/README.md`。目前只有离线校验和受前置条件约束的 raw Debian 构建入口；快照/完整依赖闭包/上游组件锁及真实构建仍未验收，不能勾掉下面第一项。预览 CI 仅新增离线配方测试和 shell 语法检查，不构建或发布 rootfs。

- [ ] 固定可重建的 Debian rootfs 配方及 MAA 依赖/CLI 版本和许可清单。
- [ ] 接通真实运行驱动：挂载、启动、停止、无游戏健康检查、任务锁及失败恢复；当前 CLI 的 nil driver 不能改成假成功。
- [ ] 完成 `packaging/project/source/` 内置 28 个原型脚本的规范化与安全终审（见 `../packaging/project/README.md` 与 `runtime-dependency-inventory.md` R01–R09）：迁移旧运行器硬编码路径、ADB、`nsenter`/`pkill`、自更新通道与原位资源改写逻辑，不要把白名单项目目录直接当成已通过通用环境审查的生产代码。
- [ ] 明确配置迁移和数据目录挂载，验证计划/培养数据升级后保留。
- [ ] 空间预算、崩溃暂存清理和完整断电恢复策略。
- [ ] 真机验收安装、模块入口、冷启动、升级、离线回滚、失败恢复及卸载保留数据。
- [ ] 生产签名源、组件物料/许可证、稳定通道推广和诊断脱敏验收。
- [ ] 根据真实手机界面完成小白安装教程和截图。

这些是未完成实现或未取得证据的事项，不是通过修改文档即可解除的限制。实机安装、系统改动和外部发布需要单独授权。
