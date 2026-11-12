#!/bin/bash
# Monitor and install Arknights client updates across Official, Bilibili, and YoStar (EN/JP/KR).

set -euo pipefail

export PATH="${HOME:-/root}/.local/bin:${MAA_HOME:-/root/maa-scripts}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG="${LANG:-en_US.UTF-8}"
export PYTHONDONTWRITEBYTECODE=1

MAA_HOME="${MAA_HOME:-/root/maa-scripts}"
MAA_CONFIG_DIR="${MAA_CONFIG_DIR:-/root/.config/maa}"
MAA_STATE_DIR="${MAA_STATE_DIR:-/root/.local/state/maa}"
MAA_CACHE_DIR="${MAA_CACHE_DIR:-/root/.cache/maa}"
MAA_LOG_DIR="${MAA_LOG_DIR:-/var/log/maa}"
MAA_ADB_ADDR="${MAA_ADB_ADDR:-127.0.0.1:5555}"
export MAA_HOME MAA_CONFIG_DIR MAA_STATE_DIR MAA_CACHE_DIR MAA_LOG_DIR MAA_ADB_ADDR

SCHEDULE_CONTROL_SCRIPT="${MAA_SCHEDULE_CONTROL_SCRIPT:-$MAA_HOME/maa_schedule_control.py}"
MAA_RT_CLIENT_TYPE="Official"
MAA_RT_AK_PACKAGE="com.hypergryph.arknights"
MAA_RT_HTTP_PROXY=""
MAA_RT_CUSTOM_PACKAGE_URL=""
if [[ -f "$SCHEDULE_CONTROL_SCRIPT" ]]; then
    if _rt_env=$(python3 "$SCHEDULE_CONTROL_SCRIPT" env --config-dir "$MAA_CONFIG_DIR" 2>/dev/null | tr -d '\r'); then
        eval "$_rt_env"
    fi
fi
if [[ -n "${MAA_RT_HTTP_PROXY:-}" ]]; then
    export HTTP_PROXY="$MAA_RT_HTTP_PROXY" HTTPS_PROXY="$MAA_RT_HTTP_PROXY" ALL_PROXY="$MAA_RT_HTTP_PROXY"
    export http_proxy="$MAA_RT_HTTP_PROXY" https_proxy="$MAA_RT_HTTP_PROXY" all_proxy="$MAA_RT_HTTP_PROXY"
    export NO_PROXY="127.0.0.1,localhost,::1" no_proxy="127.0.0.1,localhost,::1"
fi

OFFICIAL_URL_OVERRIDE="${ARKNIGHTS_APK_URL:-}"
EXPLICIT_PKG_OVERRIDE="${ARKNIGHTS_PACKAGE:-}"
CLIENT_OVERRIDE=""
PACKAGE_URL_OVERRIDE="${ARKNIGHTS_PACKAGE_URL:-}"
PACKAGE_FILE_OVERRIDE="${ARKNIGHTS_PACKAGE_FILE:-}"
ADB_ADDR="$MAA_ADB_ADDR"
LOG_DIR="$MAA_LOG_DIR"
STATE_DIR="$MAA_STATE_DIR"
CACHE_DIR="$MAA_CACHE_DIR/apk"
LOG_FILE="$LOG_DIR/arknights-apk-update-$(date +%Y%m%d-%H%M%S).log"
LOCK_FILE="${MAA_TASK_LOCK_FILE:-/tmp/maa-task.lock}"
MIN_FREE_KB="${ARKNIGHTS_APK_MIN_FREE_KB:-6291456}"
DOWNLOAD_TIMEOUT="${ARKNIGHTS_APK_DOWNLOAD_TIMEOUT:-60m}"
FORCE=0
FORCE_OFFICIAL=0
DRY_RUN=0
OPEN_STORE=0
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
Usage: $0 [--check] [--client <Official|Bilibili|YoStarEN|YoStarJP|YoStarKR>]
          [--force] [--force-official] [--package-url <https://...>]
          [--package-file <path.(apk|xapk|apkm|apks)>] [--open-store] [--dry-run]

  --check                 Check official version/CDN metadata and update when newer.
  --client <type>         Target client channel (defaults to runtime-settings client_type).
  --force                 Download/install or re-notify even if already up to date.
  --force-official        Force Official CN client check/install regardless of global setting.
  --package-url <url>     Custom HTTPS .apk/.xapk/.apkm URL to download and install.
  --package-file <path>   Install directly from a local .apk/.xapk/.apkm/.apks file.
  --open-store            Open store page on device via ADB when manual store update is needed.
  --dry-run               Only print version check decision; do not download or install.
EOF
}

