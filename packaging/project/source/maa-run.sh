#!/bin/bash
# ============================================
# MAA 自动化运行脚本
# 用法: maa-run.sh [task_name] [--debug]
# 示例:
#   maa-run.sh              # 运行 daily 任务
#   maa-run.sh daily --debug  # 调试模式
# ============================================
set -uo pipefail

# 提升文件描述符上限以防 C++ Core fd 泄漏导致崩溃
ulimit -n 65536 2>/dev/null || true

export PATH="${HOME}/.local/bin:${MAA_HOME:-/root/maa-scripts}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG="${LANG:-en_US.UTF-8}"
export PYTHONDONTWRITEBYTECODE=1
export MAA_LOG=warn
export MAA_LOG_PREFIX=Auto

# === 配置 ===
TASK="daily"
BENCH_ONLY=0
STARTUP_HEALTH_ONLY=0
if [[ "${1:-}" == "--startup-health-check" ]]; then
    STARTUP_HEALTH_ONLY=1
    TASK="startup-health"
elif [[ "${1:-}" == "--bench-screencap" ]]; then
    BENCH_ONLY=1
    if [[ -n "${2:-}" && "${2:-}" != "--debug" ]]; then
        TASK="$2"
    else
        TASK="daily"
    fi
elif [[ -n "${1:-}" && "${1:-}" != --* ]]; then
    TASK="$1"
fi
REQUESTED_TASK="$TASK"
DEBUG=""
STOP_AT_LOGIN="${MAA_STARTUP_STOP_AT_LOGIN:-0}"
KEEP_GAME_ON_EXIT="${MAA_KEEP_GAME_ON_EXIT:-0}"
WEEKDAY_OVERRIDE=""
SCHEDULED_TIME=""
SCHEDULED_DATE=""
LOCK_WAIT_SECONDS=0
RETRY_ATTEMPT=""
INFRAST_NOW=0
SCHEDULED_MODE=0
scheduled_time_count=0
scheduled_date_count=0
lock_wait_count=0
retry_attempt_count=0
infrast_now_count=0
weekday_count=0
unknown_options=()
unexpected_positionals=()
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
    arg="${args[$i]}"
    case "$arg" in
        --startup-health-check|--bench-screencap)
            ;;
        --stop-at-login)
            STOP_AT_LOGIN=1
            ;;
        --keep-game-foreground)
            KEEP_GAME_ON_EXIT=1
            ;;
        --debug)
            DEBUG="1"
            ;;
        --weekday)
            weekday_count=$((weekday_count + 1))
            i=$((i + 1))
            WEEKDAY_OVERRIDE="${args[$i]:-}"
            ;;
        --scheduled-time)
            scheduled_time_count=$((scheduled_time_count + 1))
            i=$((i + 1))
            SCHEDULED_TIME="${args[$i]:-}"
            ;;
        --scheduled-date)
            scheduled_date_count=$((scheduled_date_count + 1))
            i=$((i + 1))
            SCHEDULED_DATE="${args[$i]:-}"
            ;;
        --lock-wait-seconds)
            lock_wait_count=$((lock_wait_count + 1))
            i=$((i + 1))
            LOCK_WAIT_SECONDS="${args[$i]:-}"
            ;;
        --retry-attempt)
            retry_attempt_count=$((retry_attempt_count + 1))
            i=$((i + 1))
            RETRY_ATTEMPT="${args[$i]:-}"
            ;;
        --infrast-now)
            infrast_now_count=$((infrast_now_count + 1))
            INFRAST_NOW=1
            ;;
        --*)
            unknown_options+=("$arg")
            ;;
        *)
            if (( i == 0 )) && [[ "$arg" != --* ]]; then
                :
            elif (( BENCH_ONLY == 1 && i == 1 )) && [[ "$arg" != --* ]]; then
                :
            else
                unexpected_positionals+=("$arg")
            fi
            ;;
    esac
done
if (( INFRAST_NOW == 1 )); then
    if (( infrast_now_count != 1 || scheduled_time_count + scheduled_date_count +
          lock_wait_count + retry_attempt_count + weekday_count > 0 )) ||
       [[ "$REQUESTED_TASK" != "daily" ]]; then
        echo "--infrast-now cannot be combined with scheduled arguments" >&2
        exit 2
    fi
    SCHEDULED_DATE=$(date +%F)
    SCHEDULED_TIME=$(date +%H:%M)
    case "$(date +%u)" in
        1) WEEKDAY_OVERRIDE=mon ;; 2) WEEKDAY_OVERRIDE=tue ;;
        3) WEEKDAY_OVERRIDE=wed ;; 4) WEEKDAY_OVERRIDE=thu ;;
        5) WEEKDAY_OVERRIDE=fri ;; 6) WEEKDAY_OVERRIDE=sat ;;
        7) WEEKDAY_OVERRIDE=sun ;;
    esac
    LOCK_WAIT_SECONDS=1800
    scheduled_time_count=1
    scheduled_date_count=1
    lock_wait_count=1
    weekday_count=1
fi
if [[ -n "$WEEKDAY_OVERRIDE" && ! "$WEEKDAY_OVERRIDE" =~ ^(mon|tue|wed|thu|fri|sat|sun)$ ]]; then
    echo "Invalid --weekday value: ${WEEKDAY_OVERRIDE:-missing}" >&2
    exit 2
fi
if (( scheduled_time_count + scheduled_date_count + lock_wait_count + retry_attempt_count > 0 )); then
    SCHEDULED_MODE=1
fi
if (( SCHEDULED_MODE == 1 )); then
    if (( scheduled_time_count != 1 || lock_wait_count != 1 || weekday_count != 1 ||
          scheduled_date_count > 1 || retry_attempt_count > 1 )); then
        echo "Invalid or duplicate scheduled arguments" >&2
        exit 2
    fi
    if (( ${#unknown_options[@]} > 0 || ${#unexpected_positionals[@]} > 0 )); then
        echo "Unknown option or unexpected positional in scheduled mode" >&2
        exit 2
    fi
    if [[ "$SCHEDULED_TIME" == --* || ! "$SCHEDULED_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
        echo "Invalid --scheduled-time value" >&2
        exit 2
    fi
    if [[ "$LOCK_WAIT_SECONDS" == --* || ! "$LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]] ||
       (( LOCK_WAIT_SECONDS < 0 || LOCK_WAIT_SECONDS > 1800 )); then
        echo "Invalid --lock-wait-seconds value" >&2
        exit 2
    fi
    if [[ -n "$RETRY_ATTEMPT" ]] &&
       [[ "$RETRY_ATTEMPT" == --* || ! "$RETRY_ATTEMPT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
        echo "Invalid --retry-attempt value" >&2
        exit 2
    fi
    if [[ -n "${MAA_SCHEDULED_LOGICAL_DATE:-}" ]]; then
        if [[ -n "$SCHEDULED_DATE" && "$SCHEDULED_DATE" != "$MAA_SCHEDULED_LOGICAL_DATE" ]]; then
            echo "Scheduled logical date changed across lock wait" >&2
            exit 2
        fi
        SCHEDULED_DATE="$MAA_SCHEDULED_LOGICAL_DATE"
    elif [[ -z "$SCHEDULED_DATE" ]]; then
        SCHEDULED_DATE=$(date +%F)
    fi
    if [[ ! "$SCHEDULED_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] ||
       ! date -d "$SCHEDULED_DATE" +%F >/dev/null 2>&1; then
        echo "Invalid --scheduled-date value" >&2
        exit 2
    fi
    scheduled_weekday=$(date -d "$SCHEDULED_DATE" +%u 2>/dev/null || true)
    weekday_number=""
    case "$WEEKDAY_OVERRIDE" in
        mon) weekday_number=1 ;; tue) weekday_number=2 ;; wed) weekday_number=3 ;;
        thu) weekday_number=4 ;; fri) weekday_number=5 ;; sat) weekday_number=6 ;;
        sun) weekday_number=7 ;;
    esac
    if [[ "$scheduled_weekday" != "$weekday_number" ]]; then
        echo "--scheduled-date weekday does not match --weekday" >&2
        exit 2
    fi
elif (( retry_attempt_count > 0 )); then
    echo "--retry-attempt requires scheduled mode" >&2
    exit 2
fi
MAA_HOME="${MAA_HOME:-/root/maa-scripts}"
MAA_CONFIG_DIR="${MAA_CONFIG_DIR:-/root/.config/maa}"
MAA_DATA_DIR="${MAA_DATA_DIR:-/root/.local/share/maa}"
MAA_STATE_DIR="${MAA_STATE_DIR:-/root/.local/state/maa}"
MAA_CACHE_DIR="${MAA_CACHE_DIR:-/root/.cache/maa}"
MAA_LOG_DIR="${MAA_LOG_DIR:-/var/log/maa}"
MAA_ADB_ADDR="${MAA_ADB_ADDR:-127.0.0.1:5555}"
export MAA_HOME MAA_CONFIG_DIR MAA_DATA_DIR MAA_STATE_DIR MAA_CACHE_DIR MAA_LOG_DIR MAA_ADB_ADDR

ADB_ADDR="$MAA_ADB_ADDR"
ADB_DEVICE="-s $ADB_ADDR"
AK_PACKAGE="com.hypergryph.arknights"
LOG_DIR="$MAA_LOG_DIR"
LOG_FILE="$LOG_DIR/maa-$(date +%Y%m%d-%H%M%S).log"
FAILURE_SNAPSHOT_DIR="$LOG_DIR/failure-snapshots"
FAILURE_SNAPSHOT_KEEP_DAYS=14
STARTUP_HEALTH_STATE_FILE="$LOG_DIR/startup-health.env"
STARTUP_HEALTH_TIMEOUT="${MAA_STARTUP_HEALTH_TIMEOUT:-15m}"
LOCK_FILE="/tmp/maa-task.lock"
RUNNER_PID_FILE="${MAA_RUNNER_PID_FILE:-$MAA_STATE_DIR/maa-runner.pid}"
SCHEDULE_CONTROL_SCRIPT="${MAA_SCHEDULE_CONTROL_SCRIPT:-$MAA_HOME/maa_schedule_control.py}"
SCHEDULE_CONTROL_CONFIG_DIR="${MAA_SCHEDULE_CONTROL_CONFIG_DIR:-$MAA_CONFIG_DIR}"
UPDATE_MARKER="$LOG_DIR/last-update-ok"
MAX_RETRIES=3
RETRY_DELAY=30
UPDATE_RETRIES=2
UPDATE_RETRY_DELAY=20
UPDATE_TIMEOUT="15m"
TASK_TIMEOUT="150m"
CLOSING_SCAN_TIMEOUT="20m"
LONG_RUN_ALERT_AFTER=3600
LONG_RUN_ALERT_INTERVAL=1800
SCREENCAP_FAIL_LIMIT=20
SCREENCAP_GEOMETRY_CHECK_AFTER=3
SCREENCAP_BENCH_SAMPLES=10
SCREENCAP_WARN_AVG_MS=800
SCREENCAP_WARN_MAX_MS=2000
MIN_MEM_AVAILABLE_KB="${MAA_MIN_MEM_AVAILABLE_KB:-1572864}"
MAX_SWAP_USED_KB="${MAA_MAX_SWAP_USED_KB:-3145728}"
RUNTIME_MIN_MEM_AVAILABLE_KB="${MAA_RUNTIME_MIN_MEM_AVAILABLE_KB:-524288}"
RESOURCE_TARBALL_URL="${MAA_RESOURCE_TARBALL_URL:-https://codeload.github.com/MaaAssistantArknights/MaaResource/tar.gz/refs/heads/main}"
RESOURCE_DIR="${MAA_RESOURCE_DIR:-$MAA_DATA_DIR/resource}"
RESOURCE_CLONE_DIR="${MAA_RESOURCE_CLONE_DIR:-$MAA_DATA_DIR/MaaResource/resource}"
RESOURCE_OVERLAY_SCRIPT="${MAA_RESOURCE_OVERLAY_SCRIPT:-$MAA_HOME/maa-resource-overlay.sh}"
TASK_GUARD_SCRIPT="${MAA_TASK_GUARD_SCRIPT:-$MAA_HOME/maa-task-guard.py}"
WEEKLY_PLAN_SCRIPT="${MAA_WEEKLY_PLAN_SCRIPT:-$MAA_HOME/maa_weekly_plan.py}"
INFRAST_RUNTIME_SCRIPT="${MAA_INFRAST_RUNTIME_SCRIPT:-$MAA_HOME/maa_infrast_runtime.py}"
CULTIVATION_RUN_SCRIPT="${MAA_CULTIVATION_RUN_SCRIPT:-$MAA_HOME/maa-cultivation-run.py}"
RUN_SESSION_SCRIPT="${MAA_RUN_SESSION_SCRIPT:-$MAA_HOME/maa_run_session.py}"
export MAA_RUN_SESSION=""
export MAA_PREWARM_TOKEN=""
export MAA_STARTUP_STOP_AT_LOGIN="$STOP_AT_LOGIN"
export MAA_KEEP_GAME_ON_EXIT="$KEEP_GAME_ON_EXIT"
INFRAST_LEDGER_FILE="${MAA_INFRAST_LEDGER_FILE:-$MAA_STATE_DIR/infrast-schedule-ledger.json}"
ACTIVITY_STAGE_FILE="${MAA_ACTIVITY_STAGE_FILE:-$MAA_CACHE_DIR/StageActivityV2.json}"
MEDICINE_PLAN_SCRIPT="${MAA_MEDICINE_PLAN_SCRIPT:-$MAA_HOME/maa_medicine_service.py}"
MAA_CORE_CHANNEL="${MAA_CORE_CHANNEL:-beta}"
MAA_UPDATE_TEST_TIME="${MAA_UPDATE_TEST_TIME:-0}"
ASST_LOG_DIR="${MAA_ASST_LOG_DIR:-$MAA_STATE_DIR/debug}"
MAA_RESOLUTION="1080x1920"
MAA_RESOLUTION_WIDTH="${MAA_RESOLUTION%x*}"
MAA_RESOLUTION_HEIGHT="${MAA_RESOLUTION#*x}"
MAA_RESOLUTION_REVERSED="${MAA_RESOLUTION_HEIGHT}x${MAA_RESOLUTION_WIDTH}"
RAW_SCREENCAP_EXPECTED_SIZE=$((MAA_RESOLUTION_WIDTH * MAA_RESOLUTION_HEIGHT * 4 + 16))
RAW_SCREENCAP_MIN_SIZE=$((RAW_SCREENCAP_EXPECTED_SIZE - 200000))
RAW_SCREENCAP_MAX_SIZE=$((RAW_SCREENCAP_EXPECTED_SIZE + 200000))
RESOLUTION_CHANGED=0
ORIG_ACCELEROMETER_ROTATION=""
ORIG_USER_ROTATION=""
ROTATION_LOCKED=0
TASK_STARTED_AT=0
WATCHDOG_PID=""
STARTUP_OVERLAY_WATCHDOG_PID=""
CLEANUP_DONE=0
CURRENT_SCREENCAP_METHOD=""
RAW_BY_NC_BLACKLISTED=0
RAW_HEADER_MISMATCHED=0
SCREENCAP_BENCH_RESULT_FILE=""
SCREENCAP_METHOD_STATE="/tmp/maa-current-screencap-method"
FAILURE_SNAPSHOT_ARCHIVE=""
FAILURE_REASON="UNSET"
STARTUP_HEALTH_STATUS="not_run"
STARTUP_HEALTH_REASON=""
STARTUP_HEALTH_SCREEN="unknown"
STARTUP_HEALTH_LOG_FILE=""
STARTUP_HEALTH_SCREENSHOT=""
STARTUP_HEALTH_LOGCAT_FILE=""
EFFECTIVE_TASK_NAME=""
EFFECTIVE_TASK_FILE=""
WEEKLY_LOGICAL_WEEKDAY=""
WEEKLY_SOURCE_PATH=""
WEEKLY_REQUESTED_PATH=""
WEEKLY_ENABLED=0
WEEKLY_FALLBACK_REASON=""
ACTIVITY_EFFECTIVE_TASK_FILE=""
SCHEDULE_CLAIM_TASK_FILE=""
SCHEDULE_EXECUTION_KEY=""
SCHEDULE_ATTEMPT_ID=""
SCHEDULE_INFRAST_OMITTED=0
SCHEDULE_OMIT_REASON=""
SCHEDULE_DOWNSTREAM_COUNTS="{}"
SCHEDULE_MARK_FAILED=0
SCHEDULE_NO_INFRAST_RETRY=0
SCHEDULE_OUTCOME=""
PARTIAL_SUCCESS=0
declare -A SCHEDULE_EXPECTED_COUNTS=()
declare -A SCHEDULE_COMPLETED_COUNTS=()
SCHEDULE_DOWNSTREAM_ERROR_COUNT=0
SCHEDULE_EVENT_AMBIGUOUS=0

# === 日志函数 ===
mkdir -p "$LOG_DIR"

log() {
    local level="$1"; shift
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

info()  { log "INFO"  "$@"; }
warn()  { log "WARN"  "$@"; }
error() { log "ERROR" "$@"; }
debug() { if [[ -n "$DEBUG" ]]; then log "DEBUG" "$@"; fi; }

resolve_weekly_task() {
    local config_dir resolved
    if [[ "$REQUESTED_TASK" != "daily" || "$BENCH_ONLY" -eq 1 || "$STARTUP_HEALTH_ONLY" -eq 1 ]]; then
        return 0
    fi

    config_dir=$(maa dir config 2>/dev/null || printf '%s' "$MAA_CONFIG_DIR")
    if [[ ! -f "$WEEKLY_PLAN_SCRIPT" ]]; then
        TASK="daily"
        WEEKLY_FALLBACK_REASON="resolver_missing"
        warn "周计划解析器不存在，已回退 daily.toml: $WEEKLY_PLAN_SCRIPT"
        return 0
    fi

    local cmd=(python3 "$WEEKLY_PLAN_SCRIPT" resolve --config-dir "$config_dir" --format shell)
    if [[ -n "$WEEKDAY_OVERRIDE" ]]; then
        cmd+=(--weekday "$WEEKDAY_OVERRIDE")
    fi
    if ! resolved=$("${cmd[@]}" 2>>"$LOG_FILE"); then
        TASK="daily"
        WEEKLY_LOGICAL_WEEKDAY="${WEEKDAY_OVERRIDE:-$(date +%a | tr '[:upper:]' '[:lower:]')}"
        WEEKLY_SOURCE_PATH="$config_dir/tasks/daily.toml"
        WEEKLY_REQUESTED_PATH=""
        WEEKLY_FALLBACK_REASON="resolver_error"
        warn "周计划解析失败，已回退 daily.toml"
        return 0
    fi

    eval "$resolved"
    TASK="$WEEKLY_EFFECTIVE_TASK"
    if [[ -n "$WEEKLY_FALLBACK_REASON" ]]; then
        warn "周计划回退: weekday=$WEEKLY_LOGICAL_WEEKDAY reason=$WEEKLY_FALLBACK_REASON fallback=daily source=$WEEKLY_REQUESTED_PATH"
    elif [[ "$WEEKLY_ENABLED" == "1" || -n "$WEEKDAY_OVERRIDE" ]]; then
        info "周计划选择: weekday=$WEEKLY_LOGICAL_WEEKDAY task=$TASK source=$WEEKLY_SOURCE_PATH"
    else
        info "周计划未启用: task=daily"
    fi
}

reset_runtime_protocol() {
    RT_RESULT=""
    RT_EXECUTION_KEY=""
    RT_ATTEMPT_ID=""
    RT_EFFECTIVE_TASK=""
    RT_INFRAST_OMITTED=""
    RT_OMIT_REASON=""
    RT_STATE=""
    RT_DOWNSTREAM_COUNTS=""
}

decode_runtime_value() {
    local encoded="$1"
    local key="$2"
    local output="$3"
    python3 - "$encoded" "$key" "$output" <<'PY'
import base64
import json
import re
import sys

encoded, key, output = sys.argv[1:]
if not re.fullmatch(r"[A-Za-z0-9_-]*", encoded) or len(encoded) % 4 == 1:
    raise SystemExit(2)
try:
    raw = base64.b64decode(
        encoded + "=" * (-len(encoded) % 4),
        altchars=b"-_",
        validate=True,
    )
    value = raw.decode("utf-8")
except (ValueError, UnicodeDecodeError):
    raise SystemExit(2)
if any(mark in value for mark in ("\x00", "\r", "\n")):
    raise SystemExit(2)
if key == "DOWNSTREAM_COUNTS":
    try:
        counts = json.loads(value)
    except json.JSONDecodeError:
        raise SystemExit(2)
    if (
        not isinstance(counts, dict)
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for name, count in counts.items()
        )
        or json.dumps(counts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        != value
    ):
        raise SystemExit(2)
with open(output, "wb") as stream:
    stream.write(raw)
PY
}

parse_runtime_protocol() {
    local protocol_file="$1"
    local line key encoded value_file value field_count=0
    local decode_dir="/tmp/maa-runtime-decode.$$"
    declare -A seen=()

    reset_runtime_protocol
    rm -rf "$decode_dir"
    mkdir -p "$decode_dir" || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" != *=* || "$line" == *"="*"="* ]]; then
            rm -rf "$decode_dir"
            return 1
        fi
        key="${line%%=*}"
        encoded="${line#*=}"
        case "$key" in
            RESULT|EXECUTION_KEY|ATTEMPT_ID|EFFECTIVE_TASK|INFRAST_OMITTED|OMIT_REASON|STATE|DOWNSTREAM_COUNTS) ;;
            *) rm -rf "$decode_dir"; return 1 ;;
        esac
        if [[ -n "${seen[$key]+x}" ]]; then
            rm -rf "$decode_dir"
            return 1
        fi
        seen[$key]=1
        value_file="$decode_dir/$key"
        if ! decode_runtime_value "$encoded" "$key" "$value_file"; then
            rm -rf "$decode_dir"
            return 1
        fi
        value=""
        IFS= read -r value < "$value_file" || [[ -n "$value" ]] || true
        case "$key" in
            RESULT) RT_RESULT="$value" ;;
            EXECUTION_KEY) RT_EXECUTION_KEY="$value" ;;
            ATTEMPT_ID) RT_ATTEMPT_ID="$value" ;;
            EFFECTIVE_TASK) RT_EFFECTIVE_TASK="$value" ;;
            INFRAST_OMITTED) RT_INFRAST_OMITTED="$value" ;;
            OMIT_REASON) RT_OMIT_REASON="$value" ;;
            STATE) RT_STATE="$value" ;;
            DOWNSTREAM_COUNTS) RT_DOWNSTREAM_COUNTS="$value" ;;
        esac
        field_count=$((field_count + 1))
    done < "$protocol_file"
    rm -rf "$decode_dir"

    case "$RT_RESULT" in
        not_managed)
            (( field_count == 1 )) &&
                [[ -n "${seen[RESULT]+x}" ]]
            ;;
        not_due)
            (( field_count == 3 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[INFRAST_OMITTED]+x}" &&
                   -n "${seen[OMIT_REASON]+x}" && "$RT_INFRAST_OMITTED" == "1" ]]
            ;;
        stale_schedule)
            (( field_count == 3 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[INFRAST_OMITTED]+x}" &&
                   -n "${seen[OMIT_REASON]+x}" && "$RT_INFRAST_OMITTED" == "1" ]]
            ;;
        claimed)
            (( field_count == 5 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[EXECUTION_KEY]+x}" &&
                   -n "${seen[ATTEMPT_ID]+x}" && -n "${seen[INFRAST_OMITTED]+x}" &&
                   -n "${seen[OMIT_REASON]+x}" &&
                   "$RT_INFRAST_OMITTED" =~ ^[01]$ ]]
            ;;
        duplicate)
            (( field_count == 3 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[EXECUTION_KEY]+x}" &&
                   -n "${seen[ATTEMPT_ID]+x}" ]]
            ;;
        prepared)
            (( field_count == 5 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[EFFECTIVE_TASK]+x}" &&
                   -n "${seen[INFRAST_OMITTED]+x}" && -n "${seen[OMIT_REASON]+x}" &&
                   -n "${seen[DOWNSTREAM_COUNTS]+x}" &&
                   "$RT_INFRAST_OMITTED" =~ ^[01]$ ]]
            ;;
        marked)
            (( field_count == 4 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[EXECUTION_KEY]+x}" &&
                   -n "${seen[ATTEMPT_ID]+x}" && -n "${seen[STATE]+x}" ]]
            ;;
        status)
            (( field_count == 4 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[EXECUTION_KEY]+x}" &&
                   -n "${seen[ATTEMPT_ID]+x}" && -n "${seen[STATE]+x}" ]]
            ;;
        not_found)
            (( field_count == 2 )) &&
                [[ -n "${seen[RESULT]+x}" && -n "${seen[EXECUTION_KEY]+x}" ]]
            ;;
        *) return 1 ;;
    esac
}

