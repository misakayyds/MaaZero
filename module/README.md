# Root 模块

本目录后续承载兼容 Magisk 风格的最小安装模块。

职责仅限：

- 安装和启动救援版 `maa-managerd`。
- 检查ABI、Android API和Root管理器基本兼容性。
- 提供管理页面入口、服务停用和回退入口。
- 模块卸载时停止服务，默认保留用户数据。

本目录禁止放入完整Debian rootfs、MaaCore资源、用户配置、凭证、日志或设备备份。不同Root管理器的差异应封装在适配脚本内，公共安装状态由后台程序管理。

## 当前实现（2026-09-12，本地预览）

| 文件 | 职责 |
| --- | --- |
| `module.prop` | 稳定 `id=maa-phone-manager`；`version=0.0.0-preview` 只是占位，打包时被替换 |
| `customize.sh` | 安装期只读校验：arm64、Android API >= 30、`bin/maa-managerd` 存在且为 ELF，然后设置权限 |
| `service.sh` | `umask 077` 后再建数据目录（后台程序拒绝 0755；不改现存目录权限，`$DATA_ROOT`/`$LOG_DIR` 是 symlink 则拒绝启动），`nohup` 非阻塞拉起 `serve`，命令行固定为 `--data-root /data/local/maa-platform --listen 127.0.0.1:8088 --config "$MODDIR/release.json" --assets "$MODDIR/webroot/assets"`；只有该 PID 自己持有 127.0.0.1:8088 的监听 socket 才写 `started`，daemon stdout 不进日志 |
| `action.sh` | 固定调用 `open-ui --data-root /data/local/maa-platform` |
| `uninstall.sh` | 先调用二进制 `stop`，再按精确 argv 兜底结束 `serve`；保留全部用户数据 |
| `webroot/index.html` | KernelSU/APatch 入口：只调用固定 `open-ui` 命令与固定本地链接，无任何输入控件；`ksu.exec` 按官方桥签名传回调函数名字符串 |
| `tests/test_module_layout.ps1` | 150 项布局与边界断言（LF、稳定 ID、回环监听、固定命令、目录权限门禁、无凭证、无输入控件） |

`bin/maa-managerd` 不在源树内，由 `packaging/build-module.ps1` 在打包时提供；缺失 `release.json` 时由后台程序进入救援模式，脚本不做判断。当前是本地预览，未在真机安装或验收。
