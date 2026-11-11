# WebUI

WebUI复用现有 MAA 页面，是后台管理程序的本地客户端，不是第二个编排器，也不另建日常控制台。2026-09-12 用户确认只新增“关于与模块设置”，不改变既有首页、周计划、基建或培养操作。

新增区域的目标范围（不等于当前全部实现）：

- 首次设备检查和一键安装。
- 组件下载与安装进度。
- 当前状态、下一计划和最近结果。
- 稳定/测试更新通道。
- 检查更新、应用更新、回退上一版。
- 导出诊断包。
- 内置离线教程和常见问题。

未安装环境时由模块/后台最小引导页提供安装向导；MAA/Debian 故障时保留救援说明。该页面不是替代现有 WebUI 的第二套计划编辑器。浏览器版本和模块 WebView 版本共享业务界面，适配层只处理安全会话和入口。

WebUI不得直接执行Root命令、修改系统目录、接受任意URL或把浏览器本地存储当作用户配置真相源。

## 本地实现状态（2026-09-12）

「关于与模块设置」扩展资产已本地实现并有测试，但尚未接入运行中的后台。

- `module-settings.js`：唯一新增的导航项。通过包装宿主页 `render()` 接入，不改 `maa-webui.py` 里 `go()` 的 `runPending` 守卫、周计划草稿和既有页面渲染；所有服务端字符串用 `textContent` 写入，不使用 `innerHTML`，也不读写 `localStorage`/Cookie。
- `module-settings.css`：只使用既有主题变量（`--surface*`/`--border`/`--text*`/`--accent*`/`--success`/`--danger`/`--warning`），全部 `.ms-` 命名空间。
- 固定接口：`GET /api/v1/status`、`GET /api/v1/help`、`POST /api/v1/update/check`（体为空对象）、`POST /api/v1/update/apply` 与 `POST /api/v1/rollback`（体为 `confirm=true`，先弹确认）。全部 `credentials: same-origin`、`Content-Type: application/json`、`X-MAA-Request: 1`。更新异步执行，只有本页打开时才轮询 `/api/v1/status`。
- 诚实边界：`configured=false` 或 `can_install=false` 时禁用全部更新操作并显示原因；网络失败或 `ok:false` 一律显示失败，不制造成功状态。当前 CLI 没有真实运行驱动，固定不开放安装。通道切换、诊断导出和首次安装向导尚未实现。

注入点（唯一）：生产由 `manager/internal/api/server.go` 的反向代理在 `GET /` 的 HTML 响应里插入 `/module-assets/module-settings.css` 与 `/module-assets/module-settings.js`，并从 `Config.Assets` 提供这两个文件。`packaging/webui_overlay.py` 是同一注入的参照实现与本地 fixture 生成器，**不要**与 Go 注入同时启用（会重复注入）。

本地验证：

```powershell
node --test webui/tests/module-settings.test.mjs       # 零 npm 依赖
python -B packaging/tests/test_webui_overlay.py        # 真实 maa-webui.py 页面
node webui/tests/module-settings.browser.mjs           # 真 Chromium，要求可用 Playwright
```

未证明：没有与真实 `maa-managerd` 联调（浏览器测试用本地 stub API），只验证了 Chromium，帮助正文按纯文本显示。