run_runtime_command() {
    local output="/tmp/maa-runtime-output.$$"
    local errors="/tmp/maa-runtime-error.$$"
    local ec
    rm -f "$output" "$errors"
    python3 "$INFRAST_RUNTIME_SCRIPT" "$@" > "$output" 2> "$errors"
    ec=$?
    if (( ec != 0 )); then
        error "基建运行时命令失败: command=${1:-unknown} exit=$ec detail=$(tr '\r\n' '  ' < "$errors" | cut -c1-300)"
        rm -f "$output" "$errors"
        return "$ec"
    fi
    if ! parse_runtime_protocol "$output"; then
        error "基建运行时协议无效: command=${1:-unknown}"
        rm -f "$output" "$errors"
        return 5
    fi
    rm -f "$output" "$errors"
    return 0
}

prepare_activity_config_for_schedule() {
    local task="$1"
    local config_dir="$2"
    local source="$config_dir/tasks/${task}.toml"
    local dest="$config_dir/tasks/${task}-activity-$$.toml"

    if [[ ! -f "$source" ]]; then
        error "计划任务 TOML 不存在: $source"
        return 1
    fi
    SCHEDULE_CLAIM_TASK_FILE="$source"
    if [[ -f "$TASK_GUARD_SCRIPT" ]] &&
       python3 "$TASK_GUARD_SCRIPT" \
           --task-file "$source" \
           --activity-file "$ACTIVITY_STAGE_FILE" \
           --output "$dest" >> "$LOG_FILE" 2>&1; then
        ACTIVITY_EFFECTIVE_TASK_FILE="$dest"
        SCHEDULE_CLAIM_TASK_FILE="$dest"
        info "活动关卡守门已先生成计划任务基线: $(basename "$dest")"
    else
        rm -f "$dest" 2>/dev/null || true
        warn "活动关卡守门未生成临时配置，计划任务冻结原始 TOML"
    fi
    return 0
}

initialize_scheduled_execution() {
    local config_dir claim_rc
    if (( SCHEDULED_MODE == 0 )); then
        return 0
    fi
    if [[ ! -f "$INFRAST_RUNTIME_SCRIPT" ]]; then
        error "基建运行时守门脚本不存在: $INFRAST_RUNTIME_SCRIPT"
        return 21
    fi
    config_dir=$(maa dir config 2>/dev/null || printf '%s' "$MAA_CONFIG_DIR")
    if ! prepare_activity_config_for_schedule "$TASK" "$config_dir"; then
        return 21
    fi
    claim_args=(
        claim
        --task-file "$SCHEDULE_CLAIM_TASK_FILE"
        --schedule-file "$config_dir/infrast-schedule.json"
        --infrast-dir "$config_dir/infrast"
        --ledger "$INFRAST_LEDGER_FILE"
        --logical-date "$SCHEDULED_DATE"
        --weekday "$WEEKDAY_OVERRIDE"
        --scheduled-time "$SCHEDULED_TIME"
    )
    if [[ -n "$RETRY_ATTEMPT" ]]; then
        claim_args+=(--retry-attempt "$RETRY_ATTEMPT")
    fi
    if (( INFRAST_NOW == 1 )); then
        claim_args+=(--current)
        info "MANUAL_INFRAST_CURRENT: 按当前时间选择应执行班次"
    fi
    run_runtime_command "${claim_args[@]}"
    claim_rc=$?
    if (( claim_rc != 0 )); then
        set_failure_reason "SCHEDULE_CLAIM_FAILED"
        return "$claim_rc"
    fi
    case "$RT_RESULT" in
        not_due)
            info "SCHEDULE_NOT_DUE_SKIPPED: 当前候选时间不是跨日循环边界"
            return 10
            ;;
        duplicate)
            SCHEDULE_EXECUTION_KEY="$RT_EXECUTION_KEY"
            SCHEDULE_ATTEMPT_ID="$RT_ATTEMPT_ID"
            info "SCHEDULE_DUPLICATE_SKIPPED: key=$SCHEDULE_EXECUTION_KEY attempt=$SCHEDULE_ATTEMPT_ID"
            return 10
            ;;
        stale_schedule)
            set_failure_reason "SCHEDULE_STALE"
            error "SCHEDULE_STALE: reason=$RT_OMIT_REASON"
            send_tg_message "[MAA] 基建排班已取消：当前排班清单不可用（${RT_OMIT_REASON}）。"
            return 20
            ;;
        claimed)
            SCHEDULE_EXECUTION_KEY="$RT_EXECUTION_KEY"
            SCHEDULE_ATTEMPT_ID="$RT_ATTEMPT_ID"
            SCHEDULE_INFRAST_OMITTED="$RT_INFRAST_OMITTED"
            SCHEDULE_OMIT_REASON="$RT_OMIT_REASON"
            if [[ "$SCHEDULE_INFRAST_OMITTED" == "1" ]]; then
                warn "计划任务已安全省略 Infrast: reason=$SCHEDULE_OMIT_REASON"
            fi
            return 0
            ;;
        *)
            set_failure_reason "SCHEDULE_CLAIM_UNKNOWN"
            error "未知 claim 结果，已在设备准备前终止: ${RT_RESULT:-missing}"
            return 21
            ;;
    esac
}

mark_scheduled_state() {
    local state="$1"
    local detail="${2:-}"
    if (( SCHEDULED_MODE == 0 )); then
        return 0
    fi
    if ! run_runtime_command mark \
        --ledger "$INFRAST_LEDGER_FILE" \
        --execution-key "$SCHEDULE_EXECUTION_KEY" \
        --attempt-id "$SCHEDULE_ATTEMPT_ID" \
        --state "$state" \
        --detail "$detail"; then
        SCHEDULE_MARK_FAILED=1
        SCHEDULE_NO_INFRAST_RETRY=1
        return 1
    fi
    if [[ "$RT_RESULT" == "marked" &&
          "$RT_EXECUTION_KEY" == "$SCHEDULE_EXECUTION_KEY" &&
          "$RT_ATTEMPT_ID" == "$SCHEDULE_ATTEMPT_ID" &&
          "$RT_STATE" == "$state" ]]; then
        return 0
    fi
    SCHEDULE_MARK_FAILED=1
    SCHEDULE_NO_INFRAST_RETRY=1
    return 1
}

state_value() {
    printf '%s' "${1:-}" | tr '\r\n' '  ' | sed 's/[[:cntrl:]]//g'
}

set_failure_reason() {
    local reason="${1:-UNKNOWN}"
    if [[ -z "${FAILURE_REASON:-}" || "$FAILURE_REASON" == "UNSET" ]]; then
        FAILURE_REASON="$reason"
    fi
}

write_startup_health_state() {
    local status="${1:-unknown}"
    local reason="${2:-unknown}"
    local screen="${3:-unknown}"
    local detail="${4:-}"
    local tmp="${STARTUP_HEALTH_STATE_FILE}.$$"

    STARTUP_HEALTH_STATUS="$status"
    STARTUP_HEALTH_REASON="$reason"
    STARTUP_HEALTH_SCREEN="$screen"

    {
        echo "time=$(date '+%Y-%m-%d %H:%M:%S %Z')"
        echo "status=$(state_value "$status")"
        echo "reason=$(state_value "$reason")"
        echo "screen=$(state_value "$screen")"
        echo "detail=$(state_value "$detail")"
        echo "task=$(state_value "$TASK")"
        echo "log=$(state_value "${STARTUP_HEALTH_LOG_FILE:-$LOG_FILE}")"
        echo "screenshot=$(state_value "${STARTUP_HEALTH_SCREENSHOT:-}")"
        echo "failure_reason=$(state_value "${FAILURE_REASON:-UNSET}")"
    } > "$tmp" 2>/dev/null && mv "$tmp" "$STARTUP_HEALTH_STATE_FILE" 2>/dev/null || true
}

# === 退出清理 ===
cleanup() {
    if [[ "$CLEANUP_DONE" -eq 1 ]]; then
        return 0
    fi
    CLEANUP_DONE=1
    info "执行清理..."
    stop_startup_overlay_watchdog
    stop_task_watchdog
    cleanup_effective_task_config
    if [[ -n "${ACTIVITY_EFFECTIVE_TASK_FILE:-}" ]]; then
        rm -f "$ACTIVITY_EFFECTIVE_TASK_FILE" 2>/dev/null || true
    fi
    ACTIVITY_EFFECTIVE_TASK_FILE=""
    SCHEDULE_CLAIM_TASK_FILE=""
    rm -f "$SCREENCAP_METHOD_STATE"
    restore_resolution
    restore_rotation
    if [[ "${KEEP_GAME_ON_EXIT:-0}" -ne 1 ]]; then
        kill_arknights
    else
        info "已启用保留游戏前台 (--keep-game-foreground)，跳过关闭明日方舟"
    fi
    rm -f "$RUNNER_PID_FILE" 2>/dev/null || true
    info "清理完成"
}

terminate_runner_pid() {
    local target_pid="$1"
    if [[ -n "$target_pid" && "$target_pid" =~ ^[0-9]+$ ]] && kill -0 "$target_pid" 2>/dev/null; then
        kill -TERM -- -"$target_pid" 2>/dev/null || kill -TERM "$target_pid" 2>/dev/null || true
        local waited=0
        while kill -0 "$target_pid" 2>/dev/null && (( waited < 3 )); do
            sleep 1
            waited=$((waited + 1))
        done
        if kill -0 "$target_pid" 2>/dev/null; then
            kill -KILL -- -"$target_pid" 2>/dev/null || kill -KILL "$target_pid" 2>/dev/null || true
        fi
    fi
}

