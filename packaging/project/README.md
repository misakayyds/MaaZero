# 项目脚本打包层（`maa-phone-project`）

本目录负责将内置的业务运行脚本打包为确定性组件产物（`project/`、`VERSION`、`inventory.json`、`project.sha256`）。

## ⚠️ 内置脚本规范与安全审查声明（公开发布前必查）

> [!WARNING]
> `packaging/project/source/` 下内置的 28 个 `.py` / `.sh` / `.js` / `.json` 文件是从早期单机原型工程直接迁入的**初始快照**，仅用于本地预览构建、WebUI 注入回归测试与打包契约验证，**尚未完成面向通用多设备分发的规范化改造与安全终审**。在正式发布或接入稳定通道前，必须逐项完成以下审查与规范化：

1. **硬编码路径与单机环境假设清理（对应 `docs/runtime-dependency-inventory.md` R01 / R07）**
   - 审查 `maa-run.sh`、`maa-webui.py` 等脚本中的固定路径（如 `/root/maa-scripts`、`/root/.config/maa`、`/root/.local/share/maa`、`/var/log/maa`、`CHROOT_PATH`）与固定 `127.0.0.1:5555` ADB 端点。
   - 将程序只读目录与用户持久化数据目录、可写资源 overlay 彻底分离，禁止 `patch_config.py` 原位改写已签名的不可变组件目录。
2. **Android 宿主权限与进程控制边界收敛（对应 R02 / R03 / R05）**
   - 审查并移除业务脚本中直接通过 `nsenter -t 1` 操作 Android 宿主 shell、修改全局分辨率/旋转（`wm size`）、调用物理背光脚本（`/data/adb/maa/screen_backlight_off.sh`）及重启 `adbd` 的单机耦合逻辑，统一收敛至 `maa-managerd` 受控运行时驱动。
   - 将 `maa-webui.py` 与 `maa-run.sh` 中基于子串匹配的全局 `pkill -f` 替换为受管进程组/PID 隔离停止机制。
3. **更新通道与版本一致性收敛（对应 R06）**
   - 审查 `maa-auto-update.sh`、`maa-run.sh` 与 `maa-resource-overlay.sh` 中直接拉取上游 `main` / `beta` / `dev-v2` 或自更新 CLI/Core 的逻辑，确保正式分发环境下由 `maa-managerd` 签名清单统一决策版本，不出现双编排器冲突。
4. **定时调度与用户配置迁移规范（对应 R05 / R08）**
   - 审查 `maa-webui.py` 直接读写系统 `crontab` 的实现，明确容器内受管 cron 生命周期与升级/回滚配置兼容策略。
5. **代码规范、日志脱敏与许可证核验**
   - 统一 Shell（`shellcheck` / `bash -n`）与 Python（`ruff` / 类型与异常处理）编码规范、退出码契约。
   - 再次复核通知凭据（如 `tg.txt` 读取路径与异常输出脱敏）、诊断导出脱敏及随包静态数据（`maa-cultivation-data.json`、`infrast-operator-requirements.json`）的数据来源与许可证说明。

## 常用构建与校验命令

在仓库根目录执行：

```powershell
# 运行打包器与正向白名单测试
pwsh -File packaging/project/tests/test_build_project.ps1
pwsh -File packaging/project/tests/test_build_project_allowlist.ps1

# 仅校验内置脚本源与锚点（不写盘）
pwsh -File packaging/project/build-project.ps1 -Out .codex_diagnostics/project-check -Check
```
