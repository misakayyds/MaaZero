# 项目静态事实

## 这是什么
面向普通已 Root ARM64 Android 用户的 MAA 分发与本地管理项目（独立仓库 `E:\01_Projects\Active\maa-phone-manager`），包含最小 Magisk/KernelSU/APatch 引导模块、静态编译 Go 编排守护进程 `maa-managerd`、既有 MAA WebUI 的「关于与模块设置」扩展层、Ed25519 签名版本清单校验与确定性组件打包工具。

## 技术栈 / 架构
- 编排后台（`manager/`）：Go 1.24+（`CGO_ENABLED=0` 静态编译 `linux/arm64`），提供签名清单校验、下载校验、版本目录事务切换、WebUI 反代与模块设置 API。
- Root 引导模块（`module/`）：POSIX `/system/bin/sh` 脚本与 KernelSU/APatch WebUI 引导页，仅监听 `127.0.0.1:8088` 并将运行时数据置于 `/data/local/maa-platform`。
- 前端扩展（`webui/` + `packaging/webui_overlay.py`）：原生 ES/DOM 脚本与独立 `.ms-` 样式，通过反代在原 WebUI `</body>` 前单一注入。
- 业务脚本与环境打包（`packaging/project/`、`packaging/rootfs/`）：PowerShell 确定性打包器、28 文件正向白名单（`project-files.json`）、内置待审查脚本源（`packaging/project/source/`）及 Debian Bookworm ARM64 离线候选配方。

## 关键命令
- `cd manager && go test ./... -count=1 && go vet ./...`
- `python -B packaging/rootfs/test_recipe.py`
- `python -B -m unittest discover -s packaging/tests -v`
- `node --test webui/tests/module-settings.test.mjs`
- `powershell -ExecutionPolicy Bypass -File module/tests/test_module_layout.ps1`
- `powershell -ExecutionPolicy Bypass -File packaging/test-module.ps1`
- `powershell -ExecutionPolicy Bypass -File packaging/project/tests/test_build_project.ps1`
- `powershell -ExecutionPolicy Bypass -File packaging/project/tests/test_build_project_allowlist.ps1`

## 密钥位置(只写路径,不写密钥本身)
- 发布清单签名私钥：待确认（仅在受保护发布环境持有，绝不进入本仓库）。

## 绝不触碰
- 禁止在仓库中提交任何私钥（`*.pem`、`*.key`）、通知凭证（`tg.txt`）或真实用户配置数据。
- 未完成 `packaging/project/README.md` 所列规范与安全审查及 `docs/release-checklist.md` 阻断项前，禁止移除预览版本门禁（`-preview`）或用空操作驱动伪造安装成功。
- 禁止向 `module/` 引入 CRLF 或 UTF-8 BOM。
