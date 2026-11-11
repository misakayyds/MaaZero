#!/bin/bash
# ============================================
# MAA 自动更新脚本
# 作用: 自动检查并更新 maa-cli, MaaCore 以及资源
# ============================================
set -uo pipefail

export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG=en_US.UTF-8

LOG_DIR="/var/log/maa"
LOG_FILE="$LOG_DIR/update-$(date +%Y%m%d-%H%M%S).log"
LOCK_FILE="/tmp/maa-task.lock"
UPDATE_TIMEOUT="15m"
UPDATE_MARKER="$LOG_DIR/last-update-ok"
UPDATE_RETRIES=2
UPDATE_RETRY_DELAY=20
RESOURCE_TARBALL_URL="https://codeload.github.com/MaaAssistantArknights/MaaResource/tar.gz/refs/heads/main"
RESOURCE_DIR="/root/.local/share/maa/resource"
RESOURCE_CLONE_DIR="/root/.local/share/maa/MaaResource/resource"
RESOURCE_OVERLAY_SCRIPT="/root/maa-scripts/maa-resource-overlay.sh"
MAA_CORE_CHANNEL="${MAA_CORE_CHANNEL:-beta}"
MAA_UPDATE_TEST_TIME="${MAA_UPDATE_TEST_TIME:-0}"

