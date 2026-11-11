# 关键决策

## 2026-11-11 — 拆分为独立仓库并内置 28 个业务脚本初始快照（设规范审查门禁）
- 选了什么: 将 `maa-phone-manager` 从原单机维护仓库拆出至同级独立仓库 `E:\01_Projects\Active\maa-phone-manager`（干净初始提交），并将 `packaging/project/project-files.json` 正向白名单中的 28 个业务脚本内置到 `packaging/project/source/`，同时在 `README.md`、`packaging/project/README.md`、`docs/release-checklist.md` 与 `AGENTS.md` 明确标记“待进一步规范与安全审查”。
- 为什么(以及否决了哪些替代方案): 独立对外发布要求仓库在不依赖原私有单机仓库 `.codex_remote_maa/` 的前提下能自包含完成打包、WebUI 注入测试与 CI 验证；仅剔除凭据和设备改动脚本直接迁入可立即保持现有契约与测试闭环，但脚本内部仍残留单机路径、`nsenter`/`pkill` 与自更新逻辑（R01–R09），因此必须通过显式文档门禁防止误将初始快照当作已规范化的通用生产脚本发布。
