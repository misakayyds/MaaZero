# MAA Phone Manager

本仓库用于把早期单机定制的 `rooted Android + Debian chroot + MAA 脚本` 整理成面向普通 Root 用户的独立可分发项目。

目标用户只有一台已经 Root 的 ARM64 Android 手机，不需要预先安装 Debian、ADB、`maa-cli`，也不需要使用终端。用户从 Release 下载 Root 管理器模块，重启后通过模块入口打开 WebUI，由后台管理程序完成环境检查、组件下载、安装、运行、更新、监视和回滚。

> [!WARNING]
> **内置业务脚本待规范化审查**：`packaging/project/source/` 下内置的 28 个业务脚本与静态数据是从单机原型仓库拆出时的初始快照，当前仅用于预览打包与 WebUI 叠加回归测试，**尚未完成通用化路径解耦、Android 宿主权限边界收敛与公开发布规范终审**。对外正式发布或接通真实安装驱动前，必须先按 [项目脚本打包与审查清单](packaging/project/README.md) 及 [运行依赖与适配盘点](docs/runtime-dependency-inventory.md)（R01–R09）逐项完成审查与重构。

## 当前阶段

已实现本地预览版后台入口、最小 Root 模块、原 WebUI 的“关于与模块设置”、签名清单校验、版本事务、HTTPS 下载、安全解包和白名单项目打包。可以构建预览 ZIP，但它不是可交付普通用户的一键安装版本：后台尚未接通真实 Debian 启停及 MAA 运行驱动，默认只提供恢复入口，安装/更新执行保持禁用。没有实机验收，没有正式发布源，未修改或部署现有手机运行链。

预览构建与后续推送流程见 [维护者说明](docs/release-checklist.md)，支持状态见 [兼容性边界](docs/compatibility.md)。不要把模块 ZIP 构建成功等同于 MAA 环境安装成功。

用户已确认直接复用现有 MAA WebUI，只新增“关于与模块设置”，保留首页、周计划、基建和培养操作；不重建第二套总控。首次安装/故障恢复使用最小引导页。教程草案见 [使用说明](docs/user-guide.md)。

## 核心边界

- Root 管理器模块只负责引导、开机启动、打开管理页和救援，不承载完整 Debian 或 MAA 资源。
- `maa-managerd` 是唯一编排者，负责下载、校验、安装、任务调度、状态采集和回滚。
- WebUI 只调用后台接口，不直接拼接或执行 Root 命令。
- 用户配置、运行数据和日志与程序版本分离，升级模块或程序不得覆盖用户数据。
- 云端可以只有静态文件，无需数据库或常驻业务服务器。
- 大文件可以全部托管在 GitHub Releases、官方上游发布页或其他 HTTPS 对象存储；我们维护清单、签名、校验值、兼容规则和下载源优先级。
- 下载地址必须指向不可变的具体版本，不允许生产清单依赖 `latest`。
- 第一次发布只支持明确验收过的 ARM64 Android、Root 管理器和系统版本组合；“模块能刷入”不等于“设备受支持”。

## 目录

```text
maa-phone-manager/
├── .github/workflows/          # 手动预览构建与多平台测试工作流
├── README.md
├── docs/                       # 架构、更新回滚、依赖盘点与发布检查表
├── manifest/                   # 签名清单 Schema、通道目录与示例清单
├── manager/                    # Go 编写的 maa-managerd 后台编排器
├── module/                     # 最小 Magisk / KernelSU / APatch 引导模块
├── packaging/
│   ├── build-module.ps1        # 预览模块 ZIP 构建器
│   ├── webui_overlay.py        # WebUI 模块设置页注入参考实现与测试生成器
│   ├── project/                # 业务脚本白名单打包器与内置待审查脚本源 (source/)
│   └── rootfs/                 # Debian ARM64 基础环境候选配方
└── webui/                      # 「关于与模块设置」前端扩展与测试
```

## 文档入口

- [总体架构](docs/architecture.md)
- [小白用户流程](docs/user-flow.md)
- [云端维护范围](docs/cloud-maintenance.md)
- [更新与回滚](docs/update-and-rollback.md)
- [实施计划](docs/implementation-plan.md)
- [运行依赖与适配盘点](docs/runtime-dependency-inventory.md)
- [内置项目脚本与待审查规范](packaging/project/README.md)
- [发布与预览构建清单](docs/release-checklist.md)
- [清单格式说明](manifest/README.md)

## 内置脚本快照与上游关系

本仓库已从原单机维护仓库拆分为独立 Git 仓库，并将 [project-files.json](packaging/project/project-files.json) 白名单内的 28 个业务脚本内置到 `packaging/project/source/` 以实现自包含构建与测试（已排除任何凭据及设备级高风险改动脚本）。该内置脚本快照仍保留部分单机原型假设，必须按 [packaging/project/README.md](packaging/project/README.md) 完成进一步代码与安全规范审查后方可进入正式分发。
