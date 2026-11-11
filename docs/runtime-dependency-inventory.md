# 运行依赖与分发适配盘点

日期：2026-09-12。范围：本地结构化源和分发预览代码的静态核对；没有连接手机、执行项目脚本、构建 rootfs 或下载上游产物。本文是构建配方的输入，不是已经通过安装验收的依赖锁文件。

## 1. 结论

第一版环境包需要 Debian ARM64 userland、Python 3.11+、Bash、任务锁/超时等基础工具、ADB 客户端、cron，以及经过固定版本验证的 maa-cli/MaaCore/资源。Go 后台运行在 Android 侧，不要求用户安装 Go；现有 WebUI 不需要 Node.js 服务或前端构建环境。

先保留 chroot 内 `/root/maa-scripts` 等兼容路径，减少业务脚本迁移；外部环境根目录与用户数据绑定由新运行驱动管理，不复用现有手机 `/data/local/debian`。这是后续实现建议，不是本轮已经完成的挂载方案。

不能仅安装几个 Debian 包就称为可用：Android 控制、任务调度、可写资源与数据迁移、动态库、更新权限均有未闭合项。

## 2. 必需与条件依赖

以下软件包名称是配方候选，尚未通过固定 Debian 快照解算；不能作为可直接执行的 apt 安装命令。

| 层 | 依赖/候选包 | 源码依据与用途 | 首版处理 |
| --- | --- | --- | --- |
| Debian | ARM64 基础 userland、动态加载器、libc | 当前平台文档记录 Debian 12；需要执行 Linux ARM64 程序而非 Android APK | 以现有 Debian 12 为候选兼容基线，具体快照/安全维护状态/包版本下一步核定；不从私人手机导出 |
| Python | `python3`，解释器至少3.11 | `maa_task_config.py:7`、`maa-task-guard.py:6` 等直接导入 `tomllib` | 必需；版本不足必须拒绝环境验收 |
| Shell | `bash` | runner 使用 Bash 数组、`[[ ]]`、pipefail；`runtime/probes.go` 固定 `/bin/bash -n` | 不用 Android sh 或 BusyBox sh 冒充 Bash |
| 基础工具 | `coreutils`、`util-linux`、`grep`、`sed`、`mawk`、`findutils`、`procps` | `timeout`、`tee`、`tr`、`head`、`mkfifo`、`df`、`flock`、`nsenter`、`awk`、`find`、`ps/pkill` 等旧脚本调用 | 先保证兼容，后续按实际命令清单精简；打包工具不等于批准其所有运行行为 |
| 下载/解包 | `ca-certificates`、`curl`、`tar`、`gzip`；`git` 为旧更新路径候选 | runner/updater 的下载回退、资源 overlay 的 Git 操作 | 首版保留候选；受控更新迁移后再评估删减 git/curl，不能同时保留两套版本决策者 |
| Android连接 | Debian `adb` 客户端 | `maa-run.sh:186` 固定127.0.0.1:5555，预检/截图/游戏任务均依赖 | 必需；Android adbd 属系统服务，不是再下载一个 adbd 放入 Debian |
| 定时 | `cron` 中的 crontab 工具及 daemon | `maa-webui.py:146-171` 读写 crontab，周计划保存和恢复依赖它 | 为保持原 WebUI，首版建议由运行驱动管理一个 chroot cron，不另加第二个定时执行器；新用户默认无运行任务 |
| 时间/语言 | `tzdata`；`locales` 视是否保留en_US.UTF-8而定 | runner/updater 导出en_US.UTF-8，周计划依赖本地日期 | 明确生成locale或统一已提供的UTF-8 locale；时区初始化与计划计算需一致，不能照搬维护者的crontab |
| MAA | 固定 Linux ARM64 maa-cli、MaaCore、识别资源 | WebUI固定 `/root/.local/bin/maa`；runner调用 `maa run/update`；运行检查调用 `maa version/dir config` | 记录具体版本、产物哈希及许可证；当前并未选定生产版本 |
| Android引导 | Root管理器支持的shell、提供chroot能力的BusyBox或明确替代工具 | `runtime/probes.go` 验证BusyBox的chroot applet；模块脚本使用stat/awk/readlink等 | 独立于Debian包依赖，不能只凭Root标记就假定命令全部可用 |

### Python/OCR边界

对结构化目录16个 `.py` 文件进行 AST 静态导入扫描：显式导入均属于标准库或同目录项目模块，未发现必须通过 pip 安装的 Flask/FastAPI/requests/Pillow/OpenCV/numpy。`maa_cultivation_runtime.py:779` 的动态 sibling 加载，已核对调用点是 `maa_cultivation_service` 和 `maa_operator_box`。

这不等于 MaaCore 没有 OCR/推理依赖：本地参考源码 `MaaAssistantArknights/src/MaaCore/CMakeLists.txt:28` 链接 OpenCV、fastdeploy_ppocr、ONNXRuntime、ZLIB、Boost 等。不能据此直接列一套 Debian lib 包——静态/动态链接与随包库必须以最终下载产物的 ELF `DT_NEEDED`、RPATH/RUNPATH 及库闭包为准。当前没有检查生产二进制，因此 libc/libstdc++ 等精确最低版本仍未证明。

## 3. 项目文件清单

