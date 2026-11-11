#!/system/bin/sh
# MAA Phone Manager (preview) late_start service entry.
# 只做一件事：以后台方式拉起 maa-managerd，不阻塞开机。
# release.json 缺失时由后台程序自行进入救援模式；本脚本不下载任何东西。

# 新建目录必须是 root 私有：默认 umask 022 下 mkdir 会产生 0755，后台程序会拒绝。
# 已在任何 mkdir 之前收紧到 077。注意：本脚本不修改现存目录的权限——
# 现存目录权限不安全时由后台程序 fail closed 拒绝，模块不替它做决定。
umask 077

DATA_ROOT=/data/local/maa-platform
MODDIR="${MODDIR:-${0%/*}}"
case "$MODDIR" in
  */*) ;;
  *) MODDIR="$(pwd)" ;;
esac

BIN="$MODDIR/bin/maa-managerd"
LOG_DIR="$DATA_ROOT/logs"
LOG="$LOG_DIR/managerd.log"
PIDFILE="$DATA_ROOT/managerd.pid"
ASSETS="$MODDIR/webroot/assets"
CONFIG="$MODDIR/release.json"
LISTEN=127.0.0.1:8088

# 符号链接会绕过上面的路径假设，直接拒绝启动，并且不向可疑路径写任何东西。
fail_closed() {
  printf "%s\n" "maa-phone-manager service: $*" > /dev/kmsg 2>/dev/null
  exit 0
}

if [ -L "$DATA_ROOT" ]; then
  fail_closed "refusing to start: $DATA_ROOT is a symlink"
fi
if [ -L "$LOG_DIR" ]; then
  fail_closed "refusing to start: $LOG_DIR is a symlink"
fi
if [ -L "$LOG" ]; then
  fail_closed "refusing to start: $LOG is a symlink"
fi
if [ -L "$PIDFILE" ]; then
  fail_closed "refusing to start: $PIDFILE is a symlink"
fi

mkdir -p "$LOG_DIR" 2>/dev/null

# 写任何日志/PID 之前再确认目录本身是 root:700：现存 0755/0777 目录可能被读取，
# 也可能被植入符号链接。这里只校验，不改权限——不改现存目录是 main 的判断，
# 不符就 fail closed，绝不向不安全目录写文件。
require_root_700() {
  target="$1"
  info="$(stat -c "%u:%a" "$target" 2>/dev/null)"
  case "$info" in
    0:700|0:0700) return 0 ;;
  esac
  fail_closed "$target must be root-owned with mode 700 (got ${info:-unreadable}); refusing to write logs or pid"
}

require_root_700 "$DATA_ROOT"
require_root_700 "$LOG_DIR"

log_line() {
  if [ -d "$LOG_DIR" ]; then
    echo "$(date "+%Y-%m-%d %H:%M:%S") maa-managerd service: $*" >> "$LOG"
  fi
}

# 用 argv[0] 与 argv[1] 精确比较（不是子串匹配）：既要是本模块的二进制，也要是
# serve 子命令。action.sh 的 open-ui 进程、.bak-* 备份脚本或同名字符串都不会
# 被误判成正在运行的常驻进程。
daemon_alive() {
  target_pid="$1"
  case "$target_pid" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ -r "/proc/$target_pid/cmdline" ] || return 1
  argv="$(tr "\0" "\n" < "/proc/$target_pid/cmdline" 2>/dev/null)"
  argv0="$(printf "%s\n" "$argv" | head -n 1)"
  argv1="$(printf "%s\n" "$argv" | head -n 2 | tail -n 1)"
  [ "$argv0" = "$BIN" ] || return 1
  [ "$argv1" = "serve" ] || return 1
  return 0
}

running_pid() {
  pid="$(cat "$PIDFILE" 2>/dev/null)"
  if daemon_alive "$pid"; then
    printf "%s" "$pid"
    return 0
  fi
  return 1
}

# 就绪证据必须是「本进程自己持有的、监听 127.0.0.1:8088 的 socket inode」。
# 只看全局端口被占用不算数：设备上可能已有别的服务（例如既有生产 WebUI）
# 占着 8088，那与本次启动无关。/proc/net/tcp 中 127.0.0.1:8088 = 0100007F:1F90，
# 状态 0A 为 LISTEN，行尾字段是该 socket 的 inode。
listening_socket_owned_by() {
  target_pid="$1"
  for fd_path in /proc/$target_pid/fd/*; do
    link="$(readlink "$fd_path" 2>/dev/null)" || continue
    case "$link" in
      socket:\[*\]) ;;
      *) continue ;;
    esac
    inode="${link#socket:[}"
    inode="${inode%]}"
    [ -n "$inode" ] || continue
    # 固定字段匹配：$2=本地地址、$4=状态、$10=inode。这样 inode 123 不会匹配 1234。
    if awk -v want="$inode" '$2 == "0100007F:1F90" && $4 == "0A" && $10 "" == want { found = 1 } END { exit(found ? 0 : 1) }' /proc/net/tcp 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

# 有上限的等待（最多约 4 秒），不会长时间阻塞开机。
wait_ready() {
  target_pid="$1"
  ticks=0
  while [ "$ticks" -lt 16 ]; do
    if ! daemon_alive "$target_pid"; then
      return 1
    fi
    if listening_socket_owned_by "$target_pid"; then
      return 0
    fi
    sleep 0.25 2>/dev/null || sleep 1
    ticks=$((ticks + 1))
  done
  return 1
}

if [ ! -x "$BIN" ]; then
  log_line "missing $BIN; module is incomplete, skipping start"
  exit 0
fi

if pid="$(running_pid)"; then
  log_line "already running pid=$pid"
  exit 0
fi

# stdout 可能带 open-ui 的一次性会话 URL，不能进日志，也不能留在终端；
# 只把 stderr（诊断）追加到 root 私有日志。
nohup "$BIN" serve --data-root "$DATA_ROOT" --listen "$LISTEN" --config "$CONFIG" --assets "$ASSETS" > /dev/null 2>> "$LOG" < /dev/null &
pid=$!
echo "$pid" > "$PIDFILE"

# 只有真正就绪才写 started；否则写失败行，避免虚报已启动。
if wait_ready "$pid"; then
  log_line "started pid=$pid listening on $LISTEN"
else
  log_line "failed to start: pid=$pid is not listening on $LISTEN yet"
fi
exit 0
