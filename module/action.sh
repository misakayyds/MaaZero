#!/system/bin/sh
# Magisk module action button: open the local management page.
# open-ui 通过受保护的本地控制通道签发一次性浏览器会话，不在磁盘上留凭证文件。

# 与 service.sh 保持一致：后台程序要求平台目录 root 私有。
umask 077

DATA_ROOT=/data/local/maa-platform
MODDIR="${MODDIR:-${0%/*}}"
case "$MODDIR" in
  */*) ;;
  *) MODDIR="$(pwd)" ;;
esac

BIN="$MODDIR/bin/maa-managerd"

if [ ! -x "$BIN" ]; then
  echo "MAA Phone Manager: $BIN 不存在，无法打开管理页面"
  exit 0
fi

nohup "$BIN" open-ui --data-root "$DATA_ROOT" > /dev/null 2>&1 < /dev/null &
echo "MAA Phone Manager: 正在打开管理页面 127.0.0.1:8088"
exit 0
