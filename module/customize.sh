#!/system/bin/sh
# MAA Phone Manager (preview) install-time validation.
# 安装期只做只读校验和权限设置：不写系统分区，不下载，不安装 Debian rootfs 或游戏资源。
# Magisk / KernelSU / APatch 都提供 ui_print、abort 和 $MODPATH。

MIN_API=30
REQUIRED_ARCH=arm64

ui_print "*******************************"
ui_print " MAA Phone Manager 本地预览"
ui_print " 只安装引导与救援程序，不含运行环境"
ui_print "*******************************"

if [ -z "$ARCH" ]; then
  ARCH="$(getprop ro.product.cpu.abi 2>/dev/null)"
fi
case "$ARCH" in
  arm64|arm64-v8a) ;;
  *) abort "! 只支持 ${REQUIRED_ARCH} 设备，当前 ARCH=$ARCH" ;;
esac

if [ -z "$API" ]; then
  API="$(getprop ro.build.version.sdk 2>/dev/null)"
fi
case "$API" in
  ""|*[!0-9]*) abort "! 无法读取 Android API 版本（API=$API）" ;;
esac
if [ "$API" -lt "$MIN_API" ]; then
  abort "! 需要 Android API >= $MIN_API（Android 11 及以上），当前 API=$API"
fi

BIN="$MODPATH/bin/maa-managerd"
if [ ! -f "$BIN" ]; then
  abort "! 模块缺少 bin/maa-managerd，拒绝安装不完整的模块"
fi

# 只在 od 可用时校验 ELF 头；工具缺失不应阻止合法安装。
if command -v od >/dev/null 2>&1; then
  magic="$(head -c 4 "$BIN" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d " \n")"
  case "$magic" in
    7f454c46) ;;
    "") ui_print "! 无法读取 ELF 头，跳过校验" ;;
    *) abort "! bin/maa-managerd 不是 ELF 文件（magic=$magic）" ;;
  esac
fi

apply_perm() {
  # $1 = mode, $2 = path
  if command -v set_perm >/dev/null 2>&1; then
    set_perm "$2" 0 0 "$1"
  else
    chmod "$1" "$2"
  fi
}

apply_perm 0755 "$BIN"
for script in customize.sh service.sh action.sh uninstall.sh; do
  if [ -f "$MODPATH/$script" ]; then
    apply_perm 0755 "$MODPATH/$script"
  fi
done
if [ -f "$MODPATH/module.prop" ]; then
  apply_perm 0644 "$MODPATH/module.prop"
fi
if [ -f "$MODPATH/webroot/index.html" ]; then
  apply_perm 0644 "$MODPATH/webroot/index.html"
fi
if [ -f "$MODPATH/release.json" ]; then
  apply_perm 0644 "$MODPATH/release.json"
  ui_print "- 已包含公开引导配置 release.json"
else
  ui_print "- 无 release.json：启动后进入救援模式"
fi

ui_print "- 校验通过：arm64 / API $API"