kill_stale_processes() {
    if [[ -f "$RUNNER_PID_FILE" ]]; then
        local old_pid
        old_pid=$(cat "$RUNNER_PID_FILE" 2>/dev/null || true)
        if [[ "$old_pid" == "$$" ]]; then
            return 0
        fi
        if [[ -n "$old_pid" && "$old_pid" =~ ^[0-9]+$ && "$old_pid" != "${PPID:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
            local cmdline
            cmdline=$(tr '\0' ' ' < "/proc/$old_pid/cmdline" 2>/dev/null || true)
            if [[ "$cmdline" == *"maa"* || "$cmdline" == *"python"* ]]; then
                info "清理上一次残留的 runner 进程 (PID: $old_pid)..."
                terminate_runner_pid "$old_pid"
            fi
        fi
        rm -f "$RUNNER_PID_FILE"
    fi
}

# === 宿主设备与分辨率控制 (通过 ADB 执行) ===

restart_android_adbd_tcp() {
    warn "ADB TCP 未就绪 ($ADB_ADDR)。请确保 Android 宿主 ADB 调试已开启或由控制面 daemon 托管。"
    return 0
}

restore_android_setting() {
    local key="$1"
    local value="$2"
    if [[ -n "$value" && "$value" != "null" ]]; then
        adb $ADB_DEVICE shell settings put system "$key" "$value" 2>/dev/null || true
    else
        adb $ADB_DEVICE shell settings delete system "$key" 2>/dev/null || true
    fi
}

lock_rotation() {
    if [[ "$ROTATION_LOCKED" -eq 0 ]]; then
        ORIG_ACCELEROMETER_ROTATION=$(adb $ADB_DEVICE shell settings get system accelerometer_rotation 2>/dev/null | tr -d '\r\n' || true)
        ORIG_USER_ROTATION=$(adb $ADB_DEVICE shell settings get system user_rotation 2>/dev/null | tr -d '\r\n' || true)
        info "锁定系统旋转: accelerometer=${ORIG_ACCELEROMETER_ROTATION:-unknown} user_rotation=${ORIG_USER_ROTATION:-unknown} -> landscape"
    else
        info "系统旋转已锁定，重新应用横屏"
    fi

    adb $ADB_DEVICE shell settings put system accelerometer_rotation 0 2>/dev/null || true
    adb $ADB_DEVICE shell settings put system user_rotation 1 2>/dev/null || true
    ROTATION_LOCKED=1
    sleep 1
}

restore_rotation() {
    if [[ "$ROTATION_LOCKED" -eq 1 ]]; then
        info "恢复系统旋转设置..."
        restore_android_setting "accelerometer_rotation" "$ORIG_ACCELEROMETER_ROTATION"
        restore_android_setting "user_rotation" "$ORIG_USER_ROTATION"
        ROTATION_LOCKED=0
        info "系统旋转设置已恢复"
    fi
}

set_maa_resolution() {
    local current
    current=$(adb $ADB_DEVICE shell wm size 2>/dev/null | grep "Physical" | awk '{print $3}')
    debug "原始分辨率: $current"

    if [[ "$current" == "$MAA_RESOLUTION" || "$current" == "$MAA_RESOLUTION_REVERSED" ]]; then
        info "分辨率已是 16:9，无需修改"
        return 0
    fi

    info "尝试设置 MAA 分辨率: $MAA_RESOLUTION (原始: $current)"
    adb $ADB_DEVICE shell wm size "$MAA_RESOLUTION" 2>/dev/null || true
    sleep 2

    local verify
    verify=$(adb $ADB_DEVICE shell wm size 2>/dev/null | grep "Override" | awk '{print $3}')
    if [[ "$verify" == "$MAA_RESOLUTION" ]]; then
        RESOLUTION_CHANGED=1
        info "分辨率设置成功: $verify"
    else
        warn "分辨率修改未生效或受宿主安全策略限制 (当前: $(adb $ADB_DEVICE shell wm size 2>/dev/null || echo unknown))"
    fi
}

restore_resolution() {
    if [[ "$RESOLUTION_CHANGED" -eq 1 ]]; then
        info "恢复原始分辨率..."
        adb $ADB_DEVICE shell wm size reset 2>/dev/null || true
        sleep 1
        RESOLUTION_CHANGED=0
        info "分辨率已恢复"
    fi
}

# === ADB 函数 ===
ensure_adb() {
    info "检查 ADB 连接..."
    adb kill-server 2>/dev/null || true
    sleep 2
    adb connect "$ADB_ADDR" 2>&1 | tee -a "$LOG_FILE"
    sleep 3

    local status
    status=$(adb $ADB_DEVICE get-state 2>/dev/null || echo "unknown")
    debug "ADB state: $status"

    if [[ "$status" == "device" ]]; then
        info "ADB OK: $ADB_ADDR"
        return 0
    fi

    restart_android_adbd_tcp
    adb kill-server 2>/dev/null || true
    sleep 1
    adb connect "$ADB_ADDR" 2>&1 | tee -a "$LOG_FILE"
    sleep 2
    status=$(adb $ADB_DEVICE get-state 2>/dev/null || echo "unknown")
    debug "ADB state after adbd recovery: $status"

    if [[ "$status" == "device" ]]; then
        info "ADB OK after adbd recovery: $ADB_ADDR"
        return 0
    fi

    error "ADB FAIL state=$status"
    return 1
}

# === 游戏控制 ===
wake_screen() {
    info "唤醒屏幕..."
    local wakefulness
    wakefulness=$(adb $ADB_DEVICE shell dumpsys power 2>/dev/null | grep "mWakefulness=" | head -1 || echo "")
    debug "Screen: $wakefulness"

    if echo "$wakefulness" | grep -qi "Asleep\|Dozing"; then
        info "屏幕关闭，唤醒中..."
        adb $ADB_DEVICE shell input keyevent KEYCODE_WAKEUP
        sleep 2
        adb $ADB_DEVICE shell wm dismiss-keyguard >/dev/null 2>&1 || true
    else
        info "屏幕已亮"
        adb $ADB_DEVICE shell wm dismiss-keyguard >/dev/null 2>&1 || true
    fi
}

startup_overlay_watchdog_loop() {
    local label="${1:-startup}"
    local resumed focus dismiss_count=0

    info "启动系统遮挡 watcher: $label"
    while true; do
        resumed=$(timeout 8s adb $ADB_DEVICE shell dumpsys activity activities 2>/dev/null \
            | grep -E 'mResumedActivity|topResumedActivity|ResumedActivity' \
            | head -n 8 || true)

        if printf '%s\n' "$resumed" | grep -q "$AK_PACKAGE"; then
            focus=$(timeout 8s adb $ADB_DEVICE shell dumpsys window windows 2>/dev/null \
                | grep -E 'mCurrentFocus|mFocusedApp|mInputMethodTarget' \
                | head -n 12 || true)

            if printf '%s\n' "$focus" | grep -q 'com.android.systemui'; then
                dismiss_count=$((dismiss_count + 1))
                warn "检测到系统弹窗遮挡 StartUp，尝试关闭: label=$label count=$dismiss_count focus=$(printf '%s' "$focus" | tr '\r\n' ' ' | cut -c1-260)"
                timeout 4s adb $ADB_DEVICE shell input keyevent KEYCODE_BACK >/dev/null 2>&1 || true
                sleep 3
                continue
            fi
        fi

        sleep 2
    done
}

start_startup_overlay_watchdog() {
    local label="${1:-startup}"

    stop_startup_overlay_watchdog
    startup_overlay_watchdog_loop "$label" &
    STARTUP_OVERLAY_WATCHDOG_PID=$!
    debug "系统遮挡 watcher PID=$STARTUP_OVERLAY_WATCHDOG_PID label=$label"
}

stop_startup_overlay_watchdog() {
    local pid="${STARTUP_OVERLAY_WATCHDOG_PID:-}"

    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        debug "停止系统遮挡 watcher PID=$pid"
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi

    STARTUP_OVERLAY_WATCHDOG_PID=""
}

backlight_off_keep_awake() {
    # 物理单机背光脚本已剔除，宿主屏幕策略由控制面或 Android 系统托管
    debug "背光控制已跳过 (非单机专用环境)"
}

kill_arknights() {
    info "关闭明日方舟进程..."
    adb $ADB_DEVICE shell am force-stop "$AK_PACKAGE" 2>/dev/null || true
    sleep 2
    info "明日方舟已关闭"
}

check_arknights_installed() {
    if adb $ADB_DEVICE shell pm list packages 2>/dev/null | grep -q "$AK_PACKAGE"; then
        info "明日方舟已安装"
        return 0
    else
        error "明日方舟未安装!"
        return 1
    fi
}

test_screencap() {
    info "测试截图..."
    local ts
    ts=$(date +%s)
    if adb $ADB_DEVICE exec-out screencap -p > /tmp/maa_test_${ts}.png 2>/dev/null; then
        local size
        size=$(wc -c < /tmp/maa_test_${ts}.png 2>/dev/null || echo "0")
        rm -f /tmp/maa_test_${ts}.png
        if [[ "$size" -gt 1000 ]]; then
            info "截图OK (${size} bytes)"
            return 0
        fi
    fi
    error "截图失败"
    return 1
}

now_ms() {
    date +%s%3N
}

probe_raw_screencap_size() {
    timeout 8s adb $ADB_DEVICE exec-out screencap 2>/dev/null | wc -c 2>/dev/null | tr -d ' '
}

probe_raw_screencap_header() {
    timeout 8s adb $ADB_DEVICE exec-out screencap 2>/dev/null |
        python3 -c 'import sys; data = sys.stdin.buffer.read(); sys.exit(1) if len(data) < 8 else print(int.from_bytes(data[0:4], "little"), int.from_bytes(data[4:8], "little"))' 2>/dev/null
}

parse_maa_resolution_width() {
    printf '%s\n' "$MAA_RESOLUTION_HEIGHT"
}

parse_maa_resolution_height() {
    printf '%s\n' "$MAA_RESOLUTION_WIDTH"
}

raw_header_matches_maa_resolution() {
    local header="${1:-}"
    local width height expected_width expected_height

    if [[ -z "$header" ]]; then
        header=$(probe_raw_screencap_header 2>/dev/null || true)
    fi

    read -r width height _ <<< "$header"
    expected_width=$(parse_maa_resolution_width)
    expected_height=$(parse_maa_resolution_height)

    if [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ ]] &&
       (( width == expected_width && height == expected_height )); then
        return 0
    fi

    warn "RAW_HEADER_MISMATCH: raw_header=${width:-unknown}x${height:-unknown} expected=${expected_width}x${expected_height}"
    return 1
}

arknights_activity_resumed() {
    timeout 8s adb $ADB_DEVICE shell dumpsys activity activities 2>/dev/null \
        | grep -E 'mResumedActivity|topResumedActivity|ResumedActivity' \
        | grep -q "$AK_PACKAGE"
}

prewarm_landscape_surface() {
    local i raw_header stable_count=0
    export MAA_PREWARM_TOKEN=""

    if [[ "${RAW_HEADER_MISMATCHED:-0}" -ne 1 ]]; then
        return 0
    fi

    warn "LANDSCAPE_PREWARM: raw header 未对齐 MAA 横屏尺寸，先受控拉起游戏以等待 Surface 横屏"
    adb $ADB_DEVICE shell am start -n "$AK_PACKAGE"/com.u8.sdk.U8UnityContext >/dev/null 2>&1 || \
        warn "LANDSCAPE_PREWARM: am start 明日方舟未确认成功，继续等待 header 观测"

    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        sleep 2
        raw_header=$(probe_raw_screencap_header 2>/dev/null || true)

        if arknights_activity_resumed && raw_header_matches_maa_resolution "$raw_header"; then
            stable_count=$((stable_count + 1))
            info "LANDSCAPE_PREWARM: raw header 已对齐 MAA 横屏尺寸 ${stable_count}/2 raw_header=${raw_header:-unknown}"
            if (( stable_count >= 2 )); then
                RAW_HEADER_MISMATCHED=0
                export MAA_PREWARM_TOKEN="$$-$(date +%s%N)"
                info "LANDSCAPE_PREWARM: 游戏 Surface 横屏稳定，允许本轮使用 raw/gzip 截图通道"
                return 0
            fi
        else
            stable_count=0
            debug "LANDSCAPE_PREWARM: 等待横屏 Surface $i/15 raw_header=${raw_header:-unknown}"
        fi
    done

    RAW_HEADER_MISMATCHED=1
    warn "LANDSCAPE_PREWARM: 等待游戏 Surface 横屏超时，本轮保留 Encode 降级"
    return 1
}

validate_raw_size() {
    local size="${1:-0}"
    [[ "$size" =~ ^[0-9]+$ ]] &&
        (( size >= RAW_SCREENCAP_MIN_SIZE && size <= RAW_SCREENCAP_MAX_SIZE ))
}

mem_available_kb() {
    awk '/MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null || echo 0
}

swap_used_kb() {
    awk '/SwapTotal:/{total=$2} /SwapFree:/{free=$2} END{if (total=="") total=0; if (free=="") free=0; print total-free}' /proc/meminfo 2>/dev/null || echo 0
}

swap_total_kb() {
    awk '/SwapTotal:/{total=$2} END{if (total=="") total=0; print total}' /proc/meminfo 2>/dev/null || echo 0
}

memory_status_line() {
    local avail swap_used load
    avail=$(mem_available_kb)
    swap_used=$(swap_used_kb)
    load=$(cat /proc/loadavg 2>/dev/null || echo unknown)
    printf 'MemAvailable=%sKB SwapUsed=%sKB Loadavg=%s' "$avail" "$swap_used" "$load"
}

memory_pressure_is_high() {
    local avail swap_used swap_total
    avail=$(mem_available_kb)
    swap_used=$(swap_used_kb)
    swap_total=$(swap_total_kb)
    [[ "$avail" =~ ^[0-9]+$ ]] || avail=0
    [[ "$swap_used" =~ ^[0-9]+$ ]] || swap_used=0
    [[ "$swap_total" =~ ^[0-9]+$ ]] || swap_total=0
    if (( avail < MIN_MEM_AVAILABLE_KB )); then
        return 0
    fi
    if (( avail < 3145728 && swap_used > MAX_SWAP_USED_KB && swap_total > 0 && swap_used * 100 >= swap_total * 85 )); then
        return 0
    fi
    return 1
}

runtime_memory_pressure_is_high() {
    local avail
    avail=$(mem_available_kb)
    [[ "$avail" =~ ^[0-9]+$ ]] || avail=0
    (( avail < RUNTIME_MIN_MEM_AVAILABLE_KB ))
}

bench_raw_with_gzip_once() {
    local out_file="$1"
    timeout 12s bash -c "adb $ADB_DEVICE exec-out 'screencap 2>/dev/null | gzip -1' | gzip -dc > '$out_file'" >/dev/null 2>&1
}

bench_encode_once() {
    local out_file="$1"
    timeout 12s adb $ADB_DEVICE exec-out "screencap -p 2>/dev/null" > "$out_file" 2>/dev/null
}

bench_raw_by_nc_once() {
    local out_file="$1"
    local port="$2"
    rm -f "$out_file"
    timeout 10s python3 - "$port" "$out_file" <<'PY' &
import socket
import sys

port = int(sys.argv[1])
out_path = sys.argv[2]
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    conn, _ = server.accept()
    with conn, open(out_path, "wb") as out:
        while True:
            chunk = conn.recv(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
PY
    local server_pid=$!
    sleep 0.2
    timeout 8s adb $ADB_DEVICE exec-out "screencap 2>/dev/null | nc -w 3 127.0.0.1 $port" >/dev/null 2>&1
    local cmd_ec=$?
    wait "$server_pid" 2>/dev/null || true
    return "$cmd_ec"
}

bench_screencap_method() {
    local method="$1"
    local samples="${2:-$SCREENCAP_BENCH_SAMPLES}"
    local i out_file start end elapsed size ec
    local ok=0 fail=0 total=0 min=999999 max=0 last_size=0
    local base_port=$(( 27100 + ($$ % 1000) ))

    info "截图基准 ${method}: samples=${samples}"
    for (( i=1; i<=samples; i++ )); do
        out_file="/tmp/maa-screencap-${method}.$$.$i"
        start=$(now_ms)
        case "$method" in
            raw_by_nc)
                bench_raw_by_nc_once "$out_file" $(( base_port + i ))
                ec=$?
                ;;
            raw_with_gzip)
                bench_raw_with_gzip_once "$out_file"
                ec=$?
                ;;
            encode)
                bench_encode_once "$out_file"
                ec=$?
                ;;
            *)
                return 2
                ;;
        esac
        end=$(now_ms)
        elapsed=$(( end - start ))
        size=$(wc -c < "$out_file" 2>/dev/null || echo "0")
        rm -f "$out_file"
        last_size="$size"

        if [[ "$ec" -eq 0 ]]; then
            if [[ "$method" == "encode" ]]; then
                if [[ "$size" =~ ^[0-9]+$ ]] && (( size > 1000 )); then
                    ok=$((ok + 1))
                else
                    fail=$((fail + 1))
                fi
            elif validate_raw_size "$size"; then
                ok=$((ok + 1))
            else
                fail=$((fail + 1))
                warn "截图基准 ${method} 第 ${i} 次 raw 大小异常: size=${size} expected=${RAW_SCREENCAP_MIN_SIZE}-${RAW_SCREENCAP_MAX_SIZE}"
            fi
        else
            fail=$((fail + 1))
            warn "截图基准 ${method} 第 ${i} 次命令失败: exit=${ec} size=${size}"
        fi

        total=$((total + elapsed))
        (( elapsed < min )) && min="$elapsed"
        (( elapsed > max )) && max="$elapsed"
        debug "截图基准 ${method} ${i}/${samples}: elapsed=${elapsed}ms size=${size} exit=${ec}"
        sleep 0.2
    done

    local avg=$(( total / samples ))
    info "截图基准结果: method=${method} ok=${ok}/${samples} fail=${fail} min=${min}ms avg=${avg}ms max=${max}ms last_size=${last_size}"
    if [[ -n "${SCREENCAP_BENCH_RESULT_FILE:-}" ]]; then
        printf '%s %s %s %s %s %s %s\n' "$method" "$ok" "$fail" "$min" "$avg" "$max" "$last_size" >> "$SCREENCAP_BENCH_RESULT_FILE"
    else
        printf '%s %s %s %s %s %s %s\n' "$method" "$ok" "$fail" "$min" "$avg" "$max" "$last_size"
    fi
}

benchmark_screencap_methods() {
    local result_file="/tmp/maa-screencap-benchmark.$$"
    local method ok fail min avg max size
    local best_method="" best_avg=999999 best_max=999999 best_min=0 best_size=0
    local raw_nc_method="" raw_nc_avg=999999 raw_nc_max=999999 raw_nc_min=0 raw_nc_size=0
    local raw_header
    : > "$result_file"
    SCREENCAP_BENCH_RESULT_FILE="$result_file"

    raw_header=$(probe_raw_screencap_header 2>/dev/null || true)
    if raw_header_matches_maa_resolution "$raw_header"; then
        if [[ "$RAW_BY_NC_BLACKLISTED" -eq 0 ]]; then
            bench_screencap_method raw_by_nc "$SCREENCAP_BENCH_SAMPLES"
        else
            warn "RawByNc 已在本轮任务中熔断，跳过基准"
        fi
        bench_screencap_method raw_with_gzip "$SCREENCAP_BENCH_SAMPLES"
    else
        warn "RAW_HEADER_MISMATCH: raw header 与 MAA 横屏尺寸不一致，跳过 raw_by_nc/raw_with_gzip 基准"
    fi
    bench_screencap_method encode "$SCREENCAP_BENCH_SAMPLES"
    SCREENCAP_BENCH_RESULT_FILE=""

    while read -r method ok fail min avg max size; do
        [[ -z "$method" ]] && continue
        if [[ "$ok" =~ ^[0-9]+$ && "$fail" =~ ^[0-9]+$ && "$avg" =~ ^[0-9]+$ && "$max" =~ ^[0-9]+$ ]] &&
           (( ok == SCREENCAP_BENCH_SAMPLES && fail == 0 )); then
            if [[ "$method" == "raw_by_nc" ]]; then
                raw_nc_method="$method"
                raw_nc_avg="$avg"
                raw_nc_max="$max"
                raw_nc_min="$min"
                raw_nc_size="$size"
            fi
            if (( avg < best_avg )); then
                best_method="$method"
                best_avg="$avg"
                best_max="$max"
                best_min="$min"
                best_size="$size"
            fi
        fi
    done < "$result_file"

    rm -f "$result_file"

    if [[ -z "$best_method" ]]; then
        error "没有稳定的截图通道，停止任务以避免盲跑"
        return 1
    fi

    if [[ -n "$raw_nc_method" ]] &&
       (( raw_nc_avg <= SCREENCAP_WARN_AVG_MS && raw_nc_max <= SCREENCAP_WARN_MAX_MS )); then
        best_method="$raw_nc_method"
        best_avg="$raw_nc_avg"
        best_max="$raw_nc_max"
        best_min="$raw_nc_min"
        best_size="$raw_nc_size"
        info "RawByNc 基准稳定且低于阈值，优先选择低 CPU 截图通道"
    fi

    CURRENT_SCREENCAP_METHOD="$best_method"
    printf '%s\n' "$best_method" > "$SCREENCAP_METHOD_STATE"
    info "截图通道选择: method=${best_method} min=${best_min}ms avg=${best_avg}ms max=${best_max}ms size=${best_size}"

    if (( best_avg > SCREENCAP_WARN_AVG_MS || best_max > SCREENCAP_WARN_MAX_MS )); then
        error "截图性能异常: method=${best_method} avg=${best_avg}ms max=${best_max}ms threshold_avg=${SCREENCAP_WARN_AVG_MS}ms threshold_max=${SCREENCAP_WARN_MAX_MS}ms"
        return 1
    fi

    apply_resource_patch "$best_method"
}

display_geometry_is_bad() {
    local wm_size override raw_size
    wm_size=$(adb $ADB_DEVICE shell wm size 2>/dev/null || true)
    override=$(printf '%s\n' "$wm_size" | awk '/Override/{print $3; exit}')
    raw_size=$(probe_raw_screencap_size)
    raw_size=${raw_size:-0}

    info "截图故障诊断: wm=$(printf '%s' "$wm_size" | tr '\n' ';') raw=${raw_size} expected=${RAW_SCREENCAP_MIN_SIZE}-${RAW_SCREENCAP_MAX_SIZE}"

    if [[ "$override" != "$MAA_RESOLUTION" ]]; then
        warn "分辨率 override 异常: ${override:-none}"
        return 0
    fi

    if [[ "$raw_size" =~ ^[0-9]+$ ]] && (( raw_size < RAW_SCREENCAP_MIN_SIZE || raw_size > RAW_SCREENCAP_MAX_SIZE )); then
        warn "raw 截图尺寸异常: ${raw_size}"
        return 0
    fi

    return 1
}

verify_raw_geometry_before_run() {
    local i raw_size raw_header wm_size override ok_count=0

    info "预检 raw 截图几何..."
    for i in 1 2 3 4 5; do
        wm_size=$(adb $ADB_DEVICE shell wm size 2>/dev/null || true)
        override=$(printf '%s\n' "$wm_size" | awk '/Override/{print $3; exit}')
        raw_size=$(probe_raw_screencap_size)
        raw_size=${raw_size:-0}
        raw_header=$(probe_raw_screencap_header 2>/dev/null || true)

        info "raw 预检 $i/5: wm=$(printf '%s' "$wm_size" | tr '\n' ';') raw=${raw_size} raw_header=${raw_header:-unknown}"

        if [[ "$override" == "$MAA_RESOLUTION" ]] && [[ "$raw_size" =~ ^[0-9]+$ ]] &&
           (( raw_size >= RAW_SCREENCAP_MIN_SIZE && raw_size <= RAW_SCREENCAP_MAX_SIZE )) &&
           raw_header_matches_maa_resolution "$raw_header"; then
            ok_count=$((ok_count + 1))
            if (( ok_count >= 3 )); then
                info "raw 截图几何连续稳定，允许启动 MAA"
                return 0
            fi
        else
            ok_count=0
            warn "raw 截图几何或分辨率尚未稳定，重新等待"
            wake_screen
            set_maa_resolution
            backlight_off_keep_awake
        fi

        sleep 2
    done

    error "raw 截图几何预检失败"
    return 1
}

