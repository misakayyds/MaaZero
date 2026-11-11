#!/bin/bash
# Overlay navigation resources from MaaAssistantArknights dev-v2 when MaaResource
# has new map data but lacks tasks/Stages navigation support.
set -uo pipefail

export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LANG="${LANG:-en_US.UTF-8}"

LOG_FILE="${LOG_FILE:-/dev/stdout}"
RESOURCE_DIR="${RESOURCE_DIR:-/root/.local/share/maa/resource}"
RESOURCE_SOURCE_DIR="${RESOURCE_SOURCE_DIR:-}"
CORE_RESOURCE_GIT_URL="${MAA_NAV_OVERLAY_GIT_URL:-https://github.com/MaaAssistantArknights/MaaAssistantArknights.git}"
CORE_RESOURCE_REF="${MAA_NAV_OVERLAY_REF:-dev-v2}"
CORE_RESOURCE_CLONE_DIR="${MAA_NAV_OVERLAY_CLONE_DIR:-/root/.local/share/maa/MaaAssistantArknights-dev-v2}"
RECENT_GROUP_LIMIT="${MAA_NAV_OVERLAY_GROUP_LIMIT:-4}"
STATE_FILE="${MAA_NAV_OVERLAY_STATE_FILE:-$RESOURCE_DIR/.maa-nav-overlay-state.json}"

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [nav-overlay] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}