# === 锁保护 (防止更新与运行任务抢占) ===
if [[ "${FLOCKER:-}" != "$LOCK_FILE" ]]; then
    env FLOCKER="$LOCK_FILE" flock -n -E 75 -o "$LOCK_FILE" "$0" "$@"
    ec=$?
    if [[ "$ec" -eq 75 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已有 MAA 更新或任务正在运行，跳过本次更新"
        exit 0
    fi
    exit "$ec"
fi

mkdir -p "$LOG_DIR"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

# acquire_lock was removed because flock relaunch is handled at the top of the script

run_with_timeout() {
    local label="$1"
    shift

    log "$label"
    "$@" >> "$LOG_FILE" 2>&1
    local ec=$?
    return "$ec"
}

sync_resource_from_clone() {
    if [[ ! -d "$RESOURCE_CLONE_DIR" ]]; then
        log "未找到 MaaResource clone 资源目录，跳过 clone 同步: $RESOURCE_CLONE_DIR"
        return 0
    fi

    log "同步 MaaResource clone 到运行资源目录..."
    mkdir -p "$RESOURCE_DIR"
    if cp -a "$RESOURCE_CLONE_DIR"/. "$RESOURCE_DIR"/ >> "$LOG_FILE" 2>&1; then
        if ! apply_navigation_overlay "$RESOURCE_CLONE_DIR"; then
            log "❌ 导航覆盖层应用失败"
            return 1
        fi
        log "✅ 运行资源目录已同步: $RESOURCE_DIR"
        return 0
    fi

    log "❌ 运行资源目录同步失败"
    return 1
}

apply_navigation_overlay() {
    local source_dir="${1:-$RESOURCE_CLONE_DIR}"
    if [[ ! -x "$RESOURCE_OVERLAY_SCRIPT" ]]; then
        log "未找到导航覆盖层脚本，跳过: $RESOURCE_OVERLAY_SCRIPT"
        return 0
    fi

    log "检查官方主仓导航覆盖层..."
    LOG_FILE="$LOG_FILE" RESOURCE_DIR="$RESOURCE_DIR" RESOURCE_SOURCE_DIR="$source_dir" "$RESOURCE_OVERLAY_SCRIPT"
}

log_resource_versions() {
    python3 - "$RESOURCE_DIR/version.json" "$RESOURCE_CLONE_DIR/version.json" >> "$LOG_FILE" 2>&1 <<'PY' || true
import json
import sys
from pathlib import Path

for label, path in (("runtime", Path(sys.argv[1])), ("clone", Path(sys.argv[2]))):
    try:
        data = json.loads(path.read_text("utf-8"))
        activity = data.get("activity", {}).get("name", "unknown")
        gacha = data.get("gacha", {}).get("pool", "unknown")
        updated = data.get("last_updated", "unknown")
        print(f"[resource-version] {label}: activity={activity} gacha={gacha} last_updated={updated}")
    except Exception as exc:
        print(f"[resource-version] {label}: unavailable ({exc})")
PY
}

update_resource_from_tarball() {
    local work_dir="/tmp/maa-resource-tarball.$$"
    local tarball="/tmp/MaaResource-main.$$.tar.gz"
    local src_dir

    log "Git 更新失败，尝试通过官方 tarball 更新 MaaResource..."
    rm -rf "$work_dir" "$tarball"
    mkdir -p "$work_dir"

    if ! timeout -k 30s "$UPDATE_TIMEOUT" curl -L --fail --connect-timeout 20 --max-time 600 -o "$tarball" "$RESOURCE_TARBALL_URL" >> "$LOG_FILE" 2>&1; then
        rm -rf "$work_dir" "$tarball"
        return 1
    fi

    if ! tar -xzf "$tarball" -C "$work_dir" >> "$LOG_FILE" 2>&1; then
        rm -rf "$work_dir" "$tarball"
        return 1
    fi

    src_dir="$work_dir/MaaResource-main/resource"
    if [[ ! -d "$src_dir" ]]; then
        log "tarball 中未找到 resource 目录"
        rm -rf "$work_dir" "$tarball"
        return 1
    fi

    mkdir -p "$RESOURCE_DIR"
    if cp -a "$src_dir"/. "$RESOURCE_DIR"/ >> "$LOG_FILE" 2>&1; then
        if ! apply_navigation_overlay "$src_dir"; then
            rm -rf "$work_dir" "$tarball"
            return 1
        fi
        rm -rf "$work_dir" "$tarball"
        return 0
    fi

    rm -rf "$work_dir" "$tarball"
    return 1
}

log "=== 开始 MAA 检查更新流程 ==="
exit_code=0

# 1. maa-cli 自更新不放在高频路径，避免网络抖动拖住资源更新。
if [[ "${MAA_SELF_UPDATE:-0}" == "1" ]]; then
    if run_with_timeout "1. 正在检查并更新 maa-cli (self update)..." timeout -k 30s "$UPDATE_TIMEOUT" maa self update --batch; then
        log "✅ maa-cli 检查/更新成功"
    else
        log "⚠️ maa-cli 检查/更新失败，继续检查 MaaCore 与资源"
    fi
else
    log "1. 跳过 maa-cli 自更新；如需执行请设置 MAA_SELF_UPDATE=1"
fi

# 2. 自动更新 MaaCore 及资源
resource_updated=0
for attempt in $(seq 1 "$UPDATE_RETRIES"); do
    if run_with_timeout "2. 正在检查并更新 MaaCore 与资源 (channel=$MAA_CORE_CHANNEL, attempt=$attempt/$UPDATE_RETRIES)..." timeout -k 30s "$UPDATE_TIMEOUT" maa update "$MAA_CORE_CHANNEL" --batch --test-time "$MAA_UPDATE_TEST_TIME"; then
        if sync_resource_from_clone; then
            resource_updated=1
            break
        fi
        log "❌ MaaResource clone 同步失败 attempt=$attempt/$UPDATE_RETRIES"
    fi

    log "❌ MaaCore 与资源 检查/更新失败 attempt=$attempt/$UPDATE_RETRIES"
    if update_resource_from_tarball; then
        resource_updated=1
        break
    fi

    pkill -9 -f "git.*MaaResource" || true
    if [[ "$attempt" -lt "$UPDATE_RETRIES" ]]; then
        log "等待 ${UPDATE_RETRY_DELAY}s 后重试资源更新..."
        sleep "$UPDATE_RETRY_DELAY"
    fi
done

if [[ "$resource_updated" -eq 1 ]]; then
    log "✅ MaaCore 与资源 检查/更新成功"
    log_resource_versions
    date '+%Y-%m-%d %H:%M:%S' > "$UPDATE_MARKER"
    if python3 /root/maa-scripts/patch_config.py >> "$LOG_FILE" 2>&1; then
        log "✅ 稳定截图配置补丁已应用"
    else
        log "⚠️ 稳定截图配置补丁应用失败"
    fi
    if python3 /root/maa-scripts/maa_infrast_requirements.py \
        --update \
        --output /root/maa-scripts/infrast-operator-requirements.json \
        >> "$LOG_FILE" 2>&1; then
        log "✅ 基建技能建议门槛索引已更新"
    else
        log "⚠️ 基建技能建议门槛索引更新失败，保留上一份可用索引"
    fi
else
    log "❌ MaaCore 与资源 检查/更新最终失败"
    exit_code=1
fi

log "=== MAA 检查更新流程完成 ==="
# 清理超过 30 天的更新日志
find "$LOG_DIR" -name "update-*.log" -mtime +30 -delete 2>/dev/null || true
exit "$exit_code"