verify_startup_preconditions() {
    local i raw_size raw_header wm_size override

    info "预检启动环境..."
    RAW_HEADER_MISMATCHED=0
    for i in 1 2 3; do
        wm_size=$(adb $ADB_DEVICE shell wm size 2>/dev/null || true)
        override=$(printf '%s\n' "$wm_size" | awk '/Override/{print $3; exit}')
        raw_size=$(probe_raw_screencap_size)
        raw_size=${raw_size:-0}
        raw_header=$(probe_raw_screencap_header 2>/dev/null || true)

        info "启动预检 $i/3: wm=$(printf '%s' "$wm_size" | tr '\n' ';') raw=${raw_size} raw_header=${raw_header:-unknown}"

        if [[ "$override" == "$MAA_RESOLUTION" ]]; then
            if [[ "$raw_size" =~ ^[0-9]+$ ]] &&
               (( raw_size < RAW_SCREENCAP_MIN_SIZE || raw_size > RAW_SCREENCAP_MAX_SIZE )); then
                warn "启动前 raw 截图尺寸未处于游戏横屏尺寸: size=${raw_size} expected=${RAW_SCREENCAP_MIN_SIZE}-${RAW_SCREENCAP_MAX_SIZE}; 不阻断 MAA StartUp"
            fi
            if ! raw_header_matches_maa_resolution "$raw_header"; then
                RAW_HEADER_MISMATCHED=1
                warn "RAW_HEADER_MISMATCH: 本轮启动前禁用 raw 截图通道，避免 MaaCore 连接阶段回退不透明"
            fi
            return 0
        fi

        warn "启动前分辨率 override 尚未稳定，重新设置"
        wake_screen
        set_maa_resolution
        backlight_off_keep_awake
        sleep 2
    done

    error "启动前分辨率 override 预检失败"
    return 1
}

classify_startup_screen() {
    local png="$1"
    local size=0

    if [[ -s "$png" ]]; then
        size=$(wc -c < "$png" 2>/dev/null | tr -d ' ' || echo 0)
    fi

    if [[ "$size" =~ ^[0-9]+$ ]] && (( size >= 250000 )); then
        printf 'game_screen'
    elif [[ "$size" =~ ^[0-9]+$ ]] && (( size > 1000 )); then
        printf 'logo_or_static'
    else
        printf 'unknown'
    fi
}

classify_startup_failure_reason() {
    local maa_log="$1"
    local logcat_file="$2"
    local exit_code="${3:-1}"
    local asst_log="${4:-$ASST_LOG_DIR/asst.log}"
    local reason

    if grep -q "StartUp Error" "$maa_log" 2>/dev/null; then
        reason="STARTUP_ERROR"
    elif [[ "$exit_code" -eq 124 || "$exit_code" -eq 137 ]]; then
        reason="STARTUP_TIMEOUT"
    else
        reason="STARTUP_EXIT_${exit_code}"
    fi

    if grep -qE '服务器停机维护公告|开启服务器停机维护|维护期间玩家将无法登录游戏' "$maa_log" 2>/dev/null ||
       { [[ -s "$asst_log" ]] && tail -n 600 "$asst_log" 2>/dev/null | grep -qE '服务器停机维护公告|开启服务器停机维护|维护期间玩家将无法登录游戏'; }; then
        printf '%s_SERVER_MAINTENANCE' "$reason"
        return 0
    fi

    if grep -qE 'USB用于|USB 用于|仅充电|传输文件|AndroidAuto|Android Auto' "$maa_log" "$logcat_file" 2>/dev/null; then
        reason="${reason}_USB_DIALOG"
    fi
    if grep -qE 'Timeout .*Unity|ANR_LOG' "$logcat_file" 2>/dev/null; then
        reason="${reason}_UNITY"
    fi
    if grep -qE 'Process com\.hypergryph\.arknights:GP7(Service|Worker).*has died|ace\.gshell|GP7(Service|Worker)' "$logcat_file" 2>/dev/null; then
        reason="${reason}_GP7"
    fi
    if grep -qE 'fps is: 0\.000000|current fps is: 0' "$logcat_file" 2>/dev/null; then
        reason="${reason}_FPS_ZERO"
    fi

    printf '%s' "$reason"
}

is_startup_render_stall_failure() {
    local reason="$1"
    local screen="$2"

    [[ "$screen" == "logo_or_static" ]] || return 1
    [[ "$reason" == STARTUP_ERROR* ]] || return 1
    [[ "$reason" == *FPS_ZERO* ]] || return 1
    [[ "$reason" == *UNITY* || "$reason" == *GP7* ]] || return 1
    return 0
}

record_startup_failure_state() {
    local reason="$1"
    local screen="$2"
    local detail="$3"

    if [[ "$reason" == *SERVER_MAINTENANCE* ]]; then
        set_failure_reason "SERVER_MAINTENANCE"
        warn "启动失败分型命中服务器停机维护公告: reason=$reason screen=$screen"
    elif is_startup_render_stall_failure "$reason" "$screen"; then
        set_failure_reason "STARTUP_RENDER_STALL"
        warn "启动失败分型命中纯 Logo/Unity 渲染停滞: reason=$reason screen=$screen"
    else
        set_failure_reason "STARTUP_HEALTH_FAILED"
    fi

    write_startup_health_state "failed" "$reason" "$screen" "$detail"
}

capture_task_startup_failure_state() {
    local ts screen_file logcat_file logcat_since reason screen detail started_for_logcat

    ts=$(date +%Y%m%d-%H%M%S)
    screen_file="$LOG_DIR/startup-failure-$ts.png"
    logcat_file="$LOG_DIR/startup-failure-$ts.logcat"
    STARTUP_HEALTH_LOG_FILE="$LOG_FILE"
    STARTUP_HEALTH_SCREENSHOT="$screen_file"
    STARTUP_HEALTH_LOGCAT_FILE="$logcat_file"

    timeout 15s adb $ADB_DEVICE exec-out screencap -p > "$screen_file" 2>/dev/null || true

    started_for_logcat="$TASK_STARTED_AT"
    if [[ ! "$started_for_logcat" =~ ^[0-9]+$ || "$started_for_logcat" -le 0 ]]; then
        started_for_logcat=$(date +%s)
    fi
    logcat_since=$(date -d "@$(( started_for_logcat > 120 ? started_for_logcat - 120 : started_for_logcat ))" '+%m-%d %H:%M:%S.000' 2>/dev/null || true)
    if [[ -n "$logcat_since" ]]; then
        timeout 25s adb $ADB_DEVICE logcat -d -T "$logcat_since" 2>/dev/null \
            | grep -Ei 'hypergryph|arknights|Unity|GP7|ace\.gshell|AndroidRuntime|FATAL|ANR|crash|tombstone|lowmemorykiller|ActivityManager|ActivityTaskManager|InputDispatcher|Network|OkHttp|SSL|DNS|RefClass|InvocationTargetException|oiface|fps|chromium|MaaTouch|fingerprint|systemui|USB|仅充电|传输文件' \
            | tail -n 2000 > "$logcat_file" 2>/dev/null || true
    else
        timeout 25s adb $ADB_DEVICE logcat -d 2>/dev/null \
            | grep -Ei 'hypergryph|arknights|Unity|GP7|ace\.gshell|AndroidRuntime|FATAL|ANR|crash|tombstone|lowmemorykiller|ActivityManager|ActivityTaskManager|InputDispatcher|Network|OkHttp|SSL|DNS|RefClass|InvocationTargetException|oiface|fps|chromium|MaaTouch|fingerprint|systemui|USB|仅充电|传输文件' \
            | tail -n 2000 > "$logcat_file" 2>/dev/null || true
    fi

    screen=$(classify_startup_screen "$screen_file")
    reason=$(classify_startup_failure_reason "$LOG_FILE" "$logcat_file" 1)
    detail="daily task startup phase failed; screenshot=$screen_file logcat=$logcat_file"
    record_startup_failure_state "$reason" "$screen" "$detail"
}

request_reboot_after_startup_render_stall() {
    if [[ "${FAILURE_REASON:-UNSET}" != "STARTUP_RENDER_STALL" ]]; then
        return 0
    fi
    if [[ "$STARTUP_HEALTH_ONLY" -eq 1 || "$BENCH_ONLY" -eq 1 ]]; then
        return 0
    fi

    warn "已保存失败现场并记录状态；STARTUP_RENDER_STALL 异常（严禁整机重启，交由控制面或人工介入）"
    cleanup
}

prepare_run_session() {
    local original_config session_parent
    original_config="$(maa dir config)" || return 1
    session_parent=$(mktemp -d /tmp/maa-run-session.XXXXXXXX) || return 1
    # Keep the tiny per-run evidence until reboot; never copy the general cache.
    if ! timeout -k 5s 150s python3 "$RUN_SESSION_SCRIPT" prepare \
        --session "$session_parent/snapshot" --config-dir "$original_config" 2>&1 | tee -a "$LOG_FILE"; then
        error "RUN_SESSION: 热更新未完成，尚未开始游戏任务"
        return 1
    fi
    export MAA_RUN_SESSION="$session_parent/snapshot"
    info "RUN_SESSION: 本轮资源已固定 $MAA_RUN_SESSION"
}

run_startup_health_check() {
    local ts test_dir task_file health_log health_png health_logcat logcat_since ec reason screen detail
    ts=$(date +%Y%m%d-%H%M%S)
    test_dir="/tmp/maa-startup-health-$ts"
    task_file="$test_dir/tasks/startup_only.toml"
    health_log="$LOG_DIR/startup-health-$ts.log"
    health_png="$LOG_DIR/startup-health-$ts.png"
    health_logcat="$LOG_DIR/startup-health-$ts.logcat"
    STARTUP_HEALTH_LOG_FILE="$health_log"
    STARTUP_HEALTH_SCREENSHOT="$health_png"
    STARTUP_HEALTH_LOGCAT_FILE="$health_logcat"

    write_startup_health_state "running" "INIT" "unknown" "preparing startup-only check"
    info "启动健康检查: 只运行 StartUp，日志=$health_log"

    if ! ensure_adb; then
        set_failure_reason "ADB_PRECHECK_FAILED"
        write_startup_health_state "failed" "ADB_PRECHECK_FAILED" "unknown" "adb is not ready"
        return 1
    fi

    if ! check_arknights_installed; then
        set_failure_reason "ARKNIGHTS_NOT_INSTALLED"
        write_startup_health_state "failed" "ARKNIGHTS_NOT_INSTALLED" "unknown" "package missing"
        return 1
    fi

    kill_arknights
    wake_screen
    backlight_off_keep_awake
    lock_rotation
    set_maa_resolution
    wake_screen

    if ! verify_startup_preconditions; then
        set_failure_reason "DISPLAY_OVERRIDE_PRECHECK_FAILED"
        write_startup_health_state "failed" "DISPLAY_OVERRIDE_PRECHECK_FAILED" "unknown" "wm override did not stabilize"
        return 1
    fi
    prewarm_landscape_surface || true
    if [[ "${RAW_HEADER_MISMATCHED:-0}" -eq 1 ]]; then
        warn "RAW_HEADER_MISMATCH: 启动健康检查预热后仍未对齐横屏 raw header，本轮使用 Encode"
        apply_resource_patch encode
    else
        apply_resource_patch raw_with_gzip
        info "启动健康检查 raw header 已对齐，本轮允许 MaaCore 测试 raw/gzip 截图通道"
    fi

    mkdir -p "$test_dir/tasks" "$test_dir/profiles" "$LOG_DIR" 2>/dev/null || true
    cp -a "$MAA_CONFIG_DIR/cli.toml" "$test_dir/" 2>/dev/null || true
    cp -a "$MAA_CONFIG_DIR/profiles/default.toml" "$test_dir/profiles/default.toml" 2>/dev/null || true
    printf "%s\n" \
        "[[tasks]]" \
        "name = \"开始唤醒\"" \
        "type = \"StartUp\"" \
        "params = { client_type = \"Official\", start_game_enabled = true }" > "$task_file"

    logcat_since=$(date "+%m-%d %H:%M:%S.000")
    info "启动健康检查 logcat 起点: $logcat_since"
    write_startup_health_state "running" "STARTUP_RUNNING" "unknown" "maa startup-only task running"

    start_startup_overlay_watchdog "startup-health"
    timeout -k 10s "$STARTUP_HEALTH_TIMEOUT" env MAA_LOG=debug python3 "$RUN_SESSION_SCRIPT" exec \
        --session "$MAA_RUN_SESSION" --config-dir "$test_dir" -- run startup_only --batch --no-summary > "$health_log" 2>&1
    ec=$?
    stop_startup_overlay_watchdog

    timeout 15s adb $ADB_DEVICE exec-out screencap -p > "$health_png" 2>/dev/null || true
    timeout 25s adb $ADB_DEVICE logcat -d -T "$logcat_since" 2>/dev/null \
        | grep -Ei 'hypergryph|arknights|Unity|GP7|ace\.gshell|RefClass|InvocationTargetException|AndroidRuntime|FATAL|ANR|crash|tombstone|oiface|fps|chromium|MaaTouch|ActivityManager|ActivityTaskManager' \
        | tail -n 1500 > "$health_logcat" 2>/dev/null || true

    screen=$(classify_startup_screen "$health_png")
    if [[ "$ec" -eq 0 ]] && grep -q "StartUp Completed" "$health_log" 2>/dev/null; then
        reason="STARTUP_COMPLETED"
        detail="StartUp completed"
        info "启动健康检查通过"
        write_startup_health_state "ok" "$reason" "$screen" "$detail"
        rm -rf "$test_dir" 2>/dev/null || true
        return 0
    fi

    reason=$(classify_startup_failure_reason "$health_log" "$health_logcat" "$ec")
    detail="exit=$ec log=$health_log logcat=$health_logcat"
    error "启动健康检查失败: reason=$reason screen=$screen exit=$ec"
    record_startup_failure_state "$reason" "$screen" "$detail"
    tail -n 120 "$health_log" >> "$LOG_FILE" 2>/dev/null || true
    rm -rf "$test_dir" 2>/dev/null || true
    return 91
}

# === MAA 执行 ===
cleanup_effective_task_config() {
    if [[ -n "${EFFECTIVE_TASK_FILE:-}" ]]; then
        rm -f "$EFFECTIVE_TASK_FILE" 2>/dev/null || true
    fi
    EFFECTIVE_TASK_FILE=""
    EFFECTIVE_TASK_NAME=""
}

prepare_effective_task_config() {
    local task="$1"
    local attempt="${2:-0}"
    local config_dir task_dir source effective_name dest guarded_source guard_dest

    cleanup_effective_task_config
    EFFECTIVE_TASK_NAME="$task"

    config_dir=$(maa dir config 2>/dev/null || printf '%s' "$MAA_CONFIG_DIR")
    task_dir="$config_dir/tasks"
    source="$task_dir/${task}.toml"

    if (( SCHEDULED_MODE == 1 )); then
        effective_name="${task}-effective-$$-${attempt}"
        dest="$task_dir/${effective_name}.toml"
        if ! run_runtime_command prepare \
            --task-file "$SCHEDULE_CLAIM_TASK_FILE" \
            --output "$dest" \
            --schedule-file "$config_dir/infrast-schedule.json" \
            --infrast-dir "$config_dir/infrast" \
            --ledger "$INFRAST_LEDGER_FILE" \
            --execution-key "$SCHEDULE_EXECUTION_KEY" \
            --attempt-id "$SCHEDULE_ATTEMPT_ID"; then
            rm -f "$dest" 2>/dev/null || true
            error "计划任务 prepare 失败，禁止回退原始 TOML"
            return 1
        fi
        if [[ "$RT_RESULT" != "prepared" || ! -f "$dest" ]] ||
           ! paths_refer_to_same_file "$RT_EFFECTIVE_TASK" "$dest"; then
            rm -f "$dest" 2>/dev/null || true
            error "计划任务 prepare 返回不一致，已终止"
            return 1
        fi
        EFFECTIVE_TASK_NAME="$effective_name"
        EFFECTIVE_TASK_FILE="$dest"
        SCHEDULE_INFRAST_OMITTED="$RT_INFRAST_OMITTED"
        SCHEDULE_OMIT_REASON="$RT_OMIT_REASON"
        SCHEDULE_DOWNSTREAM_COUNTS="$RT_DOWNSTREAM_COUNTS"
        info "基建运行时已生成本轮任务配置: ${effective_name} omitted=$SCHEDULE_INFRAST_OMITTED reason=${SCHEDULE_OMIT_REASON:-none}"
        return 0
    fi

    if [[ ! -f "$source" ]]; then
        debug "未找到任务配置文件，直接运行原任务: $source"
        return 0
    fi
    effective_name="${task}-effective-$$-${attempt}"
    dest="$task_dir/${effective_name}.toml"
    guarded_source="$source"
    guard_dest=""
    if [[ ! -f "$TASK_GUARD_SCRIPT" ]]; then
        warn "活动关卡守门脚本不存在，继续使用原始任务基线: $TASK_GUARD_SCRIPT"
    elif python3 "$TASK_GUARD_SCRIPT" \
        --task-file "$source" \
        --activity-file "$ACTIVITY_STAGE_FILE" \
        --output "$dest" >> "$LOG_FILE" 2>&1; then
        guarded_source="$dest"
        guard_dest="$dest"
        info "活动关卡守门已生成本轮任务基线: ${effective_name}"
    else
        warn "活动关卡守门失败，继续使用原始任务: $task"
        rm -f "$dest" 2>/dev/null || true
        guarded_source="$source"
        guard_dest=""
    fi

    if [[ "$REQUESTED_TASK" == "daily" && -f "$INFRAST_RUNTIME_SCRIPT" ]]; then
        effective_name="${task}-takeover-$$-${attempt}"
        dest="$task_dir/${effective_name}.toml"
        if ! run_runtime_command takeover \
            --task-file "$guarded_source" \
            --schedule-file "$config_dir/infrast-schedule.json" \
            --output "$dest"; then
            rm -f "$dest" "$guard_dest" 2>/dev/null || true
            error "普通日常基建接管检查失败，已禁止回退原始 Infrast"
            return 1
        fi
        case "$RT_RESULT" in
            prepared)
                if [[ ! -f "$dest" ]] ||
                   ! paths_refer_to_same_file "$RT_EFFECTIVE_TASK" "$dest" ||
                   [[ "$RT_INFRAST_OMITTED" != "1" ]]; then
                    rm -f "$dest" "$guard_dest" 2>/dev/null || true
                    error "普通日常基建接管返回不一致"
                    return 1
                fi
                rm -f "$guard_dest" 2>/dev/null || true
                EFFECTIVE_TASK_NAME="$effective_name"
                EFFECTIVE_TASK_FILE="$dest"
                info "MANAGED_INFRAST_SKIPPED: 普通日常已自动跳过受管基建 reason=$RT_OMIT_REASON"
                return 0
                ;;
            not_managed)
                rm -f "$dest" 2>/dev/null || true
                ;;
            *)
                rm -f "$dest" "$guard_dest" 2>/dev/null || true
                error "普通日常基建接管返回未知结果: ${RT_RESULT:-missing}"
                return 1
                ;;
        esac
    fi

    if [[ -n "$guard_dest" ]]; then
        EFFECTIVE_TASK_NAME="${task}-effective-$$-${attempt}"
        EFFECTIVE_TASK_FILE="$guard_dest"
    else
        EFFECTIVE_TASK_NAME="$task"
        EFFECTIVE_TASK_FILE=""
    fi
}

