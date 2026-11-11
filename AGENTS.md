# MAA Phone Manager

面向普通 Root ARM64 Android 用户的 MAA 一体化环境编排器、最小 Root 引导模块与分层签名分发系统（当前处于预览开发阶段）。

<!-- agent-kit:session-protocol:v2 -->
## 会话协议

- 首次接手本项目时读取本文件;按任务需要读取 docs/agent/context.md 和 progress.md 最近相关条目。已在当前会话读过且未变化的内容不重复加载。
- 修改前检查相关文件和 git status --short (若有 Git);提交历史仅在追溯变化时读取。不为普通问答启动完整项目流程。
- 日志是历史参考,当前代码和实际检查用于确认现状。旧格式日志不重写,新日志使用当前轻量模板。
- 用户已明确授权的任务,简述必要计划后直接推进。仅缺少关键输入或涉及破坏性命令、生产变更、删除文件、force-push 等需审批操作时请求确认;沿用本会话已有的明确授权。
- 有实质变更、重要发现、阻塞或待交接事项时才记录进度;普通问答、无新发现的只读检查不强制落盘。重要长期决策写入 decisions.md,不重复抄写进度。
- 不自动提交或推送;仅在用户明确要求时执行,保留无关改动。任何文件不写密钥 / token / 密码。
<!-- /agent-kit:session-protocol:v2 -->

<!-- agent-kit:identity-rules:v2 -->
## 进度日志

- 标题: `YYYY-MM-DD HH:mm ±TZ — Agent / 环境`。
- 示例: `2026-07-01 23:28 +0800 — Codex / Windows`。实际写入时读取当前时间和时区,填写真实 Agent 与执行环境,不要照抄示例。
- 正文建议字段:完成 / 改动 / 验证 / 风险 / 下一步。按需合并或省略空项,区分已验证与未验证。
- 不要求模型或会话 ID;确有必要时放正文,不放标题。
- 写入前重读文件顶部;将新条目插在格式说明块之后、首条历史记录之前。保留旧日志,更正用新条目。
- 多 Agent 并行时指定一个写入者汇总;发现文件已变化则重读后合并,不得整文件覆盖其他人的新增内容。
<!-- /agent-kit:identity-rules:v2 -->

## 命令
- Go 后台测试与静态检查（在 `manager/` 目录）: `go test ./... -count=1`、`go vet ./...`
- WebUI 叠加与单元验证: `python -B -m unittest discover -s packaging/tests -v`、`node --test webui/tests/module-settings.test.mjs`
- Rootfs 配方离线检查: `python -B packaging/rootfs/test_recipe.py`
- 模块布局与打包器验证:
  - `powershell -ExecutionPolicy Bypass -File module/tests/test_module_layout.ps1`
  - `powershell -ExecutionPolicy Bypass -File packaging/test-module.ps1`
  - `powershell -ExecutionPolicy Bypass -File packaging/project/tests/test_build_project.ps1`
  - `powershell -ExecutionPolicy Bypass -File packaging/project/tests/test_build_project_allowlist.ps1`

## 约定 / 红线
- **内置脚本待审查门禁**: `packaging/project/source/` 下的 28 个业务脚本是从单机原型仓库迁入的初始快照，**尚未完成通用化路径解耦、Android 宿主边界收敛与代码规范终审**（详见 `packaging/project/README.md` 与 `docs/runtime-dependency-inventory.md` R01–R09）。未完成规范审查与真机隔离验收前，禁止将预览构建伪装为可交付普通用户的正式版。
- **行尾与编码**: 全仓源码与模块脚本强制使用纯 LF（UTF-8 无 BOM），尤其是 `module/` 下所有文件不得含任何 CR (`\r`) 字节。
- **安全与密钥红线**: 任何仓库文件、测试 fixture、打包产物中禁止写入真实签名私钥、Telegram Token、SSH 密钥或个人内网拓扑信息。
- **模块最小边界**: Root 模块（`module/`）只负责引导、开机启动 `127.0.0.1:8088` 守护与救援入口；卸载时必须保留 `/data/local/maa-platform` 用户数据。
