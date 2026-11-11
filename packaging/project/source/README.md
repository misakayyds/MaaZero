# 内置项目脚本源（待规范化审查）

本目录存放 `packaging/project/project-files.json` 正向白名单所列的 28 个业务运行脚本与静态数据文件，作为独立仓库的默认打包输入源（`build-project.ps1` 默认只读读取本目录，自动忽略非白名单的 `.md` 说明文件）。

**重要提示**：
- 本目录脚本系从单机原型工程迁入的初始快照（已剔除 `tg.txt` 凭据与 `arknights-apk-update.sh`、`maa-scheduled-reboot.sh`、`ssh_watchdog*.sh` 等设备级高风险改动脚本）。
- **本目录代码尚未完成通用化与规范化终审**，仍包含单机阶段的目录布局、ADB/chroot 假设、`nsenter`/`pkill` 调用及旧自更新入口。
- 公开发布或接入真实安装驱动前，必须按 `../README.md` 与 `../../../docs/runtime-dependency-inventory.md`（R01–R09）完成逐文件审查与重构。