load_scheduled_downstream_counts() {
    local json="$1"
    local output="/tmp/maa-downstream-counts.$$"
    local task count
    if ! python3 - "$json" > "$output" <<'PY'
import json
import re
import sys

counts = json.loads(sys.argv[1])
if not isinstance(counts, dict):
    raise SystemExit(2)
for task, count in sorted(counts.items()):
    if (
        not isinstance(task, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", task)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        raise SystemExit(2)
    print(f"{task}={count}")
PY
    then
        rm -f "$output"
        return 1
    fi
    while IFS='=' read -r task count; do
        [[ -n "$task" && "$count" =~ ^[0-9]+$ ]] || {
            rm -f "$output"
            return 1
        }
        if [[ -n "${SCHEDULE_EXPECTED_COUNTS[$task]+x}" &&
              "${SCHEDULE_EXPECTED_COUNTS[$task]}" != "$count" ]]; then
            rm -f "$output"
            return 1
        fi
        SCHEDULE_EXPECTED_COUNTS[$task]="$count"
        : "${SCHEDULE_COMPLETED_COUNTS[$task]:=0}"
    done < "$output"
    rm -f "$output"
    return 0
}

paths_refer_to_same_file() {
    python3 - "$1" "$2" <<'PY'
import os
import sys
try:
    raise SystemExit(0 if os.path.samefile(sys.argv[1], sys.argv[2]) else 1)
except OSError:
    raise SystemExit(1)
PY
}

scheduled_downstream_complete() {
    local task expected actual
    if (( SCHEDULE_EVENT_AMBIGUOUS != 0 )); then
        return 1
    fi
    for task in "${!SCHEDULE_EXPECTED_COUNTS[@]}"; do
        expected="${SCHEDULE_EXPECTED_COUNTS[$task]}"
        actual="${SCHEDULE_COMPLETED_COUNTS[$task]:-0}"
        if (( actual != expected )); then
            return 1
        fi
    done
    return 0
}

run_closing_scan() {
    local task="$1"

    [[ -f "$CULTIVATION_RUN_SCRIPT" ]] || return 0
    case "$REQUESTED_TASK" in
        operator-scan|cultivation-scan)
            debug "收尾扫描: 扫描任务本身跳过，避免递归"
            return 0
            ;;
    esac
    local config_dir task_file
    config_dir=$(maa dir config 2>/dev/null || printf '%s' "$MAA_CONFIG_DIR")
    task_file="${EFFECTIVE_TASK_FILE:-}"
    if [[ -z "$task_file" || ! -f "$task_file" ]]; then
        task_file="$config_dir/tasks/${task}.toml"
    fi
    if [[ ! -f "$task_file" ]]; then
        debug "收尾扫描: 未找到任务配置，跳过"
        return 0
    fi
    info "收尾扫描: 刷新干员与库存快照（每轮一次，失败不重跑）"
    timeout -k 30s "$CLOSING_SCAN_TIMEOUT" python3 "$CULTIVATION_RUN_SCRIPT" --mode closing-scan \
        --task-file "$task_file" --config-dir "$config_dir" >> "$LOG_FILE" 2>&1 || true
    return 0
}

run_maa_task() {
    local task="$1"
    local attempt=0
    local infrast_seen_started=0
    local infrast_terminal=""
    local persistent_unknown=0

    while (( attempt < MAX_RETRIES )); do
        attempt=$((attempt + 1))
        local attempt_infrast_started=0
        info "======================================="
        info "MAA 任务: $task (尝试 $attempt/$MAX_RETRIES)"
        info "======================================="

        if ! (( SCHEDULED_MODE == 1 && attempt == 1 )) ||
           [[ -z "$EFFECTIVE_TASK_FILE" || ! -f "$EFFECTIVE_TASK_FILE" ]]; then
            if ! prepare_effective_task_config "$task" "$attempt"; then
                set_failure_reason "SCHEDULE_PREPARE_FAILED"
                if (( SCHEDULED_MODE == 1 )); then
                    mark_scheduled_state "unknown" "prepare_failed" || true
                fi
                return 22
            fi
        fi
        if (( SCHEDULED_MODE == 1 )) &&
           ! load_scheduled_downstream_counts "$SCHEDULE_DOWNSTREAM_COUNTS"; then
            set_failure_reason "SCHEDULE_PREPARE_PROTOCOL_FAILED"
            mark_scheduled_state "unknown" "invalid_downstream_counts" || true
            return 22
        fi

        # 每个尝试启动前，都确保 ADB 正常连接
        if ! ensure_adb; then
            error "ADB 连接失败"
            set_failure_reason "ADB_ATTEMPT_FAILED"
            if (( attempt < MAX_RETRIES )); then
                warn "等待 ${RETRY_DELAY}s 后重试..."
                sleep "$RETRY_DELAY"
                continue
            fi
            return 1
        fi

        # 每次尝试都从冷启动开始，避免复用卡在启动 Logo/公告页的游戏进程。
        kill_arknights

        # 唤醒屏幕并设置 16:9 物理分辨率
        wake_screen
        backlight_off_keep_awake
        lock_rotation
        set_maa_resolution

        # 本轮预热成功时只由预热拉起；否则保留 MAA StartUp 正常启动。
        wake_screen
        info "启动责任: 预热成功后 StartUp 不重复拉起；未预热则由 StartUp 启动"
        sleep 2
        backlight_off_keep_awake
        if ! verify_startup_preconditions; then
            set_failure_reason "DISPLAY_OVERRIDE_PRECHECK_FAILED"
            return 1
        fi
        if runtime_memory_pressure_is_high; then
            set_failure_reason "MEMORY_PRESSURE_PRESTART"
            error "启动前内存压力过高，停止本次尝试: $(memory_status_line)"
            return 12
        fi
        prewarm_landscape_surface || true
        if [[ "${RAW_HEADER_MISMATCHED:-0}" -eq 1 ]]; then
            warn "RAW_HEADER_MISMATCH: 启动前 raw header 未对齐 MAA 横屏尺寸，本轮预先切到 Encode 截图通道"
            apply_resource_patch encode
        else
            apply_resource_patch raw_with_gzip
            info "启动前 raw header 已对齐 MAA 横屏尺寸，本轮允许 MaaCore 测试 raw 截图通道"
        fi

        if [[ -n "$DEBUG" ]]; then
            export MAA_LOG=debug
        elif (( SCHEDULED_MODE == 1 )); then
            # Scheduled state tracking depends on maa-cli callback INFO events.
            export MAA_LOG=info
        else
            export MAA_LOG=warn
        fi

        local start_time
        start_time=$(date +%s)

        # 引入硬超时，并在截图连续失败时提前终止本次尝试。
        local fifo="/tmp/maa-run-output.$$.$attempt"
        local fail_count=0
        local aborted=0
        local abort_exit_code=88
        local output_stream_status=0
        local runner_kind="maa-session"
        rm -f "$fifo"
        mkfifo "$fifo"

        start_startup_overlay_watchdog "task-${task}-attempt-${attempt}"

        # 培养目标接管：仅在普通日常（非计划模式）且培养已启用时，用培养运行编排替换原始
        # maa run。仍在同一外层任务锁、同一设备预检与看门狗之下；失败禁止自动重跑。
        local cultivation_active=0
        local cultivation_config_dir=""
        if [[ "$REQUESTED_TASK" == "daily" ]] && (( SCHEDULED_MODE == 0 )) &&
           [[ -f "$CULTIVATION_RUN_SCRIPT" ]]; then
            # 显式传配置目录：与 runner 使用同一份配置，不依赖子进程里的 maa dir config。
            cultivation_config_dir="$(maa dir config 2>/dev/null || printf '%s' "$MAA_CONFIG_DIR")"
            if python3 "$CULTIVATION_RUN_SCRIPT" --mode check \
                --config-dir "$cultivation_config_dir" >/dev/null 2>>"$LOG_FILE"; then
                cultivation_active=1
            fi
        fi

        if (( cultivation_active == 0 )) && [[ -f "$CULTIVATION_RUN_SCRIPT" ]]; then
            # 未接管路径（普通/定时/单任务）运行前保守失效龙门币余额：白名单外任务类型
            # （Recruit/Infrast/Custom/未知）视为可能扣款。接管路径由 runtime 按 phase 处理，
            # 避免一上来全失效导致龙门币本永远跳过。state 不存在时该调用零成本直接返回。
            if [[ -z "$cultivation_config_dir" ]]; then
                cultivation_config_dir="$(maa dir config 2>/dev/null || printf '%s' "$MAA_CONFIG_DIR")"
            fi
            local cultivation_precheck_file="${EFFECTIVE_TASK_FILE:-}"
            if [[ -z "$cultivation_precheck_file" || ! -f "$cultivation_precheck_file" ]]; then
                cultivation_precheck_file="$cultivation_config_dir/tasks/${task}.toml"
            fi
            python3 "$CULTIVATION_RUN_SCRIPT" --mode invalidate-for-task \
                --task-file "$cultivation_precheck_file" --config-dir "$cultivation_config_dir" \
                >>"$LOG_FILE" 2>&1 || true
        fi

        # 复用此前药OCR及已有收尾库存，只改临时TOML，不新增仓库识别或重入runner。
        local medicine_action="disabled"
        local medicine_started_at="$(date --iso-8601=seconds)"
        if [[ "$REQUESTED_TASK" == "daily" ]] && (( SCHEDULED_MODE == 0 && cultivation_active == 0 )) &&
           [[ -f "$MEDICINE_PLAN_SCRIPT" ]]; then
            local medicine_config_dir="$(maa dir config)"
            local medicine_source="${EFFECTIVE_TASK_FILE:-$medicine_config_dir/tasks/${task}.toml}"
            local medicine_name="${task}-medicine-$$-${attempt}"
            local medicine_dest="$medicine_config_dir/tasks/${medicine_name}.toml"
            if ! medicine_action=$(python3 "$MEDICINE_PLAN_SCRIPT" prepare \
                --config-dir "$medicine_config_dir" --source "$medicine_source" --dest "$medicine_dest" 2>>"$LOG_FILE"); then
                error "MEDICINE_POLICY_ERROR: 无法安全生成用药任务，停止本轮"
                set_failure_reason "MEDICINE_POLICY_ERROR"
                rm -f "$medicine_dest"
                return 93
            fi
            if [[ "$medicine_action" != "disabled" ]]; then
                cleanup_effective_task_config
                EFFECTIVE_TASK_FILE="$medicine_dest"
                EFFECTIVE_TASK_NAME="$medicine_name"
                info "MEDICINE_POLICY: action=$medicine_action（自动倍数、仅匹配目标允许集中用药）"
            fi
        fi

        if (( cultivation_active == 1 )); then
            runner_kind="cultivation"
            local cultivation_task_file="${EFFECTIVE_TASK_FILE:-}"
            if [[ -z "$cultivation_task_file" || ! -f "$cultivation_task_file" ]]; then
                cultivation_task_file="$cultivation_config_dir/tasks/${task}.toml"
            fi
            info "CULTIVATION_RUN: 培养目标已启用，本轮由培养运行编排接管 task=${task} 文件=${cultivation_task_file}（失败不自动重跑）"
            timeout -k 30s "$TASK_TIMEOUT" python3 "$CULTIVATION_RUN_SCRIPT" --mode execute \
                --task-file "$cultivation_task_file" --config-dir "$cultivation_config_dir" > "$fifo" 2>&1 &
        else
            timeout -k 30s "$TASK_TIMEOUT" python3 "$RUN_SESSION_SCRIPT" exec \
                --session "$MAA_RUN_SESSION" --config-dir "$(maa dir config)" \
                -- run "$EFFECTIVE_TASK_NAME" --batch > "$fifo" 2>&1 &
        fi
        local runner_pid=$!
        info "RUNNER_START: kind=$runner_kind pid=$runner_pid fifo=$fifo"

        while IFS= read -r line; do
            if ! printf '%s\n' "$line" >> "$LOG_FILE"; then
                output_stream_status=94
            fi
            if ! printf '%s\n' "$line"; then
                output_stream_status=94
            fi
            if (( SCHEDULED_MODE == 1 )); then
                clean_line=$(printf '%s\n' "$line" | sed $'s/\033\\[[0-9;]*[[:alpha:]]//g')
                if [[ "$clean_line" =~ ([A-Za-z][A-Za-z0-9_]*)\ (Start|Completed|Error|Stopped)$ ]]; then
                    event_task="${BASH_REMATCH[1]}"
                    event_state="${BASH_REMATCH[2]}"
                    if [[ "$event_task" == "Infrast" ]]; then
                        case "$event_state" in
                            Start)
                                if (( infrast_seen_started == 1 )); then
                                    SCHEDULE_EVENT_AMBIGUOUS=1
                                else
                                    infrast_seen_started=1
                                    attempt_infrast_started=1
                                    if ! mark_scheduled_state "infrast_started" "maa_callback"; then
                                        SCHEDULE_MARK_FAILED=1
                                        SCHEDULE_NO_INFRAST_RETRY=1
                                        persistent_unknown=1
                                        kill "$runner_pid" 2>/dev/null || true
                                        aborted=1
                                        abort_exit_code=92
                                        break
                                    fi
                                fi
                                ;;
                            Completed)
                                if (( infrast_seen_started != 1 )) || [[ -n "$infrast_terminal" ]]; then
                                    SCHEDULE_EVENT_AMBIGUOUS=1
                                else
                                    infrast_terminal="completed"
                                    if ! mark_scheduled_state "infrast_completed" "maa_callback"; then
                                        SCHEDULE_MARK_FAILED=1
                                        SCHEDULE_NO_INFRAST_RETRY=1
                                        persistent_unknown=1
                                        kill "$runner_pid" 2>/dev/null || true
                                        aborted=1
                                        abort_exit_code=92
                                        break
                                    fi
                                fi
                                ;;
                            Error)
                                if (( infrast_seen_started != 1 )) || [[ -n "$infrast_terminal" ]]; then
                                    SCHEDULE_EVENT_AMBIGUOUS=1
                                else
                                    infrast_terminal="failed"
                                    if ! mark_scheduled_state "infrast_failed" "maa_callback"; then
                                        SCHEDULE_MARK_FAILED=1
                                        SCHEDULE_NO_INFRAST_RETRY=1
                                        persistent_unknown=1
                                        kill "$runner_pid" 2>/dev/null || true
                                        aborted=1
                                        abort_exit_code=92
                                        break
                                    fi
                                fi
                                ;;
                            Stopped)
                                persistent_unknown=1
                                SCHEDULE_NO_INFRAST_RETRY=1
                                mark_scheduled_state "unknown" "infrast_stopped" || SCHEDULE_MARK_FAILED=1
                                ;;
                        esac
                    elif [[ -n "${SCHEDULE_EXPECTED_COUNTS[$event_task]+x}" ]]; then
                        case "$event_state" in
                            Completed)
                                SCHEDULE_COMPLETED_COUNTS[$event_task]=$(( ${SCHEDULE_COMPLETED_COUNTS[$event_task]:-0} + 1 ))
                                if (( SCHEDULE_COMPLETED_COUNTS[$event_task] > SCHEDULE_EXPECTED_COUNTS[$event_task] )); then
                                    SCHEDULE_EVENT_AMBIGUOUS=1
                                fi
                                ;;
                            Error|Stopped)
                                SCHEDULE_DOWNSTREAM_ERROR_COUNT=$((SCHEDULE_DOWNSTREAM_ERROR_COUNT + 1))
                                ;;
                        esac
                    fi
                fi
            fi
            if [[ "$line" == *"StartUp Start"* ]]; then
                write_startup_health_state "running" "STARTUP_RUNNING" "unknown" "daily task startup phase"
            elif [[ "$line" == *"StartUp Completed"* ]]; then
                write_startup_health_state "ok" "STARTUP_COMPLETED" "game_screen" "daily task startup phase completed"
            elif [[ "$line" == *"StartUp Error"* ]]; then
                warn "启动健康门失败: StartUp Error，提前停止本次任务并采集现场"
                capture_task_startup_failure_state
                kill "$runner_pid" 2>/dev/null || true
                sleep 2
                kill -9 "$runner_pid" 2>/dev/null || true
                aborted=1
                abort_exit_code=91
                break
            fi

            if [[ "$line" == *"ScreencapFailed"* ]]; then
                fail_count=$((fail_count + 1))
            else
                fail_count=0
            fi

            if (( fail_count >= SCREENCAP_GEOMETRY_CHECK_AFTER )) && (( fail_count == SCREENCAP_GEOMETRY_CHECK_AFTER || fail_count % 10 == 0 )); then
                if display_geometry_is_bad; then
                    warn "检测到截图尺寸/方向已偏离 16:9，提前终止本次尝试并重新初始化"
                    if [[ "$CURRENT_SCREENCAP_METHOD" == "raw_by_nc" ]]; then
                        RAW_BY_NC_BLACKLISTED=1
                        warn "RawByNc 本轮熔断，后续尝试切回 RawWithGzip"
                        apply_resource_patch raw_with_gzip
                    fi
                    kill "$runner_pid" 2>/dev/null || true
                    sleep 2
                    kill -9 "$runner_pid" 2>/dev/null || true
                    aborted=1
                    break
                fi
            fi

            if (( fail_count >= SCREENCAP_FAIL_LIMIT )); then
                warn "连续 ${fail_count} 次截图失败，提前终止本次尝试并重试"
                if [[ "$CURRENT_SCREENCAP_METHOD" == "raw_by_nc" ]]; then
                    RAW_BY_NC_BLACKLISTED=1
                    warn "RawByNc 本轮熔断，后续尝试切回 RawWithGzip"
                    apply_resource_patch raw_with_gzip
                fi
                kill "$runner_pid" 2>/dev/null || true
                sleep 2
                kill -9 "$runner_pid" 2>/dev/null || true
                aborted=1
                break
            fi
        done < "$fifo"

        wait "$runner_pid"
        local runner_wait_status=$?
        local ec=$runner_wait_status
        local runner_outcome="exit"
        if (( runner_wait_status == 124 )); then
            runner_outcome="timeout"
        elif (( runner_wait_status >= 128 )); then
            runner_outcome="signal_$((runner_wait_status - 128))"
        fi
        info "RUNNER_EXIT: kind=$runner_kind pid=$runner_pid wait_status=$runner_wait_status stream_status=$output_stream_status outcome=$runner_outcome"
        if (( ec == 0 && output_stream_status != 0 )); then
            ec=$output_stream_status
            set_failure_reason "RUNNER_OUTPUT_FAILED"
            error "RUNNER_OUTPUT_FAILED: stream_status=$output_stream_status"
        fi
        stop_startup_overlay_watchdog
        cleanup_effective_task_config
        rm -f "$fifo"

        if (( aborted == 1 )); then
            ec="$abort_exit_code"
        fi

        if [[ "$REQUESTED_TASK" == "daily" ]] && [[ -f "$MEDICINE_PLAN_SCRIPT" ]]; then
            python3 "$MEDICINE_PLAN_SCRIPT" finish --config-dir "$(maa dir config)" \
                --started-at "$medicine_started_at" --exit-code "$ec" >>"$LOG_FILE" 2>&1 || \
                warn "MEDICINE_EVIDENCE_FAILED: 未能更新药识别缓存，下一轮禁止猜测用药"
        fi

        # 每轮正常游戏任务收尾一次：刷新干员与库存快照；失败/断链不补扫、不重跑、不改退出码。
        # 接管路径已在自己的 tail 内完成，这里只处理普通/定时/单任务路径。
        if (( ec == 0 )) && (( cultivation_active == 0 )); then
            run_closing_scan "$task"
        fi

        if (( SCHEDULED_MODE == 1 )); then
            if (( SCHEDULE_MARK_FAILED == 1 )); then
                set_failure_reason "SCHEDULE_MARK_FAILED"
                error "基建状态持久化失败，已启用本轮内存防重并终止"
                return 92
            fi
            if [[ "$SCHEDULE_INFRAST_OMITTED" == "0" ]] &&
               (( attempt_infrast_started == 0 )); then
                SCHEDULE_NO_INFRAST_RETRY=1
                persistent_unknown=1
                if ! mark_scheduled_state "unknown" "required_infrast_callback_missing"; then
                    set_failure_reason "SCHEDULE_MARK_FAILED"
                    error "缺失 Infrast 回调且 unknown 状态无法持久化，安全终止"
                    return 92
                fi
                SCHEDULE_OUTCOME="unknown"
                set_failure_reason "INFRAST_CALLBACK_MISSING"
                error "计划包含 Infrast，但未观察到 Infrast Start；禁止推断成功或自动重试"
                return 93
            fi
            if (( infrast_seen_started == 1 )) && [[ -z "$infrast_terminal" ]]; then
                persistent_unknown=1
                SCHEDULE_NO_INFRAST_RETRY=1
                mark_scheduled_state "unknown" "infrast_terminal_missing" || {
                    set_failure_reason "SCHEDULE_MARK_FAILED"
                    return 92
                }
            fi
            if (( SCHEDULE_EVENT_AMBIGUOUS == 1 )); then
                persistent_unknown=1
                SCHEDULE_NO_INFRAST_RETRY=1
                if ! mark_scheduled_state "unknown" "ambiguous_or_excess_events"; then
                    set_failure_reason "SCHEDULE_MARK_FAILED"
                    return 92
                fi
            fi
            if [[ "$infrast_terminal" == "failed" ]] &&
               [[ "$ec" -eq 1 ]] &&
               (( SCHEDULE_DOWNSTREAM_ERROR_COUNT == 0 )) &&
               scheduled_downstream_complete; then
                if ! mark_scheduled_state "partial_success" "infrast_failed_downstream_completed"; then
                    set_failure_reason "SCHEDULE_MARK_FAILED"
                    return 92
                fi
                PARTIAL_SUCCESS=1
                SCHEDULE_OUTCOME="partial_success"
                info "Infrast 失败，但全部后续任务已完成；本轮按部分成功结束"
                return 0
            fi
            if [[ "$infrast_terminal" == "failed" && "$ec" -ne 1 ]] &&
               (( persistent_unknown == 0 )); then
                persistent_unknown=1
                SCHEDULE_NO_INFRAST_RETRY=1
                if ! mark_scheduled_state "unknown" "abnormal_exit_after_infrast_error_${ec}"; then
                    set_failure_reason "SCHEDULE_MARK_FAILED"
                    return 92
                fi
            fi
            if [[ "$ec" -eq 0 ]]; then
                if (( persistent_unknown == 1 )); then
                    SCHEDULE_OUTCOME="unknown"
                    warn "日常剩余任务完成，但基建结果保持 unknown"
                    return 0
                fi
                if ! scheduled_downstream_complete; then
                    if ! mark_scheduled_state "unknown" "missing_or_excess_downstream_events"; then
                        set_failure_reason "SCHEDULE_MARK_FAILED"
                        return 92
                    fi
                    SCHEDULE_OUTCOME="unknown"
                    warn "MAA 已退出成功，但回调事件不足或含歧义，结果保持 unknown"
                    return 0
                fi
                if [[ "$infrast_terminal" == "failed" ]]; then
                    if ! mark_scheduled_state "unknown" "infrast_failed_incomplete_remainder"; then
                        set_failure_reason "SCHEDULE_MARK_FAILED"
                        return 92
                    fi
                    SCHEDULE_OUTCOME="unknown"
                    return 0
                fi
                if ! mark_scheduled_state "daily_completed" "all_expected_tasks_completed"; then
                    set_failure_reason "SCHEDULE_MARK_FAILED"
                    return 92
                fi
                SCHEDULE_OUTCOME="daily_completed"
            fi
        fi

        if [[ "$ec" -eq 0 ]]; then
            local end_time duration
            end_time=$(date +%s)
            duration=$(( end_time - start_time ))
            info "DONE 任务完成 (${duration}s)"
            # 任务整体成功即证明启动阶段通过；正常 warn 日志级别下 MAA 不输出
            # "StartUp Completed"，仅靠上面的逐行匹配无法刷新 ok 状态，会让
            # startup-health.env / WebUI 长期残留上一次 failed。这里兜底写入 ok。
            write_startup_health_state "ok" "STARTUP_COMPLETED" "game_screen" "daily task completed; startup phase passed"
            return 0
        else
            warn "任务失败 exit=$ec attempt=$attempt/$MAX_RETRIES"
            if [[ "$ec" -eq 91 ]]; then
                error "启动健康门失败，不继续盲目重试"
                return "$ec"
            fi
            if [[ "$FAILURE_REASON" == "UNSET" ]]; then
                set_failure_reason "TASK_EXIT_${ec}"
            fi
            if [[ "$ec" -eq 88 || "$ec" -eq 124 || "$ec" -eq 137 ]]; then
                warn "检测到截图断链、任务超时或被系统杀死，清理 MAA/截图残留进程..."
                kill_stale_processes
            fi
            if (( cultivation_active == 1 )); then
                error "CULTIVATION_RUN_FAILED: 培养运行失败 exit=$ec，按防药/副产重复原则本轮不自动重跑"
                set_failure_reason "CULTIVATION_RUN_FAILED"
                return "$ec"
            fi
            if [[ "$medicine_action" == "drain" ]]; then
                error "MEDICINE_DRAIN_FAILED: 集中用药失败 exit=$ec，本轮禁止自动重跑"
                set_failure_reason "MEDICINE_DRAIN_FAILED"
                return "$ec"
            fi
            if (( attempt < MAX_RETRIES )); then
                warn "清理明日方舟进程，下一次尝试将重新冷启动..."
                kill_arknights
                warn "等待 ${RETRY_DELAY}s 后重试..."
                sleep "$RETRY_DELAY"
            fi
        fi
    done

    if (( SCHEDULED_MODE == 1 )) && [[ -z "$SCHEDULE_OUTCOME" ]]; then
        if ! mark_scheduled_state "unknown" "retries_exhausted"; then
            set_failure_reason "SCHEDULE_MARK_FAILED"
            return 92
        fi
        SCHEDULE_OUTCOME="unknown"
    fi
    error "任务 $task 在 $MAX_RETRIES 次尝试后失败"
    return 1
}

