#!/bin/bash
# Monitor and install the official CN Arknights APK when Hypergryph updates it.

set -uo pipefail

export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG=en_US.UTF-8
export PYTHONDONTWRITEBYTECODE=1

MAA_HOME="${MAA_HOME:-/root/maa-scripts}"
MAA_CONFIG_DIR="${MAA_CONFIG_DIR:-/root/.config/maa}"
MAA_STATE_DIR="${MAA_STATE_DIR:-/root/.local/state/maa}"
MAA_CACHE_DIR="${MAA_CACHE_DIR:-/root/.cache/maa}"
MAA_LOG_DIR="${MAA_LOG_DIR:-/var/log/maa}"
MAA_ADB_ADDR="${MAA_ADB_ADDR:-127.0.0.1:5555}"
export MAA_HOME MAA_CONFIG_DIR MAA_STATE_DIR MAA_CACHE_DIR MAA_LOG_DIR MAA_ADB_ADDR

APK_URL="${ARKNIGHTS_APK_URL:-https://ak.hypergryph.com/downloads/android_lastest}"
PKG="${ARKNIGHTS_PACKAGE:-com.hypergryph.arknights}"
ADB_ADDR="$MAA_ADB_ADDR"
LOG_DIR="$MAA_LOG_DIR"
STATE_DIR="$MAA_STATE_DIR"
CACHE_DIR="$MAA_CACHE_DIR/apk"
LOG_FILE="$LOG_DIR/arknights-apk-update-$(date +%Y%m%d-%H%M%S).log"
LOCK_FILE="/tmp/maa-task.lock"
META_FILE="$STATE_DIR/arknights-apk-head.meta"
APK_FILE="$CACHE_DIR/arknights-latest.apk"
MIN_FREE_KB="${ARKNIGHTS_APK_MIN_FREE_KB:-6291456}"
DOWNLOAD_TIMEOUT="${ARKNIGHTS_APK_DOWNLOAD_TIMEOUT:-60m}"
FORCE=0
DRY_RUN=0
ORIGINAL_ARGS=("$@")

mkdir -p "$LOG_DIR" "$STATE_DIR" "$CACHE_DIR"

log() {
    local level="$1"; shift
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

info() { log INFO "$@"; }
warn() { log WARN "$@"; }
error() { log ERROR "$@"; }

usage() {
    cat <<EOF
Usage: $0 [--check] [--force] [--dry-run]

  --check    Check Hypergryph APK metadata and install only when changed.
  --force    Download and install even if metadata has not changed.
  --dry-run  Only print the decision; do not download or install.
EOF
}

if [[ "${FLOCKER:-}" != "$LOCK_FILE" ]]; then
    env FLOCKER="$LOCK_FILE" flock -n -E 75 -o "$LOCK_FILE" "$0" "${ORIGINAL_ARGS[@]}"
    ec=$?
    if [[ "$ec" -eq 75 ]]; then
        error "已有 MAA 更新或任务正在运行，跳过客户端 APK 更新"
    fi
    exit "$ec"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) ;;
        --force) FORCE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) error "未知参数: $1"; usage; exit 2 ;;
    esac
    shift
done

send_tg_message() {
    local text="$1"
    local tg_file=""
    for candidate in "$MAA_CONFIG_DIR/tg.txt" "$MAA_HOME/tg.txt"; do
        if [[ -s "$candidate" ]]; then
            tg_file="$candidate"
            break
        fi
    done
    if [[ -z "$tg_file" ]]; then
        return 0
    fi

    local token chat_id
    token=$(sed -n '1p' "$tg_file" | tr -d '\r\n')
    chat_id=$(sed -n '2p' "$tg_file" | tr -d '\r\n')
    if [[ -z "$token" || -z "$chat_id" ]]; then
        return 0
    fi

    python3 - "$token" "$chat_id" "$text" <<'PY' >/dev/null 2>&1 || true
import json
import sys
import urllib.request

token, chat_id, text = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/sendMessage",
    data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(req, timeout=20).read()
PY
}

ensure_adb() {
    adb connect "$ADB_ADDR" >/dev/null 2>&1 || true
    if adb -s "$ADB_ADDR" get-state 2>/dev/null | grep -q '^device$'; then
        return 0
    fi
    error "ADB 未连接就绪 ($ADB_ADDR)"
    return 1
}