validate_client_type() {
    case "$1" in
        Official|Bilibili|YoStarEN|YoStarJP|YoStarKR) return 0 ;;
        *) return 1 ;;
    esac
}

package_for_client() {
    case "$1" in
        Official) echo "com.hypergryph.arknights" ;;
        Bilibili) echo "com.hypergryph.arknights.bilibili" ;;
        YoStarEN) echo "com.YoStarEN.Arknights" ;;
        YoStarJP) echo "com.YoStarJP.Arknights" ;;
        YoStarKR) echo "com.YoStarKR.Arknights" ;;
        *) echo "com.hypergryph.arknights" ;;
    esac
}

validate_package_name() {
    case "$1" in
        com.hypergryph.arknights|com.hypergryph.arknights.bilibili|com.YoStarEN.Arknights|com.YoStarJP.Arknights|com.YoStarKR.Arknights)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) ;;
        --force) FORCE=1 ;;
        --force-official) FORCE_OFFICIAL=1; CLIENT_OVERRIDE="Official" ;;
        --client)
            [[ $# -ge 2 ]] || { error "--client 缺少参数"; exit 2; }
            CLIENT_OVERRIDE="$2"
            shift
            ;;
        --package-url)
            [[ $# -ge 2 ]] || { error "--package-url 缺少参数"; exit 2; }
            PACKAGE_URL_OVERRIDE="$2"
            shift
            ;;
        --package-file)
            [[ $# -ge 2 ]] || { error "--package-file 缺少参数"; exit 2; }
            PACKAGE_FILE_OVERRIDE="$2"
            shift
            ;;
        --open-store) OPEN_STORE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) error "未知参数: $1"; usage; exit 2 ;;
    esac
    shift
done

ACTIVE_CLIENT="${CLIENT_OVERRIDE:-${MAA_RT_CLIENT_TYPE:-Official}}"
if ! validate_client_type "$ACTIVE_CLIENT"; then
    error "不支持的客户端类型: $ACTIVE_CLIENT"
    exit 2
fi

if [[ -n "$PACKAGE_FILE_OVERRIDE" ]]; then
    if [[ -L "$PACKAGE_FILE_OVERRIDE" || ! -f "$PACKAGE_FILE_OVERRIDE" ]]; then
        error "指定的本地安装包不存在或非常规文件: $PACKAGE_FILE_OVERRIDE"
        exit 2
    fi
fi

PKG="${EXPLICIT_PKG_OVERRIDE:-$(package_for_client "$ACTIVE_CLIENT")}"
if ! validate_package_name "$PKG"; then
    error "不允许的客户端包名: $PKG"
    exit 2
fi

if [[ "$ACTIVE_CLIENT" == "Official" ]]; then
    META_FILE="$STATE_DIR/arknights-apk-head.meta"
else
    META_FILE="$STATE_DIR/arknights-apk-head-${ACTIVE_CLIENT}.meta"
fi
ARCHIVE_FILE="$CACHE_DIR/arknights-${ACTIVE_CLIENT}-latest.pkg"

if [[ "${FLOCKER:-}" != "$LOCK_FILE" ]]; then
    ec=0
    env FLOCKER="$LOCK_FILE" flock -n -E 75 -o "$LOCK_FILE" "$0" ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"} || ec=$?
    if [[ "$ec" -eq 75 ]]; then
        error "已有 MAA 更新或任务正在运行，跳过客户端检查与更新"
    fi
    exit "$ec"
fi

TMP_META=""
ACTIVE_UNPACK_DIR=""
ACTIVE_REMOTE_TMP=""
ACTIVE_PM_SID=""

cleanup_artifacts() {
    if [[ -n "$TMP_META" ]]; then
        rm -f "$TMP_META" "$TMP_META.headers" "$TMP_META.url" "$TMP_META.normalized" || true
    fi
    if [[ -n "$ACTIVE_UNPACK_DIR" ]]; then
        rm -rf "$ACTIVE_UNPACK_DIR" || true
    fi
    rm -f "$ARCHIVE_FILE.part" || true
    if [[ -n "$ACTIVE_PM_SID" ]]; then
        timeout --foreground 10s adb -s "$ADB_ADDR" shell pm install-abandon "$ACTIVE_PM_SID" >/dev/null 2>&1 || true
        ACTIVE_PM_SID=""
    fi
    if [[ -n "$ACTIVE_REMOTE_TMP" ]]; then
        timeout --foreground 10s adb -s "$ADB_ADDR" shell rm -rf "$ACTIVE_REMOTE_TMP" >/dev/null 2>&1 || true
        ACTIVE_REMOTE_TMP=""
    fi
}
trap cleanup_artifacts EXIT INT TERM

sh_single_quote() {
    local s="$1"
    printf "'%s'" "${s//\'/\'\\\'\'}"
}

curl_cfg_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

curl_proto_rule() {
    if [[ "${MAA_ALLOW_HTTP_VERSION_API:-}" == "1" ]]; then
        printf '%s' '=http,https'
    else
        printf '%s' '=https'
    fi
}

sanitize_url_for_log() {
    local raw="${1:-}"
    printf '%s' "$raw" | sed -E 's|^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]+@|\1|; s|[?#].*$||'
}

build_curl_config() {
    local target_url="$1"
    local referer="${2:-}"
    printf 'url = "%s"\n' "$(curl_cfg_escape "$target_url")"
    if [[ -n "$referer" ]]; then
        printf 'referer = "%s"\n' "$(curl_cfg_escape "$referer")"
    fi
    if [[ "${MAA_ALLOW_HTTP_VERSION_API:-}" != "1" ]]; then
        printf 'noproxy = ""\n'
    fi
}

save_meta_file() {
    local src="$1"
    local dst="$2"
    (
        umask 077
        cp "$src" "$dst.tmp.$$"
        mv "$dst.tmp.$$" "$dst"
    )
}

send_notify_message() {
    local text="$1"
    if [[ -f "$SCHEDULE_CONTROL_SCRIPT" ]]; then
        python3 "$SCHEDULE_CONTROL_SCRIPT" notify \
            --config-dir "$MAA_CONFIG_DIR" \
            --script-dir "$MAA_HOME" \
            --title "[MaaZero] 客户端更新通知" \
            --message "$text" >/dev/null 2>&1 || true
        return 0
    fi
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
    timeout --foreground 12s adb connect "$ADB_ADDR" >/dev/null 2>&1 || true
    if timeout --foreground 8s adb -s "$ADB_ADDR" get-state 2>/dev/null | grep -q '^device$'; then
        return 0
    fi
    error "ADB 未连接就绪 ($ADB_ADDR)"
    return 1
}

read_package_dumpsys() {
    timeout --foreground 15s adb -s "$ADB_ADDR" shell dumpsys package "$PKG" 2>/dev/null | tr -d '\r' || true
}

format_dumpsys_summary() {
    awk '/versionName=|versionCode=/ { if (++n <= 4) { sub(/^[[:space:]]+/, ""); printf "%s ", $0 } }'
}

installed_version_summary() {
    local ds
    ds=$(read_package_dumpsys)
    printf '%s\n' "$ds" | format_dumpsys_summary
}

installed_version_digits() {
    local ver_name="${1:-}"
    printf '%s' "$ver_name" | tr -cd '0-9'
}

remote_url_digits() {
    local meta_file="$1"
    sed -n -E 's|^effective_url=.*/arknights-[^-]+-([0-9]+)\.apk.*$|\1|p' "$meta_file" \
        | awk '{ last = $0 } END { if (last != "") print last }'
}

fetch_head_meta() {
    local target_url="$1"
    local referer="$2"
    local tmp="$3"
    local headers="$tmp.headers"
    local effective="$tmp.url"
    local proto_rule
    proto_rule=$(curl_proto_rule)

    rm -f "$headers" "$effective" "$tmp"
    if ! (
        umask 077
        build_curl_config "$target_url" "$referer" \
            | timeout --foreground 75s curl -L -sS --fail \
                --proto "$proto_rule" --proto-redir "$proto_rule" --max-redirs 5 \
                --connect-timeout 20 --max-time 60 -r 0-0 \
                -w '%{url_effective}\n' -D "$headers" -o /dev/null --config - > "$effective"
    ); then
        rm -f "$headers" "$effective"
        return 1
    fi

    local raw_eff safe_url safe_eff
    raw_eff=$(awk '{ last = $0 } END { gsub("\r", "", last); print last }' "$effective")
    safe_url=$(sanitize_url_for_log "$target_url")
    safe_eff=$(sanitize_url_for_log "$raw_eff")

    (
        umask 077
        {
            printf 'url=%s\n' "$safe_url"
            printf 'effective_url=%s\n' "$safe_eff"
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
    )
    rm -f "$headers" "$effective"

    if ! grep -qE '^effective_url=https?://' "$tmp"; then
        error "未解析到有效的远端安装包响应头"
        return 1
    fi
}

normalize_meta() {
    local meta_file="$1"
    local total
    total=$(sed -n -E 's/^content_range=bytes [^/]+\/([0-9]+)$/\1/p' "$meta_file" \
        | awk '{ last = $0 } END { if (last != "") print last }')
    if [[ -z "$total" ]]; then
        return 0
    fi

    (
        umask 077
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
    )
}

meta_fingerprint() {
    sed -n -E '/^(effective_url|etag|last_modified|content_range)=/p' "$1" | sort
}

has_official_cdn_changed() {
    local new_meta="$1"
    local inst_ver_name="$2"
    if [[ "$FORCE" -eq 1 ]]; then
        return 0
    fi
    if ! grep -q '^effective_url=.*\.apk' "$new_meta"; then
        error "未解析到最终官服 APK URL"
        return 1
    fi
    local inst_digits rem_digits
    inst_digits=$(installed_version_digits "$inst_ver_name")
    rem_digits=$(remote_url_digits "$new_meta" || true)
    if [[ -n "$inst_digits" && -n "$rem_digits" && "$inst_digits" == "$rem_digits" ]]; then
        return 1
    fi
    if [[ -n "$inst_digits" && -n "$rem_digits" && "$inst_digits" != "$rem_digits" ]]; then
        return 0
    fi
    if [[ ! -s "$META_FILE" ]]; then
        warn "本地没有 APK metadata 基线，准备下载并更新"
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

download_package() {
    local target_url="$1"
    local referer="$2"
    local display_url="${3:-$(sanitize_url_for_log "$target_url")}"
    local part="$ARCHIVE_FILE.part"
    local proto_rule
    proto_rule=$(curl_proto_rule)

    rm -f "$part"
    info "开始下载客户端安装包 ($ACTIVE_CLIENT): $display_url"
    if ! (
        umask 077
        build_curl_config "$target_url" "$referer" \
            | timeout --foreground "$DOWNLOAD_TIMEOUT" curl -L --fail \
                --proto "$proto_rule" --proto-redir "$proto_rule" --max-redirs 5 \
                --retry 3 --retry-delay 10 --connect-timeout 30 \
                -o "$part" --config -
    ); then
        rm -f "$part"
        return 1
    fi
    mv "$part" "$ARCHIVE_FILE"
    info "下载完成: $ARCHIVE_FILE ($(du -h "$ARCHIVE_FILE" | awk '{print $1}'))"
}

install_single_apk_file() {
    local apk_path="$1"
    info "通过回环 ADB 执行单包覆盖安装 (adb install -r)"
    local output
    output=$(timeout --foreground 15m adb -s "$ADB_ADDR" install -r "$apk_path" 2>&1 || true)
    echo "$output" | tee -a "$LOG_FILE"
    if echo "$output" | grep -q "Success"; then
        info "单包 APK 安装成功"
        return 0
    fi

    warn "adb install -r 未直接成功，回退到临时推送后 pm install -r"
    local apk_bytes
    apk_bytes=$(wc -c < "$apk_path" | tr -d '[:space:]')
    local remote_tmp="/data/local/tmp/maa-arknights-update-$$.apk"
    ACTIVE_REMOTE_TMP="$remote_tmp"
    if timeout --foreground 10m adb -s "$ADB_ADDR" push "$apk_path" "$remote_tmp" >>"$LOG_FILE" 2>&1; then
        if [[ "$apk_bytes" =~ ^[1-9][0-9]*$ ]]; then
            output=$(timeout --foreground 15m adb -s "$ADB_ADDR" shell "pm install -r -S $apk_bytes - < '$remote_tmp'" 2>&1 || true)
        else
            output=$(timeout --foreground 15m adb -s "$ADB_ADDR" shell "pm install -r '$remote_tmp'" 2>&1 || true)
        fi
        if ! echo "$output" | grep -q "Success"; then
            output=$(timeout --foreground 15m adb -s "$ADB_ADDR" shell "pm install -r '$remote_tmp'" 2>&1 || true)
        fi
        timeout --foreground 10s adb -s "$ADB_ADDR" shell rm -f "$remote_tmp" >/dev/null 2>&1 || true
        ACTIVE_REMOTE_TMP=""
        echo "$output" | tee -a "$LOG_FILE"
        if echo "$output" | grep -q "Success"; then
            info "APK 回退安装成功"
            return 0
        fi
    else
        timeout --foreground 10s adb -s "$ADB_ADDR" shell rm -f "$remote_tmp" >/dev/null 2>&1 || true
        ACTIVE_REMOTE_TMP=""
    fi
    return 1
}

install_split_apk_files() {
    local split_apks=("$@")
    info "检测到 Split APKs 分包归档 (${#split_apks[@]} 个分包)，执行 adb install-multiple -r"
    local output
    output=$(timeout --foreground 15m adb -s "$ADB_ADDR" install-multiple -r "${split_apks[@]}" 2>&1 || true)
    echo "$output" | tee -a "$LOG_FILE"
    if echo "$output" | grep -q "Success"; then
        info "Split APKs 分包安装成功"
        return 0
    fi

    warn "adb install-multiple 未直接成功，回退到 pm install-create 会话安装"
    local create_out sid
    create_out=$(timeout --foreground 30s adb -s "$ADB_ADDR" shell pm install-create -r 2>&1 || true)
    sid=$(printf '%s' "$create_out" | sed -n 's/.*\[\([0-9][0-9]*\)\].*/\1/p' | awk '{ if (NR==1) print $0 }')
    if [[ ! "$sid" =~ ^[0-9]+$ ]]; then
        error "无法创建 pm install 会话: $create_out"
        return 1
    fi
    ACTIVE_PM_SID="$sid"

    local remote_dir="/data/local/tmp/maa-splits-$$"
    ACTIVE_REMOTE_TMP="$remote_dir"
    timeout --foreground 10s adb -s "$ADB_ADDR" shell mkdir -p "$remote_dir" >/dev/null 2>&1 || true
    local idx=0 ok_all=1
    for split_file in "${split_apks[@]}"; do
        local split_bytes
        split_bytes=$(wc -c < "$split_file" | tr -d '[:space:]')
        if [[ ! "$split_bytes" =~ ^[1-9][0-9]*$ ]]; then
            error "分包文件大小无效: $split_file ($split_bytes)"
            ok_all=0
            break
        fi
        local remote_split="$remote_dir/split_${idx}.apk"
        if ! timeout --foreground 10m adb -s "$ADB_ADDR" push "$split_file" "$remote_split" >>"$LOG_FILE" 2>&1; then
            ok_all=0
            break
        fi
        local write_out
        write_out=$(timeout --foreground 5m adb -s "$ADB_ADDR" shell "pm install-write -S $split_bytes $sid split_${idx}.apk - < '$remote_split'" 2>&1 || true)
        timeout --foreground 10s adb -s "$ADB_ADDR" shell rm -f "$remote_split" >/dev/null 2>&1 || true
        if ! echo "$write_out" | grep -q "Success"; then
            echo "$write_out" | tee -a "$LOG_FILE"
            ok_all=0
            break
        fi
        idx=$((idx + 1))
    done
    timeout --foreground 10s adb -s "$ADB_ADDR" shell rm -rf "$remote_dir" >/dev/null 2>&1 || true
    ACTIVE_REMOTE_TMP=""

    if [[ "$ok_all" -eq 1 ]]; then
        output=$(timeout --foreground 10m adb -s "$ADB_ADDR" shell pm install-commit "$sid" 2>&1 || true)
        ACTIVE_PM_SID=""
        echo "$output" | tee -a "$LOG_FILE"
        if echo "$output" | grep -q "Success"; then
            info "Split APKs 会话回退安装成功"
            return 0
        fi
    fi
    if [[ -n "$ACTIVE_PM_SID" ]]; then
        timeout --foreground 15s adb -s "$ADB_ADDR" shell pm install-abandon "$ACTIVE_PM_SID" >/dev/null 2>&1 || true
        ACTIVE_PM_SID=""
    fi
    return 1
}

install_package_artifact() {
    local archive_path="$1"
    local unpack_dir="$CACHE_DIR/unpack-$$"
    ACTIVE_UNPACK_DIR="$unpack_dir"
    rm -rf "$unpack_dir"

    local manifest_json
    if ! manifest_json=$(python3 "$SCHEDULE_CONTROL_SCRIPT" unpack-apk \
        --archive "$archive_path" \
        --extract-dir "$unpack_dir" \
        --expected-package "$PKG" 2>>"$LOG_FILE" | tr -d '\r'); then
        error "安装包完整性或包名校验未通过"
        rm -rf "$unpack_dir"
        ACTIVE_UNPACK_DIR=""
        return 1
    fi

    local kind=""
    local apk_list=()
    local obb_pairs=()
    while IFS= read -r -d '' record; do
        case "$record" in
            KIND:*) kind="${record#KIND:}" ;;
            APK:*) apk_list+=("${record#APK:}") ;;
            OBB:*) obb_pairs+=("${record#OBB:}") ;;
        esac
    done < <(python3 - "$manifest_json" <<'PY'
import json
import sys

obj = json.loads(sys.argv[1])
sys.stdout.write(f"KIND:{obj['kind']}\0")
for p in obj.get("apks", []):
    sys.stdout.write(f"APK:{p}\0")
for obb in obj.get("obbs", []):
    sys.stdout.write(f"OBB:{obb['local']}\t{obb['filename']}\0")
PY
    )

    if [[ "${#apk_list[@]}" -eq 0 ]]; then
        error "未解析出可安装的 APK 条目"
        rm -rf "$unpack_dir"
        ACTIVE_UNPACK_DIR=""
        return 1
    fi

    info "停止游戏进程以准备覆盖安装 ($PKG)"
    timeout --foreground 10s adb -s "$ADB_ADDR" shell am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 2

    local install_rc=1
    if [[ "$kind" == "single_apk" ]]; then
        if install_single_apk_file "${apk_list[0]}"; then
            install_rc=0
        fi
    else
        if install_split_apk_files "${apk_list[@]}"; then
            install_rc=0
        fi
    fi

    if [[ "$install_rc" -eq 0 && "${#obb_pairs[@]}" -gt 0 ]]; then
        info "正在同步 OBB 扩展资源包 (${#obb_pairs[@]} 个)..."
        timeout --foreground 15s adb -s "$ADB_ADDR" shell mkdir -p "/sdcard/Android/obb/$PKG" >/dev/null 2>&1 || true
        for pair in "${obb_pairs[@]}"; do
            local obb_local="${pair%%$'\t'*}"
            local obb_name="${pair#*$'\t'}"
            if ! timeout --foreground 10m adb -s "$ADB_ADDR" push "$obb_local" "/sdcard/Android/obb/$PKG/$obb_name" >>"$LOG_FILE" 2>&1; then
                error "推送 OBB 资源失败: $obb_name"
                rm -rf "$unpack_dir"
                ACTIVE_UNPACK_DIR=""
                return 1
            fi
        done
        info "OBB 扩展资源同步完成"
    fi

    rm -rf "$unpack_dir"
    ACTIVE_UNPACK_DIR=""
    if [[ "$install_rc" -ne 0 ]]; then
        error "客户端安装包安装失败"
        return 1
    fi
    return 0
}

open_client_store() {
    local intent_uri="$1"
    local web_url="$2"
    local q_intent q_web
    q_intent=$(sh_single_quote "$intent_uri")
    q_web=$(sh_single_quote "$web_url")
    info "正在手机端拉起应用商店详情页: $intent_uri"
    local out
    out=$(timeout --foreground 10s adb -s "$ADB_ADDR" shell "am start -a android.intent.action.VIEW -d $q_intent" 2>&1 || true)
    if echo "$out" | grep -qiE "Error:|Activity not started|unable to resolve"; then
        warn "原生商店协议未响应，回退打开网页详情页: $web_url"
        timeout --foreground 10s adb -s "$ADB_ADDR" shell "am start -a android.intent.action.VIEW -d $q_web" >/dev/null 2>&1 || true
    fi
}

is_official_still_newer_than_dumpsys() {
    local ds_text="$1"
    local rem_name="$2"
    local rem_code="${3:-0}"
    if [[ -z "$rem_name" && "$rem_code" -le 0 ]]; then
        return 1
    fi
    printf '%s' "$ds_text" | python3 - "$SCHEDULE_CONTROL_SCRIPT" "$rem_name" "$rem_code" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("maa_sc", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
inst = mod.parse_dumpsys_package_output(sys.stdin.read(524_288))
rem_name = sys.argv[2]
rem_code = int(sys.argv[3] or 0)
raise SystemExit(
    0
    if mod.is_remote_version_newer(
        str(inst["version_name"]), int(inst["version_code"]), rem_name, rem_code
    )
    else 1
)
PY
}

main() {
    ensure_adb || return 1

    local dumpsys_text inst_summary
    dumpsys_text=$(read_package_dumpsys)
    inst_summary=$(printf '%s\n' "$dumpsys_text" | format_dumpsys_summary)

    info "======================================="
    info "明日方舟客户端版本检查与更新 ($ACTIVE_CLIENT)"
    info "包名: $PKG"
    info "已安装: ${inst_summary:-未检测到已安装版本}"
    info "日志: $LOG_FILE"
    info "======================================="

    if [[ -n "$PACKAGE_FILE_OVERRIDE" ]]; then
        info "使用本地指定安装包直接安装: $PACKAGE_FILE_OVERRIDE"
        if [[ "$DRY_RUN" -eq 1 ]]; then
            warn "dry-run 模式，跳过本地包安装"
            return 0
        fi
        if install_package_artifact "$PACKAGE_FILE_OVERRIDE"; then
            local after_ver
            after_ver=$(installed_version_summary)
            send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 本地安装包覆盖安装成功。${after_ver:-unknown}"
            info "安装后版本: ${after_ver:-unknown}"
            return 0
        fi
        send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 本地安装包安装失败。日志: $LOG_FILE"
        return 1
    fi

    local check_args=(apk-check --config-dir "$MAA_CONFIG_DIR" --client "$ACTIVE_CLIENT" --installed-dumpsys-stdin --format shell)
    if [[ "$FORCE" -eq 1 ]]; then
        check_args+=(--force)
    fi
    if [[ "$FORCE_OFFICIAL" -eq 1 ]]; then
        check_args+=(--ignore-saved-custom-url)
    fi
    if [[ -n "$PACKAGE_URL_OVERRIDE" ]]; then
        check_args+=(--custom-url "$PACKAGE_URL_OVERRIDE")
    fi
    if [[ -n "$OFFICIAL_URL_OVERRIDE" && "$ACTIVE_CLIENT" == "Official" ]]; then
        check_args+=(--official-download-url "$OFFICIAL_URL_OVERRIDE")
    fi

    local check_env
    if ! check_env=$(printf '%s' "$dumpsys_text" | python3 "$SCHEDULE_CONTROL_SCRIPT" "${check_args[@]}" 2>>"$LOG_FILE" | tr -d '\r'); then
        error "查询 $ACTIVE_CLIENT 官方版本或下载源失败"
        send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 版本检查失败：无法连接版本配置接口。日志: $LOG_FILE"
        return 1
    fi
    eval "$check_env"

    info "策略: $APK_STRATEGY | 本地版本: ${APK_INST_VER_NAME:-未安装} (code=${APK_INST_VER_CODE}) | 官方最新: ${APK_REM_VER_NAME:-未知}${APK_REM_VER_CODE:+ (code=$APK_REM_VER_CODE)}"

    TMP_META=$(umask 077 && mktemp "${TMPDIR:-/tmp}/arknights-apk-meta.XXXXXX")
    local have_head_meta=0

    if [[ "$APK_STRATEGY" == "official_cdn" ]]; then
        if fetch_head_meta "$APK_DOWNLOAD_URL" "$APK_REFERER" "$TMP_META"; then
            normalize_meta "$TMP_META"
            have_head_meta=1
            info "远端 CDN metadata:"
            sed 's/^/  /' "$TMP_META" | tee -a "$LOG_FILE"
            if has_official_cdn_changed "$TMP_META" "$APK_INST_VER_NAME"; then
                APK_NEEDS_UPDATE="1"
            else
                if [[ "$APK_NEEDS_UPDATE" == "1" && "$FORCE" -eq 0 ]]; then
                    warn "官方版本配置已更新 (${APK_REM_VER_NAME})，但下载 CDN 仍为已安装版本 (${APK_INST_VER_NAME})，等待 CDN 同步后再下载"
                fi
                APK_NEEDS_UPDATE="0"
            fi
        elif [[ -z "$APK_REM_VER_NAME" ]]; then
            error "获取官服 APK CDN metadata 失败"
            send_notify_message "[MAA] 明日方舟官服 APK 更新检查失败：无法获取下载信息。日志: $LOG_FILE"
            return 1
        fi
    elif [[ "$APK_STRATEGY" == "custom_url" ]]; then
        if fetch_head_meta "$APK_DOWNLOAD_URL" "$APK_REFERER" "$TMP_META"; then
            normalize_meta "$TMP_META"
            have_head_meta=1
            if [[ "$FORCE" -eq 0 ]]; then
                if [[ -s "$META_FILE" ]] && [[ "$(meta_fingerprint "$META_FILE")" == "$(meta_fingerprint "$TMP_META")" ]]; then
                    if [[ "$APK_INSTALLED" == "1" ]]; then
                        if [[ "$APK_NEEDS_UPDATE" == "1" ]]; then
                            warn "官方已发布新版本 (${APK_REM_VER_NAME})，但自定义安装包直链内容尚未更新，跳过重复下载"
                            local stale_file="$STATE_DIR/arknights-custom-stale-${ACTIVE_CLIENT}.txt"
                            local last_stale=""
                            if [[ -s "$stale_file" ]]; then
                                last_stale=$(tr -d '\r\n' < "$stale_file")
                            fi
                            if [[ "$DRY_RUN" -eq 0 && "$last_stale" != "$APK_REM_VER_NAME" ]]; then
                                send_notify_message "[MAA] 检测到明日方舟 ($ACTIVE_CLIENT) 官方新版本 ${APK_REM_VER_NAME}（本地已装 ${APK_INST_VER_NAME:-未知}），但自定义安装包直链文件未发生变更，已跳过重复下载。请更新自定义直链安装包。"
                                printf '%s\n' "$APK_REM_VER_NAME" > "$stale_file"
                            fi
                        fi
                        APK_NEEDS_UPDATE="0"
                    fi
                elif [[ -s "$META_FILE" ]]; then
                    APK_NEEDS_UPDATE="1"
                elif [[ -z "$APK_REM_VER_NAME" ]]; then
                    APK_NEEDS_UPDATE="1"
                fi
            fi
        fi
    fi

    if [[ "$APK_NEEDS_UPDATE" != "1" && "$FORCE" -eq 0 ]]; then
        if [[ "$have_head_meta" -eq 1 ]]; then
            save_meta_file "$TMP_META" "$META_FILE"
        fi
        info "当前 $ACTIVE_CLIENT 客户端版本已是最新，无需更新"
        return 0
    fi

    if [[ "$APK_AUTO_INSTALL" != "1" ]]; then
        warn "检测到 $ACTIVE_CLIENT 客户端需要更新: 本地 ${APK_INST_VER_NAME:-未安装} -> 官方最新 ${APK_REM_VER_NAME}"
        warn "海外服官方仅通过 Google Play 分发 Split APKs；请前往商店更新，或在 WebUI 设置页配置自定义 APK/XAPK 直链。"
        if [[ "$OPEN_STORE" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
            open_client_store "$APK_STORE_INTENT" "$APK_STORE_WEB"
        fi
        local notified_file="$STATE_DIR/arknights-update-notified-${ACTIVE_CLIENT}.txt"
        local last_notified=""
        if [[ -s "$notified_file" ]]; then
            last_notified=$(tr -d '\r\n' < "$notified_file")
        fi
        if [[ "$DRY_RUN" -eq 0 && ( "$FORCE" -eq 1 || "$last_notified" != "$APK_REM_VER_NAME" ) ]]; then
            send_notify_message "[MAA] 检测到明日方舟 ($ACTIVE_CLIENT) 客户端本体更新：本地 ${APK_INST_VER_NAME:-未安装} -> 官方最新 ${APK_REM_VER_NAME}。请前往 Google Play 更新，或在设置页配置自定义 APK/XAPK 包地址。"
            printf '%s\n' "$APK_REM_VER_NAME" > "$notified_file"
        fi
        echo "--- APK_UPDATE_STORE_REQUIRED client=${ACTIVE_CLIENT} remote=${APK_REM_VER_NAME} ---"
        return 0
    fi

    warn "检测到 $ACTIVE_CLIENT 客户端存在新版本，准备下载并覆盖安装"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "dry-run 模式，不执行下载与安装"
        return 0
    fi

    check_free_space || return 1
    if ! download_package "$APK_DOWNLOAD_URL" "$APK_REFERER" "${APK_DOWNLOAD_URL_MASKED:-}"; then
        error "客户端安装包下载失败 ($ACTIVE_CLIENT)"
        send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 客户端安装包下载失败。日志: $LOG_FILE"
        return 1
    fi

    if install_package_artifact "$ARCHIVE_FILE"; then
        if [[ "$have_head_meta" -eq 1 ]]; then
            save_meta_file "$TMP_META" "$META_FILE"
        fi
        rm -f "$ARCHIVE_FILE"
        local after_ds new_ver
        after_ds=$(read_package_dumpsys)
        new_ver=$(printf '%s\n' "$after_ds" | format_dumpsys_summary)
        if [[ "$APK_STRATEGY" == "custom_url" ]] && is_official_still_newer_than_dumpsys "$after_ds" "${APK_REM_VER_NAME:-}" "${APK_REM_VER_CODE:-0}"; then
            warn "自定义安装包已安装 (${new_ver:-unknown})，但版本仍低于官方最新 ${APK_REM_VER_NAME}"
            printf '%s\n' "$APK_REM_VER_NAME" > "$STATE_DIR/arknights-custom-stale-${ACTIVE_CLIENT}.txt"
            send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 自定义安装包已覆盖安装 (${new_ver:-unknown})，但版本仍低于官方最新 ${APK_REM_VER_NAME}，请确认直链文件是否已同步更新。"
        else
            rm -f "$STATE_DIR/arknights-custom-stale-${ACTIVE_CLIENT}.txt"
            send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 客户端已自动更新并安装成功。${new_ver:-unknown}"
        fi
        info "安装后版本: ${new_ver:-unknown}"
        return 0
    fi

    rm -f "$ARCHIVE_FILE"
    send_notify_message "[MAA] 明日方舟 ($ACTIVE_CLIENT) 安装包下载完成但覆盖安装失败。日志: $LOG_FILE"
    return 1
}

main "$@"