# === 更新 ===
apply_resource_patch() {
    local mode="${1:-raw_with_gzip}"
    if MAA_SCREENCAP_MODE="$mode" python3 "$MAA_HOME/patch_config.py" >> "$LOG_FILE" 2>&1; then
        info "截图配置补丁已应用: mode=${mode}"
    else
        warn "截图配置补丁应用失败: mode=${mode}"
    fi
}

apply_navigation_overlay() {
    local source_dir="${1:-$RESOURCE_CLONE_DIR}"
    if [[ ! -x "$RESOURCE_OVERLAY_SCRIPT" ]]; then
        warn "未找到导航覆盖层脚本，跳过: $RESOURCE_OVERLAY_SCRIPT"
        return 0
    fi

    info "检查官方主仓导航覆盖层..."
    LOG_FILE="$LOG_FILE" RESOURCE_DIR="$RESOURCE_DIR" RESOURCE_SOURCE_DIR="$source_dir" "$RESOURCE_OVERLAY_SCRIPT"
}

sync_resource_from_clone() {
    if [[ ! -d "$RESOURCE_CLONE_DIR" ]]; then
        info "未找到 MaaResource clone 资源目录，跳过 clone 同步: $RESOURCE_CLONE_DIR"
        return 0
    fi

    info "同步 MaaResource clone 到运行资源目录..."
    mkdir -p "$RESOURCE_DIR"
    if cp -a "$RESOURCE_CLONE_DIR"/. "$RESOURCE_DIR"/ >> "$LOG_FILE" 2>&1; then
        if ! apply_navigation_overlay "$RESOURCE_CLONE_DIR"; then
            error "导航覆盖层应用失败"
            return 1
        fi
        info "运行资源目录已同步: $RESOURCE_DIR"
        return 0
    fi

    error "运行资源目录同步失败"
    return 1
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

    info "Git 更新失败，尝试通过官方 tarball 更新 MaaResource..."
    rm -rf "$work_dir" "$tarball"
    mkdir -p "$work_dir"

    if ! timeout -k 30s "$UPDATE_TIMEOUT" curl -L --fail --connect-timeout 20 --max-time 600 -o "$tarball" "$RESOURCE_TARBALL_URL" 2>&1 | tee -a "$LOG_FILE"; then
        rm -rf "$work_dir" "$tarball"
        return 1
    fi

    if ! tar -xzf "$tarball" -C "$work_dir" >> "$LOG_FILE" 2>&1; then
        rm -rf "$work_dir" "$tarball"
        return 1
    fi

    src_dir="$work_dir/MaaResource-main/resource"
    if [[ ! -d "$src_dir" ]]; then
        warn "tarball 中未找到 resource 目录"
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

update_resources_before_run() {
    local attempt=0

    while (( attempt < UPDATE_RETRIES )); do
        attempt=$((attempt + 1))
        info "======================================="
        info "运行前更新 MaaCore 与资源 (channel=$MAA_CORE_CHANNEL, 尝试 $attempt/$UPDATE_RETRIES)"
        info "======================================="

        timeout -k 30s "$UPDATE_TIMEOUT" maa update "$MAA_CORE_CHANNEL" --batch --test-time "$MAA_UPDATE_TEST_TIME" 2>&1 | tee -a "$LOG_FILE"
        local ec=${PIPESTATUS[0]}

        if [[ "$ec" -eq 0 ]]; then
            if ! sync_resource_from_clone; then
                warn "MaaResource clone 同步失败 attempt=$attempt/$UPDATE_RETRIES"
                continue
            fi
            date '+%Y-%m-%d %H:%M:%S' > "$UPDATE_MARKER"
            info "MaaCore 与资源更新完成"
            log_resource_versions
            apply_resource_patch
            return 0
        fi

        warn "MaaCore 与资源更新失败 exit=$ec attempt=$attempt/$UPDATE_RETRIES"
        if update_resource_from_tarball; then
            date '+%Y-%m-%d %H:%M:%S' > "$UPDATE_MARKER"
            info "MaaResource tarball 更新完成"
            log_resource_versions
            apply_resource_patch
            return 0
        fi

        if (( attempt < UPDATE_RETRIES )); then
            warn "等待 ${UPDATE_RETRY_DELAY}s 后重试更新..."
            sleep "$UPDATE_RETRY_DELAY"
        fi
    done

    error "资源更新失败。为避免使用过期资源，本次不运行任务"
    return 1
}

# === 日志清理 ===
cleanup_logs() {
    find "$LOG_DIR" -name "maa-*.log" -mtime +30 -delete 2>/dev/null || true
    find "$LOG_DIR" -path "$LOG_DIR/asst/*" -type f -mtime +14 -delete 2>/dev/null || true
}

rotate_asst_logs() {
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    mkdir -p "$LOG_DIR/asst"
    for f in "$ASST_LOG_DIR/asst.log" "$ASST_LOG_DIR/asst.bak.log"; do
        if [[ -s "$f" ]]; then
            local base
            base=$(basename "$f")
            mv "$f" "$LOG_DIR/asst/${base}.${ts}" 2>/dev/null || true
            info "已轮转 MaaCore 日志: $f -> $LOG_DIR/asst/${base}.${ts}"
        fi
    done
}

collect_failure_snapshot() {
    local task="$1"
    local exit_code="$2"
    local ts safe_task dir archive started_for_logcat logcat_since

    ts=$(date +%Y%m%d-%H%M%S)
    safe_task=$(printf '%s' "$task" | tr -c 'A-Za-z0-9_.-' '_')
    dir="$FAILURE_SNAPSHOT_DIR/${ts}-${safe_task}-exit${exit_code}"
    archive="${dir}.tar.gz"
    FAILURE_SNAPSHOT_ARCHIVE="$archive"

    info "采集失败现场: $dir"
    mkdir -p "$dir/interface" 2>/dev/null || true
    find "$FAILURE_SNAPSHOT_DIR" -maxdepth 1 -type d -mtime +"$FAILURE_SNAPSHOT_KEEP_DAYS" -name '20*-exit*' -exec rm -rf {} + 2>/dev/null || true
    find "$FAILURE_SNAPSHOT_DIR" -maxdepth 1 -type f -mtime +"$FAILURE_SNAPSHOT_KEEP_DAYS" -name '20*-exit*.tar.gz' -delete 2>/dev/null || true

    {
        echo "time=$(date '+%Y-%m-%d %H:%M:%S %Z')"
        echo "task=$task"
        echo "exit_code=$exit_code"
        echo "failure_reason=${FAILURE_REASON:-UNSET}"
        echo "startup_health_status=${STARTUP_HEALTH_STATUS:-unknown}"
        echo "startup_health_reason=${STARTUP_HEALTH_REASON:-unknown}"
        echo "startup_health_screen=${STARTUP_HEALTH_SCREEN:-unknown}"
        echo "log_file=$LOG_FILE"
        echo "task_started_at=$TASK_STARTED_AT"
        echo "screencap_method=${CURRENT_SCREENCAP_METHOD:-unknown}"
        echo "memory=$(memory_status_line)"
        echo "maa_version=$(maa version 2>&1 | tr '\n' ';' | sed 's/;$//')"
        echo "device=$(adb $ADB_DEVICE shell getprop ro.product.model 2>/dev/null | tr -d '\r')"
        echo "android=$(adb $ADB_DEVICE shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')"
    } > "$dir/manifest.txt" 2>&1 || true

    cp "$LOG_FILE" "$dir/maa-run.log" 2>/dev/null || true
    cp "$STARTUP_HEALTH_STATE_FILE" "$dir/startup-health.env" 2>/dev/null || true
    if [[ -n "${STARTUP_HEALTH_LOG_FILE:-}" && -s "$STARTUP_HEALTH_LOG_FILE" ]]; then
        cp "$STARTUP_HEALTH_LOG_FILE" "$dir/startup-health.log" 2>/dev/null || true
    fi
    if [[ -n "${STARTUP_HEALTH_SCREENSHOT:-}" && -s "$STARTUP_HEALTH_SCREENSHOT" ]]; then
        cp "$STARTUP_HEALTH_SCREENSHOT" "$dir/startup-health.png" 2>/dev/null || true
    fi
    if [[ -n "${STARTUP_HEALTH_LOGCAT_FILE:-}" && -s "$STARTUP_HEALTH_LOGCAT_FILE" ]]; then
        cp "$STARTUP_HEALTH_LOGCAT_FILE" "$dir/startup-health.logcat" 2>/dev/null || true
    fi
    tail -n 400 "$LOG_FILE" > "$dir/maa-run-tail.log" 2>/dev/null || true
    tail -n 5000 "$ASST_LOG_DIR/asst.log" > "$dir/asst-tail.log" 2>/dev/null || true
    tail -n 2000 "$ASST_LOG_DIR/asst.bak.log" > "$dir/asst-bak-tail.log" 2>/dev/null || true

    timeout 15s adb $ADB_DEVICE exec-out screencap -p > "$dir/current-screen.png" 2>/dev/null || true
    if [[ -d "$ASST_LOG_DIR/interface" ]]; then
        find "$ASST_LOG_DIR/interface" -maxdepth 1 -type f -name '*.png' -printf '%T@ %p\n' 2>/dev/null \
            | sort -rn \
            | head -n 12 \
            | cut -d' ' -f2- \
            | while IFS= read -r f; do
                cp "$f" "$dir/interface/" 2>/dev/null || true
            done
    fi

    {
        echo "=== date ==="
        date
        echo "=== uptime ==="
        uptime
        echo "=== loadavg ==="
        cat /proc/loadavg 2>/dev/null || true
        echo "=== memory ==="
        free -h 2>/dev/null || true
        cat /proc/meminfo 2>/dev/null | head -n 30 || true
        echo "=== disk ==="
        df -h / "$LOG_DIR" /tmp 2>/dev/null || true
        echo "=== debian top cpu ==="
        ps -eo pid,ppid,stat,pcpu,pmem,comm,args --sort=-pcpu 2>/dev/null | head -n 30 || true
    } > "$dir/debian-system.txt" 2>&1 || true

    {
        echo "=== adb devices ==="
        adb devices 2>/dev/null || true
        echo "=== adb state ==="
        adb $ADB_DEVICE get-state 2>/dev/null || true
        echo "=== props ==="
        adb $ADB_DEVICE shell getprop ro.product.model 2>/dev/null || true
        adb $ADB_DEVICE shell getprop ro.build.version.release 2>/dev/null || true
        adb $ADB_DEVICE shell getprop sys.boot_completed 2>/dev/null || true
        echo "=== wm ==="
        adb $ADB_DEVICE shell wm size 2>/dev/null || true
        adb $ADB_DEVICE shell wm density 2>/dev/null || true
        echo "=== power ==="
        adb $ADB_DEVICE shell dumpsys power 2>/dev/null | grep -E 'mWakefulness=|Display Power|mHolding' | head -n 30 || true
        echo "=== battery ==="
        adb $ADB_DEVICE shell dumpsys battery 2>/dev/null | grep -E 'AC powered|USB powered|status|level|temperature|voltage' || true
        echo "=== activity ==="
        adb $ADB_DEVICE shell dumpsys activity activities 2>/dev/null | grep -E 'mResumedActivity|topResumedActivity|ResumedActivity' | head -n 20 || true
        echo "=== window focus ==="
        adb $ADB_DEVICE shell dumpsys window windows 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp|mInputMethodTarget' | head -n 20 || true
        echo "=== game pids ==="
        adb $ADB_DEVICE shell pidof "$AK_PACKAGE" 2>/dev/null || true
        adb $ADB_DEVICE shell ps -A 2>/dev/null | grep -E 'hypergryph|arknights|GP7|ace.gshell' || true
        echo "=== package ==="
        adb $ADB_DEVICE shell dumpsys package "$AK_PACKAGE" 2>/dev/null | grep -E 'versionCode|versionName|firstInstallTime|lastUpdateTime|userId=|granted=|READ_EXTERNAL_STORAGE|WRITE_EXTERNAL_STORAGE|POST_NOTIFICATIONS' | head -n 120 || true
        echo "=== android storage ==="
        adb $ADB_DEVICE shell df -h /data /sdcard 2>/dev/null || true
        echo "=== android network ==="
        adb $ADB_DEVICE shell settings get global http_proxy 2>/dev/null || true
        timeout 8s adb $ADB_DEVICE shell ping -c 1 -W 2 ak.hypergryph.com 2>/dev/null || true
        timeout 12s adb $ADB_DEVICE shell curl -I --max-time 8 https://ak.hypergryph.com/downloads/android_lastest 2>/dev/null | head -n 40 || true
    } > "$dir/android-state.txt" 2>&1 || true

    started_for_logcat="$TASK_STARTED_AT"
    if [[ ! "$started_for_logcat" =~ ^[0-9]+$ || "$started_for_logcat" -le 0 ]]; then
        started_for_logcat=$(date +%s)
    fi
    logcat_since=$(date -d "@$(( started_for_logcat > 120 ? started_for_logcat - 120 : started_for_logcat ))" '+%m-%d %H:%M:%S.000' 2>/dev/null || true)
    if [[ -n "$logcat_since" ]]; then
        timeout 25s adb $ADB_DEVICE logcat -d -T "$logcat_since" 2>/dev/null \
            | grep -Ei 'hypergryph|arknights|Unity|GP7|ace\.gshell|AndroidRuntime|FATAL|ANR|crash|tombstone|lowmemorykiller|ActivityManager|ActivityTaskManager|InputDispatcher|Network|OkHttp|SSL|DNS|RefClass|InvocationTargetException|oiface|fps|chromium|MaaTouch|fingerprint' \
            | tail -n 2000 > "$dir/logcat-filtered.log" 2>/dev/null || true
    else
        timeout 25s adb $ADB_DEVICE logcat -d 2>/dev/null \
            | grep -Ei 'hypergryph|arknights|Unity|GP7|ace\.gshell|AndroidRuntime|FATAL|ANR|crash|tombstone|lowmemorykiller|ActivityManager|ActivityTaskManager|InputDispatcher|Network|OkHttp|SSL|DNS|RefClass|InvocationTargetException|oiface|fps|chromium|MaaTouch|fingerprint' \
            | tail -n 2000 > "$dir/logcat-filtered.log" 2>/dev/null || true
    fi

    adb $ADB_DEVICE shell "ls -lt /data/tombstones 2>/dev/null | head -n 30" > "$dir/tombstones-list.txt" 2>&1 || true
    adb $ADB_DEVICE shell "for f in \$(ls -t /data/tombstones/tombstone_* 2>/dev/null | head -n 6); do echo ===\$f===; grep -E 'Cmdline|pid:|signal|Abort message|backtrace|#00|#01|#02|#03|#04|/odm|/vendor' \$f 2>/dev/null | head -n 120; done" > "$dir/tombstones-summary.txt" 2>&1 || true

    tar -czf "$archive" -C "$FAILURE_SNAPSHOT_DIR" "$(basename "$dir")" 2>/dev/null || true
    if [[ -s "$archive" ]]; then
        info "失败现场已保存: $archive"
    else
        warn "失败现场目录已保存但打包失败: $dir"
    fi
}

# === 状态报告 ===
print_status() {
    info "=== 环境状态 ==="
    info "maa-cli: $(maa version 2>&1 | head -1)"
    info "MaaCore: $(maa version 2>&1 | tail -1)"
    info "Device: $(adb $ADB_DEVICE shell getprop ro.product.model 2>/dev/null || echo unknown)"
    info "Android: $(adb $ADB_DEVICE shell getprop ro.build.version.release 2>/dev/null || echo unknown)"
    info "Screen: $(adb $ADB_DEVICE shell wm size 2>/dev/null | head -1 || echo unknown)"
    info "Battery: $(adb $ADB_DEVICE shell dumpsys battery 2>/dev/null | grep level | head -1 || echo unknown)"
    info "Loadavg: $(cat /proc/loadavg 2>/dev/null || echo unknown)"
    info "Mem: $(free -h 2>/dev/null | awk '/Mem:/{print $3 "/" $2 " used, available " $7}' || echo unknown)"
    info "================"
}

# === Telegram 通知 ===
send_tg_message() {
    local message="$1"

    local tg_file="${MAA_TG_FILE:-$MAA_HOME/tg.txt}"
    if [[ ! -f "$tg_file" ]]; then
        tg_file="$MAA_CONFIG_DIR/tg.txt"
    fi
    
    if [[ ! -f "$tg_file" ]]; then
        warn "未找到 tg.txt 配置文件，跳过发送 Telegram 通知"
        return 0
    fi
    
    local token
    local chat_id
    token=$(sed -n '1p' "$tg_file" | tr -d '\r\n')
    chat_id=$(sed -n '2p' "$tg_file" | tr -d '\r\n')
    
    if [[ -z "$token" || -z "$chat_id" ]]; then
        warn "tg.txt 格式不正确，跳过发送 Telegram 通知"
        return 0
    fi

    info "正在发送 Telegram 通知..."
    local response_file http_code attempt response
    response_file="/tmp/maa_tg_response.$$"

    for attempt in 1 2 3; do
        : > "$response_file"
        http_code=$(curl -sS -o "$response_file" -w "%{http_code}" -X POST \
            "https://api.telegram.org/bot${token}/sendMessage" \
            --connect-timeout 8 \
            --max-time 20 \
            --data-urlencode "chat_id=${chat_id}" \
            --data-urlencode "text=${message}" 2>>"$LOG_FILE" || true)

        if [[ "$http_code" == "200" ]]; then
            rm -f "$response_file"
            info "Telegram 通知发送成功"
            return 0
        fi

        response=$(head -c 240 "$response_file" 2>/dev/null | tr '\n' ' ')
        warn "Telegram 通知发送失败 attempt=${attempt}/3 http=${http_code:-curl_failed} response=${response:-empty}"
        sleep 5
    done

    rm -f "$response_file"
}

format_duration() {
    local seconds="${1:-0}"
    local hours=$(( seconds / 3600 ))
    local minutes=$(( (seconds % 3600) / 60 ))
    local rest=$(( seconds % 60 ))

    if (( hours > 0 )); then
        printf '%dh %dm %ds' "$hours" "$minutes" "$rest"
    else
        printf '%dm %ds' "$minutes" "$rest"
    fi
}

build_tg_summary() {
    local task="$1"
    local exit_code="$2"
    local started_at="${3:-0}"
    local failure_reason="${4:-UNSET}"
    local startup_status="${5:-not_run}"
    local startup_reason="${6:-}"
    local startup_screen="${7:-unknown}"
    python3 - "$LOG_FILE" "$task" "$exit_code" "$started_at" "$failure_reason" "$startup_status" "$startup_reason" "$startup_screen" "${EFFECTIVE_TASK_FILE:-}" "$ACTIVITY_STAGE_FILE" "${WEEKLY_LOGICAL_WEEKDAY:-}" "${WEEKLY_SOURCE_PATH:-}" "${WEEKLY_FALLBACK_REASON:-}" <<'PY'
import datetime as dt
import json
import os
import re
import sys
try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

log_path, task, exit_code_s, started_s = sys.argv[1:5]
failure_reason = sys.argv[5] if len(sys.argv) > 5 else "UNSET"
startup_status = sys.argv[6] if len(sys.argv) > 6 else "not_run"
startup_reason = sys.argv[7] if len(sys.argv) > 7 else ""
startup_screen = sys.argv[8] if len(sys.argv) > 8 else "unknown"
task_config_path = sys.argv[9] if len(sys.argv) > 9 else os.environ.get("MAA_TG_TASK_FILE", "")
activity_path = sys.argv[10] if len(sys.argv) > 10 else os.environ.get("MAA_TG_ACTIVITY_FILE", os.path.join(os.environ.get("MAA_CACHE_DIR", "/root/.cache/maa"), "StageActivityV2.json"))
weekly_weekday = sys.argv[11] if len(sys.argv) > 11 else ""
weekly_source_path = sys.argv[12] if len(sys.argv) > 12 else ""
weekly_fallback_reason = sys.argv[13] if len(sys.argv) > 13 else ""
exit_code = int(exit_code_s)
started_at = int(started_s or "0")

def parse_now():
    override = os.environ.get("MAA_TG_NOW", "").strip()
    if override:
        try:
            return dt.datetime.fromisoformat(override.replace("Z", "+00:00"))
        except ValueError:
            pass
    return dt.datetime.now()

now = parse_now()

try:
    log_text = open(log_path, "r", encoding="utf-8", errors="replace").read()
except Exception:
    log_text = ""
log_lines = log_text.splitlines()

def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def parse_dt(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("/", "-").replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None

def local_naive(value, timezone_value=8):
    try:
        tz_hours = int(timezone_value)
    except Exception:
        tz_hours = 8
    tz = dt.timezone(dt.timedelta(hours=tz_hours))
    if value.tzinfo is not None:
        return value.astimezone(tz).replace(tzinfo=None)
    return value

def fmt_remaining(seconds):
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

def strip_log_prefix(line):
    return re.sub(r"^\[\d{4}-.*?\]\s*\[[A-Z]+\]\s*", "", line).strip()

def sanitize_summary_line(line):
    text = strip_log_prefix(line)
    if re.search(r"(/var/log/|failure-snapshots|\.tar\.gz|日志:|log_file=)", text):
        return ""
    text = re.sub(r"[A-Za-z]:\\[^\s]+", "[path]", text)
    text = re.sub(r"/[^\s:]+(?:/[^\s:]+)+", "[path]", text)
    return text.strip()

def exit_code_hint(code):
    hints = {
        "1": "通用任务失败，通常表示 MAA 识别、导航或作战链路没有完成",
        "88": "截图连续失败，脚本已提前终止本次尝试",
        "91": "StartUp 失败，游戏没有进入可作业画面",
        "124": "任务超过硬超时，被 timeout 终止",
        "137": "任务进程被强制结束，可能是超时清理或系统杀进程",
    }
    return hints.get(str(code), "脚本或 maa-cli 返回的失败码")

def explain_failure_line(line):
    text = sanitize_summary_line(line)
    if not text:
        return ""

    m = re.search(r"任务失败 exit=(\d+) attempt=(\d+)/(\d+)", text)
    if not m:
        m = re.search(r"exit=(\d+) attempt=(\d+)/(\d+)", text)
    if m:
        code, attempt, total = m.groups()
        return f"第 {attempt}/{total} 次尝试失败：maa-cli 返回 exit={code}（{exit_code_hint(code)}）。"

    m = re.search(r"任务\s+(\S+)\s+在\s+(\d+)\s+次尝试后失败", text)
    if m:
        failed_task, total = m.groups()
        return f"最终结果：{failed_task} 连续 {total} 次尝试都失败，已停止重试。"

    if "TASK FAILED after retries" in text or text == "=== FAILED ===":
        return "任务状态：本轮以 FAILED 结束；重试机会已经用完。"
    if "清理明日方舟进程" in text or "等待" in text and "后重试" in text:
        return ""
    if "ADB" in text and ("失败" in text or "failed" in text.lower() or "Connection refused" in text):
        return "ADB 连接失败：控制链路没有连上 Android ADB，任务可能尚未稳定进入 MAA。"
    if "服务器停机维护公告" in text or "开启服务器停机维护" in text:
        return "服务器停机维护：检测到官方停机维护公告，维护期间无法登录游戏，本轮已安全停止。"
    if "StartUp Error" in text or "启动健康门失败" in text:
        return "StartUp 失败：游戏启动阶段没有进入可作业画面，本轮已停止盲跑。"
    if "ScreencapFailed" in text or "连续" in text and "截图失败" in text:
        return "截图连续失败：ADB 截图链路不稳定，脚本已提前终止本次尝试。"
    if "启动前 raw 截图尺寸未处于游戏横屏尺寸" in text:
        return "截图方向未稳定：启动前 raw buffer 仍不是 MAA 期望的横屏尺寸，本轮会走安全截图路径。"
    if "RAW_HEADER_MISMATCH" in text:
        return "截图方向不匹配：已禁用 raw 截图并尝试横屏预热，避免 MaaCore 误选截图通道。"
    if "LANDSCAPE_PREWARM" in text:
        return "横屏预热：启动前 raw header 未对齐，脚本先等待游戏 Surface 横屏稳定。"
    if "MaaCore 与资源更新失败" in text:
        return "资源更新失败：MaaCore 或资源更新没有成功，本轮不会继续使用旧资源盲跑。"
    return text

def load_json_file(path):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None

def activity_windows(activity_data, client="Official"):
    if not isinstance(activity_data, dict):
        return {}, []
    official = activity_data.get(client) or {}
    side_story = official.get("sideStoryStage") or official.get("SideStoryStage") or {}
    by_stage = {}
    groups = []
    for info in side_story.values():
        activity = info.get("Activity") or info.get("activity") or {}
        start = activity.get("UtcStartTime") or activity.get("utcStartTime")
        end = activity.get("UtcExpireTime") or activity.get("utcExpireTime")
        if not start or not end:
            continue
        timezone_value = activity.get("TimeZone", activity.get("timeZone", 8))
        name = activity.get("StageName") or activity.get("Name") or activity.get("Tip") or "活动"
        group = {"name": name, "start": start, "end": end, "timezone": timezone_value, "stages": []}
        for stage in info.get("Stages") or info.get("stages") or []:
            value = stage.get("Value") or stage.get("value") or stage.get("Display") or stage.get("display")
            if not value:
                continue
            group["stages"].append(value)
            by_stage.setdefault(value, []).append(group)
        groups.append(group)
    return by_stage, groups

def collect_fight_details(lines):
    stages = []
    results = []
    drops = []
    for line in lines:
        text = strip_log_prefix(line)
        m = re.search(r"\[Fight\s+([^\]]+)\].*Completed", text)
        if m:
            stages.append(m.group(1).strip())
        m = re.match(r"Fight\s+(\S+)\s+(.+)$", text)
        if m:
            stages.append(m.group(1).strip())
            results.append(text)
        if text.startswith("total drops:"):
            drops.append(text)
    seen = set()
    unique_stages = []
    for stage in stages:
        if stage and stage not in seen:
            seen.add(stage)
            unique_stages.append(stage)
    return unique_stages, results[-2:], drops[-2:]

def task_config_stages(path):
    if not path or not tomllib:
        return []
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return []
    stages = []
    for task_item in data.get("tasks", []) or []:
        if task_item.get("type") != "Fight":
            continue
        params = task_item.get("params") or {}
        if params.get("stage"):
            stages.append(params["stage"])
        for variant in task_item.get("variants") or []:
            stage = (variant.get("params") or {}).get("stage")
            if stage:
                stages.append(stage)
    seen = set()
    return [stage for stage in stages if not (stage in seen or seen.add(stage))]

def format_activity_lines(stages, activity_data):
    by_stage, groups = activity_windows(activity_data)
    lines = []
    alert = ""
    used_groups = set()

    def append_group(group, stage=None):
        nonlocal alert
        key = (group.get("name"), group.get("end"), stage or "")
        if key in used_groups:
            return
        used_groups.add(key)
        tz = group.get("timezone", 8)
        end_dt = parse_dt(group.get("end"))
        start_dt = parse_dt(group.get("start"))
        if not end_dt:
            return
        now_local = local_naive(now, tz)
        end_local = local_naive(end_dt, tz)
        start_local = local_naive(start_dt, tz) if start_dt else None
        stage_text = f" / {stage}" if stage else ""
        if now_local >= end_local:
            lines.append(f"活动时间: {group.get('name', '活动')}{stage_text}: 已于 {end_local:%Y-%m-%d %H:%M} 结束")
            if not alert:
                alert = "活动提醒: 关联活动已结束，请确认关卡兜底是否生效。"
            return
        remain = (end_local - now_local).total_seconds()
        if start_local and now_local < start_local:
            lines.append(f"活动时间: {group.get('name', '活动')}{stage_text}: {start_local:%Y-%m-%d %H:%M} 开始，{end_local:%Y-%m-%d %H:%M} 结束")
        else:
            lines.append(f"活动时间: {group.get('name', '活动')}{stage_text}: 至 {end_local:%Y-%m-%d %H:%M}，剩余 {fmt_remaining(remain)}")
        if remain <= 3 * 86400 and not alert:
            alert = "活动提醒: 距离结束不足 3 天，请优先确认活动关卡和兜底配置。"

    for stage in stages:
        for group in by_stage.get(stage, [])[:1]:
            append_group(group, stage)

    if not lines:
        now_local = local_naive(now, 8)
        active_groups = []
        for group in groups:
            start_dt = parse_dt(group.get("start"))
            end_dt = parse_dt(group.get("end"))
            if not end_dt:
                continue
            start_local = local_naive(start_dt, group.get("timezone", 8)) if start_dt else None
            end_local = local_naive(end_dt, group.get("timezone", 8))
            if (not start_local or now_local >= start_local) and now_local < end_local:
                active_groups.append((end_local, group))
        active_groups.sort(key=lambda item: item[0])
        if active_groups:
            append_group(active_groups[0][1])
    return lines[:3], alert

duration_s = max(0, int(dt.datetime.now().timestamp()) - started_at) if started_at else 0
for line in reversed(log_lines):
    m = re.search(r"DONE (?:任务完成|task completed) \((\d+)s\)", line)
    if m:
        duration_s = int(m.group(1))
        break

schedule_outcome = os.environ.get("MAA_TG_SCHEDULE_OUTCOME", "")
if exit_code == 0 and schedule_outcome == "partial_success":
    status_title = "日常任务完成，基建部分失败"
elif exit_code == 0 and schedule_outcome == "unknown":
    status_title = "日常任务完成，基建结果未知"
else:
    status_title = "所有任务已完成" if exit_code == 0 else "任务执行失败"
status_body = f"总耗时 {fmt_duration(duration_s)}" if exit_code == 0 else f"exit_code={exit_code}，已运行 {fmt_duration(duration_s)}"
out = [f"[MAA] {status_title}: {status_body}"]
out.append(f"任务: {task}")
weekday_names = {
    "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
    "fri": "周五", "sat": "周六", "sun": "周日",
}
if weekly_fallback_reason:
    out.append(f"周计划: {weekday_names.get(weekly_weekday, weekly_weekday or '当日')}配置异常，已回退 daily.toml（{weekly_fallback_reason}）")
elif weekly_weekday and os.path.basename(weekly_source_path).startswith("daily-"):
    out.append(f"周计划: {weekday_names.get(weekly_weekday, weekly_weekday)}配置")
out.append(f"时间: {local_naive(now, 8):%Y-%m-%d %H:%M:%S}")
if failure_reason and failure_reason != "UNSET":
    out.append(f"失败分型: {failure_reason}")
if startup_status and startup_status != "not_run":
    startup_line = f"启动健康: {startup_status}"
    if startup_reason:
        startup_line += f" / {startup_reason}"
    if startup_screen and startup_screen != "unknown":
        startup_line += f" / {startup_screen}"
    out.append(startup_line)

asst_path = os.environ.get("MAA_TG_ASST_LOG", os.path.join(os.environ.get("MAA_STATE_DIR", "/root/.local/state/maa"), "debug", "asst.log"))
asst_lines = []
try:
    with open(asst_path, "r", encoding="utf-8", errors="replace") as f:
        asst_lines = f.readlines()
except Exception:
    pass

start_dt = None
if started_at:
    start_dt = dt.datetime.fromtimestamp(started_at) - dt.timedelta(minutes=2)

run_asst = []
if start_dt:
    ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.")
    for line in asst_lines:
        m = ts_re.match(line)
        if not m:
            continue
        try:
            line_dt = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if line_dt >= start_dt:
            run_asst.append(line.rstrip("\n"))
else:
    run_asst = [line.rstrip("\n") for line in asst_lines[-2000:]]

fastest = None
for line in run_asst:
    m = re.search(r"The fastest way is ([A-Za-z0-9_]+) , cost: (\d+) ms", line)
    if m:
        fastest = (m.group(1), m.group(2))

cost = None
for line in run_asst:
    if '"what":"ScreencapCost"' in line:
        avg = re.search(r'"avg":(\d+)', line)
        mn = re.search(r'"min":(\d+)', line)
        mx = re.search(r'"max":(\d+)', line)
        fault = re.search(r'"fault_times":(\d+)', line)
        if avg:
            cost = (avg.group(1), mn.group(1) if mn else "?", mx.group(1) if mx else "?", fault.group(1) if fault else "0")

bench_choice = None
configured_mode = None
perf_alerts = []
for line in log_lines:
    m = re.search(r"截图通道选择: method=(\S+) min=(\d+)ms avg=(\d+)ms max=(\d+)ms size=(\d+)", line)
    if not m:
        m = re.search(r"screencap choice: method=(\S+) min=(\d+)ms avg=(\d+)ms max=(\d+)ms size=(\d+)", line)
    if m:
        bench_choice = m.groups()
    m = re.search(r"截图配置补丁已应用: mode=(\S+)", line)
    if not m:
        m = re.search(r"screencap patch applied: mode=(\S+)", line)
    if m:
        configured_mode = m.group(1)
    if "截图性能异常:" in line or "没有稳定的截图通道" in line:
        perf_alerts.append(line.strip())

if fastest:
    out.append(f"截图通道: {fastest[0]} ({fastest[1]} ms)")
elif bench_choice:
    method, mn, avg, mx, size = bench_choice
    out.append(f"截图通道: {method} (启动前 avg {avg} ms)")
elif configured_mode:
    out.append(f"截图通道: {configured_mode}")
if cost:
    out.append(f"截图耗时: avg {cost[0]} ms, min {cost[1]}, max {cost[2]}, fault {cost[3]}")
    try:
        avg_i, max_i = int(cost[0]), int(cost[2])
        if avg_i > 800 or max_i > 2000:
            out.append(f"截图性能告警: avg={avg_i}ms, max={max_i}ms，已超过运行阈值。")
    except ValueError:
        pass
if perf_alerts and exit_code != 0:
    out.append("截图性能告警:")
    out.extend(perf_alerts[-2:])

sanity = None
for line in run_asst:
    m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\].*Current Sanity: (\d+) , Max Sanity: (\d+)", line)
    if m:
        sanity = (m.group(1), int(m.group(2)), int(m.group(3)))
if sanity:
    report_time = dt.datetime.strptime(sanity[0], "%Y-%m-%d %H:%M:%S")
    cur, max_sanity = sanity[1], sanity[2]
    out.append(f"理智: {cur}/{max_sanity}")
    if cur < max_sanity:
        full_time = report_time + dt.timedelta(minutes=(max_sanity - cur) * 6)
        out.append(f"理智将在 {full_time:%Y-%m-%d %H:%M} 回满。({fmt_duration((full_time - now).total_seconds())} 后)")

fight_stages, fight_results, drops = collect_fight_details(log_lines)
if not fight_stages:
    fight_stages = task_config_stages(task_config_path)
if fight_stages:
    out.append("作战关卡: " + ", ".join(fight_stages[:4]))
if fight_results:
    out.append("作战结果: " + "；".join(fight_results))
if drops:
    out.append("作战掉落: " + "；".join(drops))

activity_data = load_json_file(activity_path)
activity_lines, activity_alert = format_activity_lines(fight_stages, activity_data)
out.extend(activity_lines)
if activity_alert:
    out.append(activity_alert)

high_recruit = []
for line in log_lines:
    if "★★★★★" in line or "★★★★★★" in line or "高级资深" in line or "资深干员" in line or "RecruitResult" in line:
        high_recruit.append(strip_log_prefix(line))
if high_recruit:
    out.append("公招结果:")
    out.extend(high_recruit[-4:])

if any("[技能专精]" in line or "技能专精回调已更新" in line for line in log_lines):
    mastery_state_path = os.path.join(
        os.environ.get("MAA_CONFIG_DIR", "/root/.config/maa"),
        "cultivation",
        "state.json",
    )
    mastery_data = (load_json_file(mastery_state_path) or {}).get("mastery") or {}
    mastery_lines = []
    for p in mastery_data.get("plans") or []:
        if not isinstance(p, dict) or not p.get("enabled", True):
            continue
        op_name = p.get("operator_name") or p.get("operator_id") or "干员"
        label = p.get("status_label") or p.get("status") or ""
        cur_m = p.get("current_mastery") or [0, 0, 0]
        tgt_m = p.get("skill_mastery") or [0, 0, 0]
        cur_parts = [
            f"{i + 1}技能专{cur_m[i] if i < len(cur_m) else 0}/{v}"
            for i, v in enumerate(tgt_m)
            if isinstance(v, int) and v > 0
        ]
        progress_str = f"（{'，'.join(cur_parts)}）" if cur_parts else ""
        if label:
            mastery_lines.append(f"{op_name}: {label}{progress_str}")
    if mastery_lines:
        out.append("技能专精: " + "；".join(mastery_lines[:3]))

if exit_code != 0:
    interesting = []
    maintenance_window = ""
    maintenance_detected = (
        failure_reason == "SERVER_MAINTENANCE"
        or "SERVER_MAINTENANCE" in (startup_reason or "")
    )
    for line in run_asst:
        if any(k in line for k in ("服务器停机维护公告", "开启服务器停机维护", "维护期间玩家将无法登录游戏")):
            maintenance_detected = True
        m = re.search(r"((?:20\d{2}年)?\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*[~～-]\s*\d{1,2}:\d{2})", line)
        if m and ("停机维护" in line or maintenance_detected or "text:" in line):
            maintenance_window = m.group(1).strip()
    if maintenance_detected:
        window_note = f"（{maintenance_window}）" if maintenance_window else ""
        interesting.append(f"服务器停机维护：检测到官方停机维护公告{window_note}，维护期间无法登录游戏，本轮已安全停止。")
    patterns = ("ERROR", "WARN", "FAILED", "任务失败", "失败现场", "启动健康", "ScreencapFailed", "截图故障诊断", "截图性能异常", "没有稳定的截图通道", "游戏掉线", "重新连接", "Reconnect", "Disconnect", "timeout when reading")
    for line in log_lines:
        if any(p in line for p in patterns):
            text = explain_failure_line(line)
            if text:
                interesting.append(text)
    for line in run_asst:
        if any(p in line for p in patterns):
            text = explain_failure_line(line)
            if text:
                interesting.append(text)
    if interesting:
        deduped = []
        seen = set()
        for item in interesting:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        out.append("失败说明:")
        out.extend(deduped[-8:])

message = "\n".join(out)
if len(message) > 3800:
    message = message[:3600].rstrip() + "\n...(已截断)"
print(message)
PY
}

send_tg_notification() {
    local task="$1"
    local exit_code="$2"
    local message
    message=$(MAA_TG_SCHEDULE_OUTCOME="${SCHEDULE_OUTCOME:-}" build_tg_summary "$task" "$exit_code" "$TASK_STARTED_AT" "${FAILURE_REASON:-UNSET}" "${STARTUP_HEALTH_STATUS:-not_run}" "${STARTUP_HEALTH_REASON:-}" "${STARTUP_HEALTH_SCREEN:-unknown}")
    if [[ "$REQUESTED_TASK" == "daily" ]] && [[ -f "$MEDICINE_PLAN_SCRIPT" ]]; then
        local medicine_summary
        medicine_summary=$(python3 "$MEDICINE_PLAN_SCRIPT" summary --config-dir "$(maa dir config)" \
            --started-at "$TASK_STARTED_AT" 2>>"$LOG_FILE") || medicine_summary="理智药：缓存读取失败，请核查日志"
        if [[ -n "$medicine_summary" ]]; then
            message="${message:0:3400}"
            message+=$'\n'"$medicine_summary"
        fi
    fi
    send_tg_message "$message"
}

start_task_watchdog() {
    local next_alert=$(( TASK_STARTED_AT + LONG_RUN_ALERT_AFTER ))
    WATCHDOG_PID=""

    (
        perf_next_alert=0
        mem_next_alert=0
        while true; do
            sleep 60
            now=$(date +%s)
            elapsed=$(( now - TASK_STARTED_AT ))
            if (( now >= mem_next_alert )) && runtime_memory_pressure_is_high; then
                send_tg_message "$(cat <<EOF
[MAA] 运行中内存压力过高。
$(memory_status_line)
阈值: MemAvailable < ${RUNTIME_MIN_MEM_AVAILABLE_KB}KB
已运行: $(format_duration "$elapsed")
EOF
)"
                mem_next_alert=$(( now + LONG_RUN_ALERT_INTERVAL ))
            fi
            cost_line=$(grep '"what":"ScreencapCost"' "$ASST_LOG_DIR/asst.log" 2>/dev/null | tail -n 1 || true)
            if [[ -n "$cost_line" ]]; then
                avg=$(printf '%s\n' "$cost_line" | sed -n 's/.*"avg":\([0-9][0-9]*\).*/\1/p')
                max=$(printf '%s\n' "$cost_line" | sed -n 's/.*"max":\([0-9][0-9]*\).*/\1/p')
                min=$(printf '%s\n' "$cost_line" | sed -n 's/.*"min":\([0-9][0-9]*\).*/\1/p')
                if [[ "$avg" =~ ^[0-9]+$ && "$max" =~ ^[0-9]+$ ]] &&
                   (( now >= perf_next_alert )) &&
                   (( avg > SCREENCAP_WARN_AVG_MS || max > SCREENCAP_WARN_MAX_MS )); then
                    method=$(cat "$SCREENCAP_METHOD_STATE" 2>/dev/null || echo unknown)
                    send_tg_message "$(cat <<EOF
[MAA] 截图性能异常：avg=${avg}ms, max=${max}ms, min=${min:-?}ms。
当前截图通道: ${method:-unknown}
阈值: avg>${SCREENCAP_WARN_AVG_MS}ms 或 max>${SCREENCAP_WARN_MAX_MS}ms
已运行: $(format_duration "$elapsed")
EOF
)"
                    perf_next_alert=$(( now + LONG_RUN_ALERT_INTERVAL ))
                fi
            fi
            if (( now < next_alert )); then
                continue
            fi

            elapsed_min=$(( elapsed / 60 ))
            last_mtime=$(stat -c %Y "$LOG_FILE" 2>/dev/null || echo "$TASK_STARTED_AT")
            stale_seconds=$(( now - last_mtime ))
            stale_min=$(( stale_seconds / 60 ))
            next_human=$(format_duration "$elapsed")
            recent=$(tail -n 6 "$LOG_FILE" 2>/dev/null || true)

            send_tg_message "$(cat <<EOF
[MAA] 任务已运行超过 60 分钟（当前已运行 ${elapsed_min} 分钟）。如果长时间无进一步日志更新，可能需要手动干预。
已运行: ${next_human}
最近日志更新时间: ${stale_min} 分钟前

最近日志:
${recent}
EOF
)"
            next_alert=$(( next_alert + LONG_RUN_ALERT_INTERVAL ))
        done
    ) &
    WATCHDOG_PID=$!
}

