#!/system/bin/sh
# MAA Phone Manager (preview) uninstall hook.
# 只停止后台程序：先调用二进制 stop，再保留 /data/local/maa-platform 下的全部用户数据。
# 不使用按进程名匹配的强杀命令，也不删除任何文件。

DATA_ROOT="${DATA_ROOT:-/data/local/maa-platform}"
MODDIR="${MODDIR:-${0%/*}}"
case "$MODDIR" in
  */*) ;;
  *) MODDIR="$(pwd)" ;;
esac

BIN="$MODDIR/bin/maa-managerd"
PIDFILE="$DATA_ROOT/managerd.pid"

if [ -x "$BIN" ]; then
  "$BIN" stop --data-root "$DATA_ROOT" 2>&1
fi

# 兜底：仅当 pidfile 里的 pid 仍存在，且 /proc/<pid>/cmdline 的 argv[0] 精确等于
# 本模块二进制、argv[1] 是 serve 时才结束它；绝不做子串匹配。
pid="$(cat "$PIDFILE" 2>/dev/null)"
case "$pid" in
  ""|*[!0-9]*) pid="" ;;
esac
if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
  argv="$(tr "\0" "\n" < "/proc/$pid/cmdline" 2>/dev/null)"
  argv0="$(printf "%s\n" "$argv" | head -n 1)"
  argv1="$(printf "%s\n" "$argv" | head -n 2 | tail -n 1)"
  if [ "$argv0" = "$BIN" ] && [ "$argv1" = "serve" ]; then
    kill "$pid" 2>/dev/null
    echo "MAA Phone Manager: stopped daemon pid=$pid"
  fi
fi

echo "MAA Phone Manager: user data kept at $DATA_ROOT"
exit 0