本轮登记23个候选项目文件，逐项列在 `runtime-project-files.json`。它是审计快照，尚未接入打包器，不是运行配置。构成：16个Python、3个Shell、2个JS、2个JSON。

注意一个前轮描述需要纠正：`packaging/project/build-project.ps1:182-218` 当前枚举源目录，使用扩展名白名单加文件排除规则，并非真正的逐文件正向名单。新出现的合法 `.py/.sh/.json/.js` 可能自动入包。正式构建前应改成消费明确文件列表，未知文件先阻断审核；本轮没有修改打包器。

排除：`arknights-apk-update.sh`、`maa-scheduled-reboot.sh`、`maa-edit.sh`、两个SSH watchdog、`start_webui.sh`，以及凭证、日志、缓存、截图、个人配置、上游源码树。23个候选文件仍需适配，不代表原样执行安全。

## 4. 必须适配的阻断项

| 编号 | 已证明的本地事实 | 后续实现要求 |
| --- | --- | --- |
| R01 | `maa-run.sh:186-188,232` 固定ADB地址、游戏包名和Android外部chroot路径；`CHROOT_PATH` 当前仅看到声明，不能据此断言它正在被使用 | 区分实际调用路径与遗留常量；连接端点和客户端选择由受控配置初始化，不要求用户手填 |
| R02 | `maa-run.sh:707-718` 通过nsenter执行Android shell并重启adbd；`service.d/adb_tcp.sh` 也直接开5555 | Android操作收敛为固定helper；验证授权、监听暴露范围和恢复原值，不把开启ADB TCP误当成仅回环安全 |
| R03 | runner修改旋转/分辨率，调用 `/data/adb/maa/screen_backlight_off.sh`；包含按字符串匹配的pkill | 背光定制默认不带；屏幕控制由明确任务授权触发，完整恢复；停止操作只针对自己的进程/进程组 |
| R04 | `service.d/start_debian.sh:8-9` 转调 `/data/local/tmp/debian_ctl.sh`；结构化Android目录没有该文件 | 本地无法证明现有挂载与卸载流程；新运行驱动必须明确/proc、/dev、必要/dev/pts、临时目录、DNS及数据挂载，不能复制一个缺失脚本的调用当作安装器 |
| R05 | WebUI读写crontab；`/api/kill`仍有全局pkill（`maa-webui.py:5117-5118`） | 保留计划语义，但由新驱动管理cron生命周期；停止行为隔离；所有入口共享真实 `/tmp/maa-task.lock` inode，避免重复执行 |
| R06 | `maa-auto-update.sh:18,22,150,162` 跟随main、默认beta、自更新CLI/Core；runner也有同类更新；overlay默认dev-v2 | 三处全部迁移到受控版本/策略，不能只禁用独立auto-update脚本；稳定版不允许绕过签名清单静默升级 |
| R07 | 配置位于/root/.config/maa，状态/root/.local/state/maa，缓存/root/.cache/maa，资源/root/.local/share/maa，日志/var/log/maa；`patch_config.py:36` 指定原位修改的资源config.json路径 | 持久数据独立于release；资源需要可写工作副本/overlay，不能把已签名不可变目录原位改写；回退不覆盖用户计划/库存 |
| R08 | 当前运行检查只验证部分文件、Bash语法、maa版本及配置路径；未验证Python版本、全部命令与动态库闭包 | 扩充无游戏预检；`--startup-health-check`实际会运行StartUp并启动游戏，不能当作无游戏安装自检 |
| R09 | rootfs解包拒绝link成员之下的归档路径、特殊文件，并忽略uid/gid、剥离特殊权限；只支持tar.gz | 配方规范化merged-usr，不包含设备节点；检验所有者/权限取舍是否支持所选运行方式；不能称为与任意完整Debian归档等价 |

## 5. 不带入默认环境的内容

不带SSH服务器和密钥、Telegram凭证、用户计划/库存/游戏账号、整机Wi-Fi恢复、充电/HAL控制、定期重启、APK覆盖安装和旧手机备份。不安装桌面/X11/浏览器、Node/npm或Go编译器；这些不是现有服务端运行依赖。构建阶段可使用工具，但不得混入用户rootfs。

不把 `nc` 简单写成“Debian装netcat即可”：现有截图命令的nc/gzip/screencap运行在Android shell一侧，必须按设备能力探测；不能用Debian包满足Android命令缺失。

## 6. 下一步构建配方的交付标准

1. 核定Debian官方快照与精确包锁、maa-cli/Core/资源产物及哈希；不复制示例清单的版本和虚假哈希，不声明追随latest/main即可复现。
2. 建立构建输入锁文件与rootfs配方；本轮的23文件快照接入真正的正向名单测试。具体包版本由解算生成后审阅，不手编不存在的版本号。
3. 验证Python>=3.11、基础命令、locale/时区、ELF库闭包；禁止服务在构建时自启，不执行游戏或手机控制脚本。
4. 输出归一化tar.gz、软件包清单/许可证、文件哈希；用当前安全解包器完整解包验证，而非仅检查tar能列文件。
5. 随后实现R01-R09涉及的运行接线与数据映射；所有依赖就绪之前CLI保持nil Driver，不能用假健康结果开启安装。

本轮只完成依赖与阻断项盘点。没有正式版本锁、rootfs产物、手机安装证据或云端发布；精确下载量、磁盘预算和手机内存需求仍需真实构建/测量。