stop_task_watchdog() {
    if [[ -n "${WATCHDOG_PID:-}" ]]; then
        kill "$WATCHDOG_PID" 2>/dev/null || true
        wait "$WATCHDOG_PID" 2>/dev/null || true
        WATCHDOG_PID=""
    fi
}

# === 主流程 ===
main() {
    TASK_STARTED_AT=$(date +%s)

    resolve_weekly_task
    if (( SCHEDULED_MODE == 1 )); then
        initialize_scheduled_execution
        schedule_rc=$?
        if (( schedule_rc == 10 )); then
            rm -f "${ACTIVITY_EFFECTIVE_TASK_FILE:-}" 2>/dev/null || true
            exit 0
        fi
        if (( schedule_rc != 0 )); then
            rm -f "${ACTIVITY_EFFECTIVE_TASK_FILE:-}" 2>/dev/null || true
            exit "$schedule_rc"
        fi
        SCHEDULE_EXPECTED_COUNTS=()
        SCHEDULE_COMPLETED_COUNTS=()
        SCHEDULE_DOWNSTREAM_ERROR_COUNT=0
        SCHEDULE_EVENT_AMBIGUOUS=0
        if ! prepare_effective_task_config "$TASK" "preflight" ||
           ! load_scheduled_downstream_counts "$SCHEDULE_DOWNSTREAM_COUNTS"; then
            set_failure_reason "SCHEDULE_PREPARE_FAILED"
            mark_scheduled_state "unknown" "prepare_failed_before_device" || true
            cleanup_effective_task_config
            rm -f "${ACTIVITY_EFFECTIVE_TASK_FILE:-}" 2>/dev/null || true
            exit 22
        fi
    fi
    trap cleanup EXIT INT TERM

    info "======================================="
    info "  MAA 自动任务 $(date '+%Y-%m-%d %H:%M')"
    info "  任务: $TASK"
    info "  日志: $LOG_FILE"
    info "======================================="

    info "清理历史残留进程..."
    kill_stale_processes
    echo $$ > "$RUNNER_PID_FILE" 2>/dev/null || true

    cleanup_logs
    rotate_asst_logs

    if memory_pressure_is_high; then
        set_failure_reason "MEMORY_PRESSURE_PRECHECK_FAILED"
        error "内存压力过高，停止本次任务: $(memory_status_line)"
        send_tg_message "$(cat <<EOF
[MAA] 任务未启动：设备内存压力过高。
$(memory_status_line)
阈值: MemAvailable >= ${MIN_MEM_AVAILABLE_KB}KB 且 SwapUsed <= ${MAX_SWAP_USED_KB}KB
建议: 空闲时重启设备或释放后台占用后再运行。
EOF
)"
        exit 12
    fi

    if [[ "$STARTUP_HEALTH_ONLY" -eq 1 ]]; then
        if ! prepare_run_session; then
            write_startup_health_state "failed" "RESOURCE_UPDATE_FAILED" "unknown" "run session preflight failed"
            exit 10
        fi
        local health_exit=0
        info "进入启动健康检查模式，不运行完整 MAA 日常任务"
        print_status
        run_startup_health_check || health_exit=$?
        if (( health_exit != 0 )); then
            collect_failure_snapshot "$TASK" "$health_exit"
        fi
        exit "$health_exit"
    fi

    if [[ "$BENCH_ONLY" -eq 1 ]]; then
        info "进入截图基准测试模式，不运行 MAA 任务"
        if ! ensure_adb; then
            set_failure_reason "ADB_PRECHECK_FAILED"
            send_tg_notification "$TASK" 1
            exit 1
        fi
        wake_screen
        backlight_off_keep_awake
        lock_rotation
        set_maa_resolution
        wake_screen
        info "截图基准不再手动预启动游戏，仅验证当前横屏截图链路"
        sleep 2
        backlight_off_keep_awake
        print_status
        if runtime_memory_pressure_is_high; then
            set_failure_reason "MEMORY_PRESSURE_AFTER_STARTUP"
            error "游戏启动后内存压力过高，跳过截图基准: $(memory_status_line)"
            send_tg_notification "$TASK" 12
            exit 12
        fi
        if ! verify_raw_geometry_before_run; then
            warn "raw 截图几何预检失败，继续执行 encode-only 截图基准"
        fi
        if benchmark_screencap_methods; then
            info "截图基准测试完成"
            exit 0
        fi
        error "截图基准测试失败"
        send_tg_notification "$TASK" 1
        exit 1
    fi

    local exit_code=0
    if ! update_resources_before_run; then
        exit_code=10
        set_failure_reason "RESOURCE_UPDATE_FAILED"
        send_tg_notification "$TASK" "$exit_code"
        exit "$exit_code"
    fi

    # 首次启动前作前置环境检查
    if ! prepare_run_session; then
        set_failure_reason "RESOURCE_UPDATE_FAILED"
        send_tg_notification "$TASK" 10
        exit 10
    fi
    if ! ensure_adb; then
        error "ADB 连接失败"
        set_failure_reason "ADB_PRECHECK_FAILED"
        send_tg_notification "$TASK" 1
        exit 1
    fi

    if ! check_arknights_installed; then
        set_failure_reason "ARKNIGHTS_NOT_INSTALLED"
        send_tg_notification "$TASK" 1
        exit 1
    fi

    if ! test_screencap; then
        set_failure_reason "SCREENSHOT_PRECHECK_FAILED"
        send_tg_notification "$TASK" 1
        exit 1
    fi

    print_status

    start_task_watchdog
    run_maa_task "$TASK" || exit_code=$?
    stop_task_watchdog

    if (( exit_code != 0 )); then
        collect_failure_snapshot "$TASK" "$exit_code"
    fi

    # 发送 Telegram 通知
    send_tg_notification "$TASK" "$exit_code"

    # cleanup trap 会自动恢复分辨率并杀掉游戏

    if (( exit_code == 0 && PARTIAL_SUCCESS == 1 )); then
        info "=== PARTIAL SUCCESS ==="
    elif (( exit_code == 0 )) && [[ "$SCHEDULE_OUTCOME" == "unknown" ]]; then
        warn "=== SCHEDULE UNKNOWN ==="
    elif (( exit_code == 0 )); then
        info "=== ALL DONE ==="
    else
        error "=== FAILED ==="
        request_reboot_after_startup_render_stall
    fi

    exit $exit_code
}