overlay_prefixes() {
    python3 - "$RESOURCE_DIR" "$RESOURCE_SOURCE_DIR" "$STATE_FILE" "$RECENT_GROUP_LIMIT" <<'PY'
import json
import hashlib
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
source_arg = sys.argv[2]
source = Path(source_arg) if source_arg else None
state_path = Path(sys.argv[3])
limit = int(sys.argv[4])
overview = root / "Arknights-Tile-Pos" / "overview.json"
stage_dir = root / "tasks" / "Stages"
tpl_root = root / "template" / "StageNavigation" / "SideStory"

try:
    data = json.loads(overview.read_text("utf-8"))
except Exception:
    sys.exit(0)

groups = {}
for item in data.values():
    if not isinstance(item, dict):
        continue
    level_id = item.get("levelId", "")
    code = item.get("code", "")
    if not level_id.startswith("activities/") or not code:
        continue
    parts = level_id.split("/")
    if len(parts) < 2:
        continue
    activity_id = parts[1]
    prefix = code.split("-", 1)[0]
    groups.setdefault(activity_id, set()).add(prefix)

task_prefixes = {p.stem for p in stage_dir.glob("*.json")} if stage_dir.is_dir() else set()
tpl_prefixes = {p.name for p in tpl_root.iterdir() if p.is_dir()} if tpl_root.is_dir() else set()

try:
    state = json.loads(state_path.read_text("utf-8"))
except Exception:
    state = {}
managed = state.get("managed", {})

source_task_prefixes = set()
source_tpl_prefixes = set()
source_known = bool(source and source.is_dir())
if source_known:
    source_stage_dir = source / "tasks" / "Stages"
    source_tpl_root = source / "template" / "StageNavigation" / "SideStory"
    if source_stage_dir.is_dir():
        source_task_prefixes = {p.stem for p in source_stage_dir.glob("*.json")}
    if source_tpl_root.is_dir():
        source_tpl_prefixes = {p.name for p in source_tpl_root.iterdir() if p.is_dir()}

def activity_sort_key(activity_id):
    nums = [int(x) for x in re.findall(r"\d+", activity_id or "")]
    return nums[0] if nums else -1

def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def tree_sha256(path):
    if not path.is_dir():
        return None
    h = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(item.relative_to(path)).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(file_sha256(item).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()

def runtime_matches_overlay(prefix, entry):
    stage_hash = entry.get("stage_sha256")
    template_hash = entry.get("template_sha256")
    if not stage_hash and not template_hash:
        # Legacy state from before hash tracking; refresh once and record hashes.
        return True
    stage_path = stage_dir / f"{prefix}.json"
    tpl_path = tpl_root / prefix
    if stage_hash and (not stage_path.is_file() or file_sha256(stage_path) != stage_hash):
        return False
    if template_hash and tree_sha256(tpl_path) != template_hash:
        return False
    return True

overlay = []
official_now = []
for activity_id, prefixes in sorted(groups.items(), key=lambda kv: activity_sort_key(kv[0]), reverse=True)[:limit]:
    for prefix in sorted(prefixes):
        source_has_nav = prefix in source_task_prefixes
        runtime_has_nav = prefix in task_prefixes
        managed_entry = managed.get(prefix)
        if source_known and source_has_nav:
            if managed_entry:
                official_now.append(prefix)
                managed.pop(prefix, None)
            continue
        if managed_entry:
            if runtime_matches_overlay(prefix, managed_entry):
                if prefix not in overlay:
                    overlay.append(prefix)
            else:
                official_now.append(prefix)
                managed.pop(prefix, None)
            continue
        if (not runtime_has_nav or prefix in managed) and prefix not in overlay:
            overlay.append(prefix)

if official_now:
    state["managed"] = managed
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")
    print("[nav-overlay] official-resource-now=" + ",".join(official_now), file=sys.stderr)

if overlay:
    print("\n".join(overlay))
PY
}

prepare_core_resource_checkout() {
    mkdir -p "$(dirname "$CORE_RESOURCE_CLONE_DIR")"
    if [[ -d "$CORE_RESOURCE_CLONE_DIR/.git" ]]; then
        log "更新官方主仓资源索引: ref=$CORE_RESOURCE_REF"
        git -C "$CORE_RESOURCE_CLONE_DIR" remote set-url origin "$CORE_RESOURCE_GIT_URL" >> "$LOG_FILE" 2>&1 || true
        git -C "$CORE_RESOURCE_CLONE_DIR" fetch --depth 1 origin "$CORE_RESOURCE_REF" >> "$LOG_FILE" 2>&1
    else
        log "浅克隆官方主仓资源索引: ref=$CORE_RESOURCE_REF"
        rm -rf "$CORE_RESOURCE_CLONE_DIR"
        git clone --filter=blob:none --no-checkout --depth 1 --branch "$CORE_RESOURCE_REF" \
            "$CORE_RESOURCE_GIT_URL" "$CORE_RESOURCE_CLONE_DIR" >> "$LOG_FILE" 2>&1
    fi

    git -C "$CORE_RESOURCE_CLONE_DIR" sparse-checkout init --cone >> "$LOG_FILE" 2>&1 || true
    git -C "$CORE_RESOURCE_CLONE_DIR" sparse-checkout set \
        resource/tasks/Stages \
        resource/template/StageNavigation/SideStory >> "$LOG_FILE" 2>&1
    git -C "$CORE_RESOURCE_CLONE_DIR" checkout -f -B "$CORE_RESOURCE_REF" "origin/$CORE_RESOURCE_REF" >> "$LOG_FILE" 2>&1
}

apply_overlay() {
    local core_commit
    core_commit="$(git -C "$CORE_RESOURCE_CLONE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    python3 - "$RESOURCE_DIR" "$CORE_RESOURCE_CLONE_DIR/resource" "$STATE_FILE" "$CORE_RESOURCE_REF" "$core_commit" "$@" <<'PY'
import json
import hashlib
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

runtime = Path(sys.argv[1])
core_resource = Path(sys.argv[2])
state_path = Path(sys.argv[3])
ref = sys.argv[4]
commit = sys.argv[5]
prefixes = sys.argv[6:]

copied = []
missing = []
copied_meta = {}

def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def tree_sha256(path):
    if not path.is_dir():
        return None
    h = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(str(item.relative_to(path)).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(file_sha256(item).encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()

for prefix in prefixes:
    copied_one = False
    stage_hash = None
    template_hash = None
    stage_src = core_resource / "tasks" / "Stages" / f"{prefix}.json"
    if stage_src.is_file():
        stage_dst = runtime / "tasks" / "Stages" / f"{prefix}.json"
        stage_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stage_src, stage_dst)
        stage_hash = file_sha256(stage_dst)
        copied_one = True

    tpl_src = core_resource / "template" / "StageNavigation" / "SideStory" / prefix
    if tpl_src.is_dir():
        tpl_dst = runtime / "template" / "StageNavigation" / "SideStory" / prefix
        shutil.copytree(tpl_src, tpl_dst, dirs_exist_ok=True)
        template_hash = tree_sha256(tpl_dst)
        copied_one = True

    if copied_one:
        copied.append(prefix)
        copied_meta[prefix] = {
            "stage_sha256": stage_hash,
            "template_sha256": template_hash,
        }
    else:
        missing.append(prefix)

if copied:
    try:
        state = json.loads(state_path.read_text("utf-8"))
    except Exception:
        state = {}
    managed = state.setdefault("managed", {})
    updated_at = datetime.now(timezone.utc).isoformat()
    for prefix in copied:
        managed[prefix] = {
            "source": "MaaAssistantArknights",
            "ref": ref,
            "commit": commit,
            "updated_at": updated_at,
            **{k: v for k, v in copied_meta.get(prefix, {}).items() if v},
        }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")
    print("[nav-overlay] copied=" + ",".join(copied))
    print(f"[nav-overlay] source-ref={ref} source-commit={commit}")
if missing:
    print("[nav-overlay] no-official-navigation=" + ",".join(missing))
PY
}

main() {
    if [[ "${MAA_NAV_OVERLAY:-1}" != "1" ]]; then
        log "导航覆盖层已关闭: MAA_NAV_OVERLAY=${MAA_NAV_OVERLAY:-}"
        return 0
    fi

    if [[ ! -d "$RESOURCE_DIR" ]]; then
        log "运行资源目录不存在，跳过: $RESOURCE_DIR"
        return 0
    fi

    mapfile -t prefixes < <(overlay_prefixes 2> >(tee -a "$LOG_FILE" >&2))
    local nonempty=()
    local prefix
    for prefix in "${prefixes[@]}"; do
        [[ -n "$prefix" ]] && nonempty+=("$prefix")
    done
    prefixes=("${nonempty[@]}")
    if [[ "${#prefixes[@]}" -eq 0 ]]; then
        log "最新活动无需导航覆盖（官方资源已具备或无缺口）"
        return 0
    fi

    log "检测到需要官方主仓导航覆盖的活动前缀: ${prefixes[*]}"
    if ! prepare_core_resource_checkout; then
        log "官方主仓导航资源拉取失败"
        return 1
    fi

    apply_overlay "${prefixes[@]}" 2>&1 | tee -a "$LOG_FILE"
    log "导航覆盖层处理完成"
}

main "$@"
