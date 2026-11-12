# 项目脚本打包层（`maa-phone-project`）

本目录负责将内置的业务运行脚本打包为确定性组件产物（`project/`、`VERSION`、`inventory.json`、`project.sha256`）。

## 内置脚本规范与安全边界（公开发布必查）

`packaging/project/source/` 下内置的 `.py` / `.sh` / `.js` / `.json` 文件由 `project-files.json` 正向白名单严格约束。修改或新增脚本并发布正式版本前，必须确保满足以下规范与安全要求（详见 `../../docs/release-checklist.md`）：

1. **路径解耦与只读发布目录隔离**
   - 业务脚本通过环境变量（`MAA_HOME`、`MAA_CONFIG_DIR`、`MAA_DATA_DIR`、`MAA_STATE_DIR`、`MAA_CACHE_DIR`、`MAA_LOG_DIR`、`MAA_ADB_ADDR`）定位目录与端点。
   - 程序只读目录与用户持久化数据目录、可写资源 overlay 彻底分离，禁止原位改写已签名的不可变组件目录。
2. **Android 宿主权限与进程控制边界收敛**
   - 禁止业务脚本通过 `nsenter` 逃逸操作 Android 宿主 shell、调用单机物理背光或整机重启脚本。
   - 禁止使用全局 `pkill -f`，统一通过受管进程组与 PID/任务锁隔离启停。
3. **更新通道与版本一致性收敛**
   - 正式分发环境下由 `maa-managerd` 签名清单统一决策组件版本，避免不受控的双编排器冲突。
4. **定时调度与用户配置兼容**
   - 容器内 `cron` 生命周期由运行驱动托管，升级或回滚不得覆盖用户配置与库存数据。
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
