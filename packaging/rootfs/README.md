# Debian 基础环境候选构建器

这是维护者工具，不需要最终用户配置。当前输出是 **raw Debian base candidate**，不是可安装 MAA 环境，不接模块更新源。不会连接手机、启动 Docker、安装宿主软件、启动游戏或发布资产。

## 本机检查配方（无需下载）

在项目根目录执行：

```powershell
python -B packaging/rootfs/test_recipe.py
python -B packaging/rootfs/recipe.py --output .codex_diagnostics/rootfs-candidate
```

默认只输出计划，不创建目录，也不下载。Windows 显示的是本机路径，不能把整段 argv 复制到 Linux；在 Linux 工作副本重新运行入口。`runtime_ready=false`、`snapshot_verified=false` 和 `dependency_closure_locked=false` 是刻意保留的边界。

## 真构建前置条件

独立、可信的原生 ARM64 Linux 构建机；非 root 用户可用 user namespaces/subuid/subgid；预先提供 mmdebstrap、APT/dpkg、gpgv、chroot、newuidmap/newgidmap、Debian archive keyring、Python 3.11+。输出卷和构建机 `/tmp` 至少各有 6 GiB 可用空间（同卷不代表总共只需6GiB），这不是手机安装空间预算。工具不会自动安装这些软件或提权。缺项会拒绝，命名空间等实际错误保留在构建日志中。

2026-09-12 本机检查：Docker Linux engine 未运行；Ubuntu WSL 为 x86_64，未找到 mmdebstrap/debootstrap/qemu-aarch64-static。故本轮不具备上述真构建条件，不冒充已经生成 rootfs。

在已经准备好的构建机、项目根目录执行（父目录应已存在，输出目录必须是新名称）：

```sh
python3 -B packaging/rootfs/recipe.py --output /path/to/builds/base-candidate-001 --build
```

构建会下载 Debian 软件包并在隔离 user namespace 中执行包安装脚本；应在专用构建环境中运行。保留 mmdebstrap 默认服务启动阻断，不执行宿主服务控制。失败不覆盖/清理已有目录；修复后换新输出名称重试。

产物：`debian-base.raw.tar`、`build-plan.json`、`build.log`、成功后才有的 `build-result.json`（哈希/大小/实际构建器版本/keyring摘要）。没有结果JSON就不是成功构建，任何阶段失败均需换新目录重试；构建中tar使用 `.partial` 后缀。父进程通过已打开的文件描述符接收tar，不放宽输出目录0700权限。hook文本内嵌到固定命令参数，避免隔离UID依赖工作副本目录读权限。包版本清单在 tar 内的 `usr/share/maa-build/packages.tsv`；APT lists 中保留签名仓库元数据用于后续审计。后处理写固定 hostname/hosts，清空构建机 DNS/machine-id，生成 en_US.UTF-8。运行时 DNS 必须由后续 Driver 接入。keyring摘要仅记录构建期信任根，尚不构成受签名发布锁；构建机仍必须可信。

## 固定什么，尚未固定什么

`recipe.json` 固定 bookworm、arm64、候选快照时间点 `20260901T000000Z` 和19个直接包。此时间点的网络可用性尚未验证：本轮网页读取失败，Windows curl TLS 凭证层失败；未绕过 TLS。它是**候选输入而非发布锁**，快照时间点也不等于各 archive 实际快照时间。构建必须通过 APT 的 Debian 签名/索引/包校验，不允许退回普通浮动镜像或使用 trusted=yes。仅关闭历史快照的 Valid-Until 过期检查，不禁用签名检查。具体行为参见 [Debian mmdebstrap 官方手册](https://manpages.debian.org/bookworm/mmdebstrap/mmdebstrap.1.en.html)。

尚未完成：签名元数据摘要与全部间接包闭包锁、构建工具版本锁、两次独立构建比较、maa-cli/MaaCore/资源的固定版本与哈希、原生库闭包、符合 manager 策略的 tar.gz 归一化及解包验收、配置迁移和 Android Driver。因此即使本脚本成功，也不能标记为 `runtime_ready=true` 或挂到正式更新源。

维护分工：项目文件名单仅在增删文件时修改；Debian 配方低频因安全/兼容问题升级；MAA/Core/资源跟随经过验证的上游版本；每次发版生成版本/哈希/签名清单。已发布资产保持不变，用新版本修复，不覆盖旧文件。
