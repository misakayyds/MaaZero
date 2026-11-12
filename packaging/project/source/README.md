# 内置项目脚本源

本目录存放 `packaging/project/project-files.json` 正向白名单所列的业务运行脚本与静态数据文件，作为独立仓库的默认打包输入源（`build-project.ps1` 默认只读读取本目录，自动忽略非白名单的 `.md` 说明文件）。

**维护约束**：
- 严禁向本目录写入任何通知凭据（如 `tg.txt`）、私钥、内网拓扑信息或单机硬件改动脚本。
- 所有路径定位与进程管理须遵守 `../README.md` 与 `../../../docs/release-checklist.md` 中的沙箱隔离与安全规范。