installed_version() {
    adb -s "$ADB_ADDR" shell dumpsys package "$PKG" 2>/dev/null \
        | grep -E "versionName=|versionCode=" \
        | head -n 4 \
        | tr -d '\r' \
        | sed 's/^[[:space:]]*//'
}

installed_version_digits() {
    adb -s "$ADB_ADDR" shell dumpsys package "$PKG" 2>/dev/null \
        | sed -n 's/.*versionName=\([^[:space:]]*\).*/\1/p' \
        | head -n 1 \
        | tr -cd '0-9'
}

remote_url_digits() {
    local meta_file="$1"
    sed -n -E 's|^effective_url=.*/arknights-[^-]+-([0-9]+)\.apk.*$|\1|p' "$meta_file" | tail -n 1
}

fetch_head_meta() {
    local tmp="$1"
    local headers="$tmp.headers"
    local effective="$tmp.url"

    curl -L -s --connect-timeout 20 --max-time 60 \
        -r 0-0 \
        -w '%{url_effective}\n' \
        -D "$headers" \
        -o /dev/null "$APK_URL" > "$effective"

    {
        printf 'url=%s\n' "$APK_URL"
        printf 'effective_url=%s\n' "$(tail -n 1 "$effective" | tr -d '\r')"
        awk '
            BEGIN { IGNORECASE=1 }
            /^HTTP\// { etag=""; lm=""; cl=""; ct=""; cr="" }
            /^etag:/ { gsub("\r", ""); etag=substr($0, index($0,":")+2) }
            /^last-modified:/ { gsub("\r", ""); lm=substr($0, index($0,":")+2) }
            /^content-length:/ { gsub("\r", ""); cl=substr($0, index($0,":")+2) }
            /^content-type:/ { gsub("\r", ""); ct=substr($0, index($0,":")+2) }
            /^content-range:/ {
                gsub("\r", "")
                cr=substr($0, index($0,":")+2)
                split(cr, parts, "/")
                if (parts[2] != "") cl=parts[2]
            }
            END {
                if (etag != "") print "etag=" etag
                if (lm != "") print "last_modified=" lm
                if (cl != "") print "content_length=" cl
                if (ct != "") print "content_type=" ct
                if (cr != "") print "content_range=" cr
            }
        ' "$headers"
    } | sed '/=$/d' > "$tmp"
    rm -f "$headers" "$effective"

    grep -q '^effective_url=.*\.apk' "$tmp" || {
        error "未解析到最终 APK URL"
        sed 's/^/  /' "$tmp" | tee -a "$LOG_FILE"
        return 1
    }
}

normalize_meta() {
    local meta_file="$1"
    local total
    total=$(sed -n -E 's/^content_range=bytes [^/]+\/([0-9]+)$/\1/p' "$meta_file" | tail -n 1)
    [[ -n "$total" ]] || return 0

    awk -v total="$total" '
        BEGIN { wrote = 0 }
        /^content_length=/ {
            if (!wrote) {
                print "content_length=" total
                wrote = 1
            }
            next
        }
        { print }
        END {
            if (!wrote) {
                print "content_length=" total
            }
        }
    ' "$meta_file" > "$meta_file.normalized"
    mv "$meta_file.normalized" "$meta_file"
}

meta_fingerprint() {
    sed -n -E '/^(effective_url|etag|last_modified|content_range)=/p' "$1" | sort
}

has_remote_changed() {
    local new_meta="$1"
    if [[ "$FORCE" -eq 1 ]]; then
        return 0
    fi
    local inst_digits rem_digits
    inst_digits=$(installed_version_digits || true)
    rem_digits=$(remote_url_digits "$new_meta" || true)
    if [[ -n "$inst_digits" && -n "$rem_digits" && "$inst_digits" == "$rem_digits" ]]; then
        return 1
    fi
    if [[ ! -s "$META_FILE" ]]; then
        warn "本地没有 APK metadata 基线且版本不一致，准备下载并更新"
        return 0
    fi

    local old_fp new_fp
    old_fp=$(meta_fingerprint "$META_FILE")
    new_fp=$(meta_fingerprint "$new_meta")
    [[ "$old_fp" != "$new_fp" ]]
}