consume_scheduled_daily_skip() {
    [[ "$REQUESTED_TASK" == "daily" ]] || return 1
    [[ -f "$SCHEDULE_CONTROL_SCRIPT" ]] || return 1
    python3 "$SCHEDULE_CONTROL_SCRIPT" consume \
        --config-dir "$SCHEDULE_CONTROL_CONFIG_DIR" \
        --parent-pid "${MAA_INVOCATION_PARENT_PID:-$PPID}" \
        --proc-root "${MAA_PROC_ROOT:-/proc}"
}

if consume_scheduled_daily_skip; then
    info "SKIP_NEXT_DAILY_CONSUMED: 已跳过本次定时日常；资源更新与后续定时不受影响"
    exit 0
fi

run_with_outer_lock() {
    local ec
    # Use flock as an outer wrapper. Holding the lock inside main lets adb
    # inherit the descriptor and keep the task lock forever.
    if [[ "${FLOCKER:-}" != "$LOCK_FILE" ]]; then
        if (( SCHEDULED_MODE == 1 )); then
            env FLOCKER="$LOCK_FILE" MAA_SCHEDULED_LOGICAL_DATE="$SCHEDULED_DATE" \
                flock -w "$LOCK_WAIT_SECONDS" -E 75 -o "$LOCK_FILE" "$0" "$@"
        else
            env FLOCKER="$LOCK_FILE" flock -n -E 75 -o "$LOCK_FILE" "$0" "$@"
        fi
        ec=$?
        if [[ "$ec" -eq 75 ]]; then
            if (( SCHEDULED_MODE == 1 )); then
                set_failure_reason "SCHEDULE_LOCK_TIMEOUT"
                error "SCHEDULE_LOCK_TIMEOUT: waited=${LOCK_WAIT_SECONDS}s date=$SCHEDULED_DATE weekday=$WEEKDAY_OVERRIDE time=$SCHEDULED_TIME"
                send_tg_message "[MAA] 基建排班等待任务锁超时（${LOCK_WAIT_SECONDS}s），本次未启动设备或资源流程。"
            else
                error "已有 MAA 更新或任务正在运行，退出以避免互相抢占"
            fi
        fi
        exit "$ec"
    fi
    main
}

run_with_outer_lock "$@"
