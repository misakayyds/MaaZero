# 后台管理程序

`maa-managerd`将是本产品唯一的特权编排者，计划使用静态ARM64程序实现。

它负责设备探测、签名清单验证、下载与校验、版本化安装、事务恢复、任务监督、更新、回滚、诊断和本地API。它不接受任意shell字符串或任意下载URL，只暴露固定操作。

第一阶段通过适配器调用现有MAA脚本，不重写任务领域逻辑。具体文件和测试顺序见 `../docs/implementation-plan.md`。

## 当前实现

Task 1已提供无第三方依赖的 `internal/manifest` 包：

- Ed25519分离签名验证，覆盖清单原始字节。
- 最大1 MiB输入限制和未知字段拒绝。
- Schema v1、通道、递增序号、组件、兼容范围和上游策略语义校验。
- 具体版本HTTPS下载路径、SHA-256和文件大小校验。
- 按设备架构、Android API和Root管理器选择产物。
- 紧急阻断版本拒绝。

`cmd/maa-managerd` 已提供 `version`、`serve`、`open-ui` 和 `stop`。除版本查询外，仅允许 Linux root 环境；HTTP 只绑定回环。模块入口通过私有 Unix socket 领取一次性入口票据，票据换取内存会话，不写入文件。它仍是未完成运行集成的预览程序，不是正式守护进程交付。

2026-09-12 新增基础库：

- `internal/store`：单写者锁、原子版本/事务状态、失败或重启回退、通道序号防回放。逻辑指针在 `transactions/state.json`，不修改用户数据，不自动清理旧版本。
- `internal/download`：HTTPS、Range 续传、镜像回退、大小/完整 SHA-256 校验、取消与进度。调用方必须传入已验签并通过兼容筛选的产物，并串行访问私有下载目录。
- `internal/install`：项目 `tar.gz` 严格解包、原始 manager 二进制暂存，以及单独的 rootfs `tar.gz` 链接安全策略。真实 Debian 配方和运行激活未完成，不支持 ZIP / tar.zst。

2026-09-12 新增设备探测：

`internal/device` 提供只读探测 `device.Probe()`。它通过固定属性白名单调用 `/system/bin/getprop`，读取固定 Root 管理器标记路径、uid、`statfs` 空闲空间与 SELinux 状态，并以 fixture 注入方式本地测试。`Facts.DeviceFacts`（`manifest.DeviceFacts` 字段）直接供 `SelectArtifact` 使用；`Facts.Supported()` 只代表技术前置条件齐备，不代表实机验收。非 Linux 主机返回 `ErrUnsupportedHost`。

`internal/api` / `internal/control` 已连接签名检查、版本状态和带测试驱动的项目更新事务；模块启动命令、公开配置和 UI 静态资产已对齐。CLI 故意不提供真实运行 Driver，默认恢复模式，不能执行安装或更新。`internal/runtime` 目前只有固定只读探测、包检查和任务锁，不具备启动/停止/激活 Debian 的完整能力。

Store 不代替运行任务锁；阶段推进不代替实际下载校验；恢复指针不等于旧环境已通过健康检查。Windows 测试通过和 Linux/ARM64 交叉编译不等于 Android 实机验收。