check_free_space() {
    local free_kb
    free_kb=$(df -Pk "$CACHE_DIR" | awk 'NR==2 {print $4}')
    if [[ -z "$free_kb" || "$free_kb" -lt "$MIN_FREE_KB" ]]; then
        error "可用空间不足: ${free_kb:-unknown}KB < ${MIN_FREE_KB}KB"
        return 1
    fi
}

download_apk() {
    local part="$APK_FILE.part"
    rm -f "$part"
    info "开始下载官服 APK: $APK_URL"
    timeout "$DOWNLOAD_TIMEOUT" curl -L --fail --retry 3 --retry-delay 10 \
        --connect-timeout 30 \
        -o "$part" "$APK_URL"
    mv "$part" "$APK_FILE"
    info "下载完成: $APK_FILE ($(du -h "$APK_FILE" | awk '{print $1}'))"
}

install_apk() {
    info "停止游戏进程以安装 APK"
    adb -s "$ADB_ADDR" shell am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 2

    info "通过回环 ADB 执行覆盖安装 (adb install -r)"
    local output
    output=$(adb -s "$ADB_ADDR" install -r "$APK_FILE" 2>&1 || true)
    echo "$output" | tee -a "$LOG_FILE"
    if echo "$output" | grep -q "Success"; then
        info "APK 安装成功"
        return 0
    fi

    warn "adb install -r 未直接成功，回退到临时推送后 pm install -r"
    local remote_tmp="/data/local/tmp/maa-arknights-update-$$.apk"
    if adb -s "$ADB_ADDR" push "$APK_FILE" "$remote_tmp" >>"$LOG_FILE" 2>&1; then
        output=$(adb -s "$ADB_ADDR" shell pm install -r "$remote_tmp" 2>&1 || true)
        adb -s "$ADB_ADDR" shell rm -f "$remote_tmp" >/dev/null 2>&1 || true
        echo "$output" | tee -a "$LOG_FILE"
        if echo "$output" | grep -q "Success"; then
            info "APK 回退安装成功"
            return 0
        fi
    else
        adb -s "$ADB_ADDR" shell rm -f "$remote_tmp" >/dev/null 2>&1 || true
    fi

    error "APK 安装失败"
    return 1
}

main() {
    ensure_adb || return 1

    info "======================================="
    info "明日方舟官服 APK 更新检查"
    info "URL: $APK_URL"
    info "包名: $PKG"
    info "已安装: $(installed_version | tr '\n' ' ' || echo unknown)"
    info "日志: $LOG_FILE"
    info "======================================="

    local tmp_meta="/tmp/arknights-apk-meta.$$"
    trap 'rm -f "/tmp/arknights-apk-meta.$$"' EXIT
    if ! fetch_head_meta "$tmp_meta"; then
        error "获取远端 APK metadata 失败"
        send_tg_message "[MAA] 明日方舟 APK 更新检查失败：无法获取官服下载信息。日志: $LOG_FILE"
        return 1
    fi
    normalize_meta "$tmp_meta"

    info "远端 metadata:"
    sed 's/^/  /' "$tmp_meta" | tee -a "$LOG_FILE"

    if ! has_remote_changed "$tmp_meta"; then
        cp "$tmp_meta" "$META_FILE"
        info "当前客户端版本已是最新，无需下载"
        return 0
    fi

    warn "检测到客户端存在新版本，需要下载并安装"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "dry-run 模式，不执行下载/安装"
        return 0
    fi

    check_free_space || return 1
    download_apk || {
        send_tg_message "[MAA] 明日方舟 APK 下载失败。日志: $LOG_FILE"
        return 1
    }

    if install_apk; then
        cp "$tmp_meta" "$META_FILE"
        rm -f "$APK_FILE"
        local version
        version=$(installed_version | tr '\n' ' ' || echo unknown)
        send_tg_message "[MAA] 明日方舟客户端 APK 已更新并安装成功。$version"
        info "安装后版本: $version"
        return 0
    fi

    send_tg_message "[MAA] 明日方舟 APK 下载完成但安装失败。日志: $LOG_FILE"
    return 1
}

main "$@"
