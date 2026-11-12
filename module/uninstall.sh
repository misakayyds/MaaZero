#!/system/bin/sh
# MAA Phone Manager (preview) / MaaZero uninstall hook.
# 只停止后台程序：先调用二进制 stop，再保留 /data/local/maa-platform 下的全部用户数据。
# 不使用按进程名匹配的强杀命令，也不清理任何文件。

DATA_ROOT="${DATA_ROOT:-/data/local/maa-platform}"
MODDIR="${MODDIR:-${0%/*}}"
case "$MODDIR" in
  */*) ;;
  *) MODDIR="$(pwd)" ;;
esac

BRAND="MAA Phone Manager"
if [ -f "$MODDIR/README.md" ] && [ ! -f "$MODDIR/README-preview.md" ]; then
  BRAND="$(sed -n 's/^name=//p' "$MODDIR/module.prop" 2>/dev/null | head -n 1)"
  [ -n "$BRAND" ] || BRAND="MaaZero"
fi

BIN="$MODDIR/bin/maa-managerd"
PIDFILE="$DATA_ROOT/managerd.pid"

daemon_matches_pid() {
  pid="$1"
  case "$pid" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ -r "/proc/$pid/cmdline" ] || return 1
  argv="$(tr "\0" "\n" < "/proc/$pid/cmdline" 2>/dev/null)"
  argv0="$(printf "%s\n" "$argv" | head -n 1)"
  argv1="$(printf "%s\n" "$argv" | head -n 2 | tail -n 1)"
  [ "$argv0" = "$BIN" ] && [ "$argv1" = "serve" ]
}

if [ -x "$BIN" ]; then
  "$BIN" stop --data-root "$DATA_ROOT" 2>&1
fi

# 先等待优雅停止与挂载栈卸载完成；仅当超时后 pidfile 里的 pid 仍存在，且
# /proc/$pid/cmdline 的 argv[0] 精确等于本模块二进制、argv[1] 是 serve 时才兜底结束它。
pid="$(cat "$PIDFILE" 2>/dev/null)"
case "$pid" in
  ""|*[!0-9]*) pid="" ;;
esac
if [ -n "$pid" ]; then
  ticks=0
  while [ "$ticks" -lt 40 ] && daemon_matches_pid "$pid"; do
    if sleep 0.25 2>/dev/null; then
      ticks=$((ticks + 1))
    else
      sleep 1
      ticks=$((ticks + 4))
    fi
  done
  if daemon_matches_pid "$pid"; then
    kill "$pid" 2>/dev/null
    ticks=0
    while [ "$ticks" -lt 20 ] && daemon_matches_pid "$pid"; do
      if sleep 0.25 2>/dev/null; then
        ticks=$((ticks + 1))
      else
        sleep 1
        ticks=$((ticks + 4))
      fi
    done
    echo "$BRAND: stopped daemon pid=$pid"
  fi
fi

echo "$BRAND: user data kept at $DATA_ROOT"
exit 0
