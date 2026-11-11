#!/usr/bin/env python3
"""
MAA WebUI v2 - 复刻桌面 GUI 体验
单文件 Python Web 应用，模拟 MAA 桌面端界面

运行: python3 /root/maa-scripts/maa-webui.py
访问: http://<设备IP>:8088
"""

import http.server
import json
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

from maa_infrast_plan import (
    PlanImportError,
    PlanValidationError,
    commit_plan_import,
    get_plan_details,
    list_plan_summaries,
    preview_plan_import,
)
from maa_operator_box import (
    OperatorBoxError,
    commit_weekly_schedule,
    compare_plan,
    load_scan_status,
    load_weekly_schedule,
    preview_weekly_schedule,
)
from maa_infrast_requirements import (
    compare_training,
    load_requirement_index,
)
from maa_infrast_schedule import (
    ScheduleError,
    analyze_plan_file,
    build_cycle_definition,
    commit_schedule_change,
    commit_schedule_restore,
    parse_managed_crontab,
    preview_schedule_change,
    preview_schedule_restore,
    recover_pending_transaction,
)
from maa_weekly_plan import (
    WEEKDAYS,
    atomic_write_text,
    initialize_weekday_files,
    load_settings,
    resolve_daily_task,
    set_enabled,
    validate_task_file,
    weekday_task_path,
)
from maa_task_config import load_tasks, save_tasks
from maa_activity_cache import get_activity
from maa_schedule_control import (
    arm_skip_next_daily,
    cancel_skip_next_daily,
    load_skip_next_daily,
)

PORT = 8088
MAA_BIN = "/root/.local/bin/maa"
SCRIPT_DIR = "/root/maa-scripts"
LOG_DIR = "/var/log/maa"
STARTUP_HEALTH_STATE_FILE = os.path.join(LOG_DIR, "startup-health.env")
ENV_PATH = "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

CONFIG_DIR = ""
try:
    r = subprocess.run([MAA_BIN, "dir", "config"], capture_output=True, text=True, timeout=5,
                       env={**os.environ, "PATH": ENV_PATH})
    CONFIG_DIR = r.stdout.strip()
except Exception:
    CONFIG_DIR = os.path.expanduser("~/.config/maa")

TASK_FILE = os.path.join(CONFIG_DIR, "tasks", "daily.toml")
PROFILE_FILE = os.path.join(CONFIG_DIR, "profiles", "default.toml")
CLI_FILE = os.path.join(CONFIG_DIR, "cli.toml")
RESOURCE_DIR = "/root/.local/share/maa/resource"
RESOURCE_CLONE_DIR = "/root/.local/share/maa/MaaResource/resource"
WEB_SERVER_CLASS = http.server.ThreadingHTTPServer

FALLBACK_SUPPLY_STAGE_CODES = (
    "CE-5", "CE-6", "LS-5", "LS-6", "AP-5", "CA-5", "SK-5",
    "PR-A-1", "PR-A-2", "PR-B-1", "PR-B-2",
    "PR-C-1", "PR-C-2", "PR-D-1", "PR-D-2",
)
COMMON_MAIN_STAGE_CODES = ("Annihilation", "1-7", "R8-11", "12-17-HARD", "JT8-3")
COMMON_STAGE_RESOURCE_ALIASES = {"12-17-HARD": "12-17"}
SUPPLY_STAGE_RE = re.compile(r"^(?:CE|LS|AP|CA|SK)-\d+$|^PR-[A-D]-[12]$")
SUPPLY_PREFIX_META = {
    "CE": ("龙门币/经验", "龙门币", ("tue", "thu", "sat", "sun")),
    "LS": ("龙门币/经验", "作战记录", ("mon", "thu", "sat", "sun")),
    "AP": ("红票/技能/碳", "采购凭证", ("mon", "thu", "sat", "sun")),
    "CA": ("红票/技能/碳", "技能概要", ("tue", "wed", "fri", "sun")),
    "SK": ("红票/技能/碳", "碳素", ("mon", "wed", "fri", "sat")),
    "PR-A": ("芯片", "", ("mon", "thu", "fri", "sun")),
    "PR-B": ("芯片", "", ("mon", "tue", "fri", "sat")),
    "PR-C": ("芯片", "", ("wed", "thu", "sat", "sun")),
    "PR-D": ("芯片", "", ("tue", "wed", "sat", "sun")),
}

STAGE_API = "https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json"
RESOURCE_STAGE_GROUP_LIMIT = 4
MAX_IMPORT_BODY_BYTES = 4_194_304
MAX_SCHEDULE_BODY_BYTES = 1_048_576
MAX_CULTIVATION_BODY_BYTES = 262_144
CULTIVATION_CATALOG_FILE = os.path.join(SCRIPT_DIR, "maa-cultivation-data.json")
CULTIVATION_RUNNER = os.path.join(SCRIPT_DIR, "maa-cultivation-run.py")
CULTIVATION_AVATAR_DIR = "/var/cache/maa/avatars"
CULTIVATION_AVATAR_ID_RE = re.compile(r"^char_[0-9a-zA-Z_]{1,48}$")
OPERATOR_SCAN_TASK = "operator-scan"
OPERATOR_SCAN_TASK_TEXT = """[[tasks]]
name = "开始唤醒"
type = "StartUp"
params = { client_type = "Official", start_game_enabled = true }

[[tasks]]
name = "干员扫描"
type = "OperBox"
"""

running_task = {"active": False, "task": "", "start_time": "", "pid": None, "output": ""}
run_lock = threading.Lock()
# Coordinates WebUI writers only. External `crontab` invocations do not honor
# this lock; commit still performs its final hash/read-back checks under it.
schedule_write_lock = threading.Lock()

# ─── stage cache ───
_stage_cache = {"data": None, "ts": 0, "error": ""}
_common_stage_cache = {"key": None, "data": None}

def get_env():
    env = os.environ.copy()
    env["PATH"] = ENV_PATH
    env["LANG"] = "en_US.UTF-8"
    return env


def read_crontab_bytes():
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, timeout=5, env=get_env()
    )
    if result.returncode not in (0, 1):
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise OSError(stderr or "crontab read failed")
    stdout = result.stdout
    return stdout.encode("utf-8") if isinstance(stdout, str) else stdout


def install_crontab_bytes(content):
    result = subprocess.run(
        ["crontab", "-"],
        input=content,
        capture_output=True,
        timeout=5,
        env=get_env(),
    )
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise OSError(stderr or "crontab install failed")


def recover_schedule_or_raise(config_dir=CONFIG_DIR):
    result = recover_pending_transaction(
        Path(config_dir), read_crontab_bytes, install_crontab_bytes
    )
    if result.get("status") == "blocked":
        raise ScheduleError(
            result.get("code", "pending_transaction"),
            "$.transaction",
            "排班事务恢复被阻止",
        )
    return result

# ─── file helpers ───
def read_file(p):
    try: return Path(p).read_text("utf-8")
    except: return ""

def write_file(p, c):
    try:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(c, "utf-8")
        return True
    except:
        return False

def get_weekly_plan_payload(config_dir=CONFIG_DIR):
    config_dir = Path(config_dir)
    settings = load_settings(config_dir)
    days = []
    for weekday in WEEKDAYS:
        path = weekday_task_path(config_dir, weekday)
        valid, error = validate_task_file(path)
        days.append({
            "weekday": weekday,
            "tasks": load_tasks(path) if valid else [],
            "valid": valid,
            "error": error,
        })
    selection = resolve_daily_task(config_dir)
    return {
        **settings,
        "current_weekday": selection["logical_weekday"],
        "days": days,
        "selection": selection,
    }


def get_infrast_dir(config_dir=CONFIG_DIR):
    return Path(config_dir) / "infrast"


INFRAST_REQUIREMENTS_PATH = (
    Path(__file__).resolve().parent / "infrast-operator-requirements.json"
)


def get_infrast_plan_list_payload(config_dir=CONFIG_DIR):
    try:
        manifest = _read_schedule_manifest(config_dir)
    except ScheduleError:
        manifest = None
    active_filename = (
        manifest.get("filename")
        if manifest and manifest.get("enabled") is True
        else None
    )
    plans = [
        {**plan, "schedule_active": plan.get("filename") == active_filename}
        for plan in list_plan_summaries(get_infrast_dir(config_dir))
    ]
    return {"plans": plans}


def get_infrast_plan_payload(config_dir, filename):
    return get_plan_details(get_infrast_dir(config_dir), filename)


def get_operator_box_payload(config_dir=CONFIG_DIR, filename=None, crontab_raw=b""):
    config_dir = Path(config_dir)
    status = load_scan_status(
        config_dir / "operator-box.json",
        config_dir / "operator-box-state.json",
    )
    payload = {**status, "comparison": None, "training": None}
    snapshot = status.get("snapshot")
    if snapshot:
        payload["owned"] = snapshot["own_opers"]
        payload["unowned"] = [
            item for item in snapshot["all_opers"] if item.get("own") is False
        ]
        if filename:
            details = get_infrast_plan_payload(config_dir, filename)
            payload["comparison"] = {
                "filename": filename,
                **compare_plan(details, snapshot),
            }
            requirement_status = load_requirement_index(INFRAST_REQUIREMENTS_PATH)
            payload["training"] = {
                **compare_training(
                    details,
                    snapshot,
                    requirement_status.get("index"),
                ),
                "index": {
                    "available": requirement_status["available"],
                    "generated_at": requirement_status.get("generated_at"),
                    "source": requirement_status.get("source"),
                },
            }
    else:
        payload["owned"] = []
        payload["unowned"] = []
    payload["weekly"] = {
        **load_weekly_schedule(config_dir, crontab_raw),
        "available": bool(status.get("available")),
        "reason": (
            "首次完整扫描成功后才能启用"
            if not status.get("available")
            else ""
        ),
    }
    return payload


def _ensure_operator_scan_task(config_dir=CONFIG_DIR):
    task_path = Path(config_dir) / "tasks" / f"{OPERATOR_SCAN_TASK}.toml"
    if task_path.exists() and task_path.read_text("utf-8") == OPERATOR_SCAN_TASK_TEXT:
        return task_path
    atomic_write_text(task_path, OPERATOR_SCAN_TASK_TEXT)
    return task_path


def _read_schedule_manifest(config_dir):
    path = Path(config_dir) / "infrast-schedule.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ScheduleError("invalid_manifest", "$.manifest", "排班清单损坏")
    if not isinstance(value, dict):
        raise ScheduleError("invalid_manifest", "$.manifest", "排班清单必须是对象")
    return value


def _valid_schedule_manifest_days(days, plan_count):
    if not isinstance(days, dict) or set(days) != set(WEEKDAYS):
        return False

    def valid_time(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
            return False
        hour, minute = map(int, value.split(":"))
        return hour < 24 and minute < 60

    for weekday in WEEKDAYS:
        slots = days[weekday]
        if not isinstance(slots, list) or len(slots) != plan_count:
            return False
        indexes = []
        times = set()
        for slot in slots:
            if not isinstance(slot, dict):
                return False
            plan_index = slot.get("plan_index")
            time = slot.get("time")
            if (
                not isinstance(plan_index, int)
                or isinstance(plan_index, bool)
                or not valid_time(time)
                or not valid_time(slot.get("source_time"))
                or not isinstance(slot.get("name"), str)
                or not slot["name"]
                or slot.get("source") not in {"period", "inferred", "manual"}
                or time in times
            ):
                return False
            indexes.append(plan_index)
            times.add(time)
        if sorted(indexes) != list(range(plan_count)):
            return False
    return True


def get_infrast_schedule_payload(config_dir, filename, crontab_raw):
    config_dir = Path(config_dir)
    manifest = _read_schedule_manifest(config_dir)
    anchor_time = "02:00"
    if (
        manifest
        and manifest.get("filename") == filename
        and isinstance(manifest.get("cycle"), dict)
        and isinstance(manifest["cycle"].get("anchor_time"), str)
    ):
        anchor_time = manifest["cycle"]["anchor_time"]
    analysis = analyze_plan_file(
        get_infrast_dir(config_dir), filename, anchor_time=anchor_time
    )
    details = get_plan_details(get_infrast_dir(config_dir), filename)
    plans = {
        plan.get("index"): plan
        for plan in details.get("plans", [])
        if isinstance(plan, dict)
    }
    manifest_matches = bool(
        manifest
        and manifest.get("enabled") is True
        and manifest.get("filename") == filename
        and manifest.get("json_sha256") == analysis.get("sha256")
        and _valid_schedule_manifest_days(
            manifest.get("days"), len(analysis.get("slots", []))
        )
    )
    if (
        manifest
        and manifest.get("schema_version") == 2
        and manifest.get("enabled") is True
        and manifest.get("filename") == filename
        and manifest.get("json_sha256") == analysis.get("sha256")
        and isinstance(manifest.get("cycle"), dict)
    ):
        cycle = manifest["cycle"]
        try:
            expected_cycle = build_cycle_definition(
                analysis.get("durations_hours"),
                [
                    str(slot.get("name") or f"计划 {index + 1}")
                    for index, slot in enumerate(analysis.get("slots", []))
                ],
                cycle.get("anchor_date"),
                cycle.get("anchor_time"),
            )
        except ScheduleError:
            expected_cycle = None
        manifest_matches = (
            manifest.get("days") == {}
            and expected_cycle is not None
            and cycle == expected_cycle
        )
    analysis = {
        **analysis,
        "slots": [
            {
                **slot,
                "details": plans.get(slot.get("plan_index")),
                "merge_status": "已合并" if manifest_matches else "待合并",
                "validation_badge": "有效",
            }
            for slot in analysis.get("slots", [])
        ],
    }
    cron = parse_managed_crontab(crontab_raw)
    safe_entries = [
        {
            "display": entry["line"].decode("utf-8", "replace"),
            "line_hash": entry["line_hash"],
        }
        for entry in cron["adoption_candidates"]
    ]
    managed_lines = [
        line
        for line in cron["managed_block"].splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    ]
    return {
        "analysis": analysis,
        "details": details,
        "manifest": manifest,
        "weekly": get_weekly_plan_payload(config_dir),
        "cron": {
            "has_managed_block": cron["has_managed_block"],
            "managed_entry_count": len(managed_lines),
            "safe_daily_entries": safe_entries,
            "unsafe_daily": {
                "blocked": bool(cron["unsafe_daily_lines"]),
                "count": len(cron["unsafe_daily_lines"]),
            },
        },
    }


def infrast_error_status(error):
    if error.code == "file_not_found":
        return 404
    if error.code in {"invalid_filename", "file_not_regular"}:
        return 400
    return 422


def infrast_import_error_status(error):
    if error.code in {"invalid_request", "invalid_filename", "file_not_regular"}:
        return 400
    if error.code in {"request_too_large", "file_too_large"}:
        return 413
    if error.code in {"invalid_utf8", "invalid_json", "invalid_schema"}:
        return 422
    if error.code in {"preview_changed", "conflict", "stale_preview"}:
        return 409
    return 500


def save_weekday_tasks(config_dir, weekday, tasks):
    if weekday not in WEEKDAYS:
        raise ValueError("Invalid weekday")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("No tasks received")
    path = weekday_task_path(Path(config_dir), weekday)
    if not save_tasks(tasks, path):
        raise OSError(f"Failed to save {path}")
    return path


def copy_weekday_tasks(config_dir, source, targets):
    if source not in WEEKDAYS:
        raise ValueError("Invalid source weekday")
    unique_targets = list(dict.fromkeys(targets or []))
    if not unique_targets or any(target not in WEEKDAYS for target in unique_targets):
        raise ValueError("Invalid target weekdays")
    source_path = weekday_task_path(Path(config_dir), source)
    valid, error = validate_task_file(source_path)
    if not valid:
        raise ValueError(f"Source weekday config is invalid: {error}")
    source_content = source_path.read_text(encoding="utf-8")
    for target in unique_targets:
        if target != source:
            atomic_write_text(weekday_task_path(Path(config_dir), target), source_content)
    return [target for target in unique_targets if target != source]

# ─── stage API ───
def fetch_stages():
    data, warning = get_activity()
    _stage_cache.update(data=data, ts=datetime.now().timestamp(), error=warning)
    return data

def _parse_activity_time(value, tz_offset=8):
    tz = timezone(timedelta(hours=tz_offset))
    return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=tz)

def _cli_activity_time(value):
    return str(value or "").replace("/", "-").replace(" ", "T")

def _stage_value(stage):
    return stage.get("Value") or stage.get("value") or stage.get("Display") or stage.get("display") or ""

def _stage_display(stage):
    return stage.get("Display") or stage.get("display") or _stage_value(stage)

def _load_json(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except Exception:
        return None

def _load_item_names(resource_root=RESOURCE_DIR):
    data = _load_json(Path(resource_root) / "item_index.json")
    if not isinstance(data, dict):
        return {}
    return {
        str(item_id): item["name"].strip()
        for item_id, item in data.items()
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].strip()
    }

def _display_drop_material(drop, item_names):
    if not isinstance(drop, str) or not drop.isdecimal():
        return drop
    return item_names.get(drop) or drop

def _supply_prefix(code):
    return code.rsplit("-", 1)[0]


def _supply_sort_key(code):
    prefix_order = {
        "CE": 0, "LS": 1, "AP": 2, "CA": 3, "SK": 4,
        "PR-A": 5, "PR-B": 6, "PR-C": 7, "PR-D": 8,
    }
    prefix = _supply_prefix(code)
    try:
        level = int(code.rsplit("-", 1)[1])
    except ValueError:
        level = 0
    return prefix_order.get(prefix, 99), level, code


def _load_common_stage_catalog(resource_root=RESOURCE_DIR):
    resource_root = Path(resource_root)
    source_paths = (
        resource_root / "stages.json",
        resource_root / "item_index.json",
        resource_root / "tasks" / "Stages" / "Supplies.json",
    )
    signatures = []
    for path in source_paths:
        try:
            stat = path.stat()
            signatures.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            signatures.append((str(path), 0, 0))
    cache_key = tuple(signatures)
    if _common_stage_cache["key"] == cache_key:
        return _common_stage_cache["data"]

    stages = _load_json(resource_root / "stages.json")
    item_names = _load_item_names(resource_root)
    supplies = _load_json(resource_root / "tasks" / "Stages" / "Supplies.json")
    supported = (
        sorted(
            (code for code in supplies if isinstance(code, str) and SUPPLY_STAGE_RE.fullmatch(code)),
            key=_supply_sort_key,
        )
        if isinstance(supplies, dict)
        else list(FALLBACK_SUPPLY_STAGE_CODES)
    )
    if not supported:
        supported = list(FALLBACK_SUPPLY_STAGE_CODES)
    by_code = {}
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict) or not isinstance(stage.get("code"), str):
            continue
        by_code.setdefault(stage["code"], stage)

    result = []
    for code in supported:
        prefix = _supply_prefix(code)
        group, fallback_drop, weekdays = SUPPLY_PREFIX_META.get(
            prefix, ("资源关卡", "", ())
        )
        stage = by_code.get(code) or {}
        names = []
        for drop in stage.get("dropInfos") or []:
            if not isinstance(drop, dict) or drop.get("dropType") != "NORMAL_DROP":
                continue
            name = item_names.get(str(drop.get("itemId", "")))
            if name and name not in names:
                names.append(name)
        result.append({
            "value": code,
            "group": group,
            "drop": " / ".join(names) or fallback_drop,
            "open_weekdays": list(weekdays),
        })

    main_meta = {
        "Annihilation": ("剿灭", ""),
        "1-7": ("主线", "固源岩"),
        "R8-11": ("主线", "晶体元件"),
        "12-17-HARD": ("主线", "化合切削液"),
        "JT8-3": ("主线", "酮凝集组"),
    }
    for code in COMMON_MAIN_STAGE_CODES:
        group, fallback_drop = main_meta[code]
        resource_code = COMMON_STAGE_RESOURCE_ALIASES.get(code, code)
        stage = by_code.get(resource_code) or {}
        names = []
        for drop in stage.get("dropInfos") or []:
            if not isinstance(drop, dict) or drop.get("dropType") != "NORMAL_DROP":
                continue
            name = item_names.get(str(drop.get("itemId", "")))
            if name and name not in names:
                names.append(name)
        result.append({
            "value": code,
            "group": group,
            "drop": " / ".join(names) or fallback_drop,
            "open_weekdays": [],
        })
    _common_stage_cache["key"] = cache_key
    _common_stage_cache["data"] = result
    return result


def _load_common_stage_drops(resource_root=RESOURCE_DIR):
    return {
        item["value"]: item["drop"]
        for item in _load_common_stage_catalog(resource_root)
        if item.get("drop")
    }

def _resource_version(path):
    data = _load_json(Path(path) / "version.json")
    if not isinstance(data, dict):
        return {}
    return {
        "activity": data.get("activity", {}).get("name", ""),
        "gacha": data.get("gacha", {}).get("pool", ""),
        "last_updated": data.get("last_updated", ""),
    }

def _activity_sort_key(activity_id):
    nums = [int(x) for x in re.findall(r"\d+", activity_id or "")]
    return nums[0] if nums else -1

def _stage_sort_key(stage):
    code = stage.get("Value", "")
    nums = [int(x) for x in re.findall(r"\d+", code)]
    special = 0
    if "-TR-" in code:
        special = -1
    elif "-P-" in code:
        special = 50
    elif "-EX-" in code:
        special = 100
    return (special, nums[0] if nums else 999, code)

def _resource_stage_groups(resource_root, source):
    overview = Path(resource_root) / "Arknights-Tile-Pos" / "overview.json"
    data = _load_json(overview)
    if not isinstance(data, dict):
        return []

    groups = {}
    for item in data.values():
        if not isinstance(item, dict):
            continue
        level_id = item.get("levelId", "")
        if not level_id.startswith("activities/"):
            continue
        code = item.get("code", "")
        if not code:
            continue
        parts = level_id.split("/")
        if len(parts) < 2:
            continue
        activity_id = parts[1]
        group = groups.setdefault(activity_id, {
            "key": f"{source}:{activity_id}",
            "activity_id": activity_id,
            "source": source,
            "tip": "来自本地资源索引，活动 API 未必已同步；MAA 导航支持需以 tasks/Stages 为准",
            "name": activity_id,
            "days_left": None,
            "stages": [],
            "_prefixes": set(),
        })
        prefix = code.split("-", 1)[0]
        group["_prefixes"].add(prefix)
        group["stages"].append({
            "Value": code,
            "Display": f"{code} {item.get('name', '')}".strip(),
            "Drop": "",
        })

    result = []
    for group in groups.values():
        group["stages"].sort(key=_stage_sort_key)
        prefixes = sorted(group.pop("_prefixes"))
        if len(prefixes) == 1:
            group["name"] = f"{prefixes[0]} ({group['activity_id']})"
        else:
            group["name"] = f"{'/'.join(prefixes)} ({group['activity_id']})"
        group["prefixes"] = prefixes
        result.append(group)
    result.sort(key=lambda g: _activity_sort_key(g["activity_id"]), reverse=True)
    return result

def _stage_prefixes_from_tasks(resource_root):
    stage_dir = Path(resource_root) / "tasks" / "Stages"
    if not stage_dir.is_dir():
        return set()
    return {
        p.stem
        for p in stage_dir.glob("*.json")
        if p.stem not in {"base", "Supplies"}
    }

def get_stage_catalog():
    """Get active stages plus diagnostics and local resource-index candidates."""
    data = fetch_stages()
    item_names = _load_item_names(RESOURCE_DIR)
    official = data.get("Official", {})
    ss = official.get("sideStoryStage", {})
    now = datetime.now(timezone(timedelta(hours=8)))
    active = []
    expired = []
    for key, info in ss.items():
        act = info.get("Activity", {})
        stages_list = info.get("Stages", [])
        try:
            tz_offset = act.get("TimeZone", 8)
            start = _parse_activity_time(act["UtcStartTime"], tz_offset)
            end = _parse_activity_time(act["UtcExpireTime"], tz_offset)
            item = {
                "key": key,
                "tip": act.get("Tip", ""),
                "name": act.get("StageName", key),
                "days_left": (end - now).days,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "start_cli": _cli_activity_time(act.get("UtcStartTime", "")),
                "end_cli": _cli_activity_time(act.get("UtcExpireTime", "")),
                "timezone": tz_offset,
                "source": "stage-api",
                "stages": [{
                    "Value": _stage_value(s),
                    "Display": _stage_display(s),
                    "Drop": _display_drop_material(s.get("Drop", ""), item_names),
                } for s in stages_list]
            }
            if start <= now <= end:
                active.append(item)
            else:
                expired.append(item)
        except:
            pass

    clone_groups = _resource_stage_groups(RESOURCE_CLONE_DIR, "resource-index")
    runtime_groups = _resource_stage_groups(RESOURCE_DIR, "runtime-resource")
    runtime_activity_ids = {g.get("activity_id") for g in runtime_groups}
    runtime_stage_prefixes = _stage_prefixes_from_tasks(RESOURCE_DIR)
    # 官方活动 API 的关卡也标注运行资源是否真有导航支持：仅凭活动时间窗判定“开放”
    # 不代表 MAA 能导航进去（历史 CF-8 类 Fight 失败）。
    for item in active + expired:
        prefixes = {
            str(s.get("Value", "")).split("-", 1)[0]
            for s in item.get("stages") or []
            if s.get("Value")
        }
        item["runtime_navigable"] = bool(prefixes & runtime_stage_prefixes)
    resource_candidates = []
    for group in clone_groups[:RESOURCE_STAGE_GROUP_LIMIT]:
        group = dict(group)
        group["runtime_synced"] = group.get("activity_id") in runtime_activity_ids
        prefixes = set(group.get("prefixes") or [])
        group["runtime_navigable"] = bool(prefixes & runtime_stage_prefixes)
        resource_candidates.append(group)

    versions = {
        "runtime": _resource_version(RESOURCE_DIR),
        "clone": _resource_version(RESOURCE_CLONE_DIR),
    }
    warnings = []
    if _stage_cache.get("error"):
        warnings.append(f"活动 API 获取失败: {_stage_cache['error']}")
    if not active and expired:
        newest = max(expired, key=lambda x: x.get("end", ""))
        warnings.append(f"官方活动 API 当前没有开放活动；最近过期活动是 {newest.get('name')}，结束于 {newest.get('end')}")
    if versions["clone"].get("last_updated") and versions["runtime"].get("last_updated"):
        if versions["clone"]["last_updated"] != versions["runtime"]["last_updated"]:
            warnings.append(
                "MaaResource clone 与运行资源版本不同；更新脚本会尝试同步到运行资源目录"
            )
    if resource_candidates and not active:
        warnings.append("本地 MaaResource 索引存在较新的活动关卡候选，但官方活动 API 尚未展示开放活动")

    return {
        "active": active,
        "expired": expired[:5],
        "resource_candidates": resource_candidates,
        "common_drops": _load_common_stage_drops(RESOURCE_DIR),
        "versions": versions,
        "warnings": warnings,
    }

# ─── system helpers ───
def get_maa_version():
    try:
        r = subprocess.run([MAA_BIN, "version"], capture_output=True, text=True, timeout=10, env=get_env())
        return r.stdout.strip()
    except: return "unknown"

def get_adb_status():
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10, env=get_env())
        return "device" in r.stdout
    except: return False

def get_logs(n=50):
    try:
        logs = sorted(Path(LOG_DIR).glob("maa-*.log"), key=os.path.getmtime, reverse=True)
        if not logs: return "", ""
        latest = logs[0]
        lines = latest.read_text("utf-8", errors="replace").splitlines()
        return latest.name, "\n".join(lines[-n:])
    except: return "", ""

def get_maacore_log(n=40):
    try:
        f = os.path.expanduser("~/.local/state/maa/debug/asst.log")
        if os.path.exists(f):
            lines = Path(f).read_text("utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
    except: pass
    return ""

def read_startup_health_state():
    data = {
        "status": "unknown",
        "reason": "",
        "screen": "unknown",
        "detail": "",
        "time": "",
        "log": "",
        "screenshot": "",
        "failure_reason": "",
        "exists": False,
    }
    try:
        path = Path(STARTUP_HEALTH_STATE_FILE)
        if not path.exists():
            return data
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
        data["exists"] = True
        try:
            data["age_seconds"] = max(0, int(datetime.now().timestamp() - path.stat().st_mtime))
        except Exception:
            data["age_seconds"] = None
    except Exception as e:
        data["status"] = "error"
        data["reason"] = str(e)
    return data

def get_crontab():
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5, env=get_env())
        return r.stdout.strip() if r.returncode == 0 else ""
    except: return ""

def run_task_bg(task_name, weekday=None, infrast_now=False):
    global running_task
    if weekday is not None and weekday not in WEEKDAYS:
        return False
    if infrast_now and (task_name != "daily" or weekday is not None):
        return False
    with run_lock:
        if running_task["active"]: return False
    def worker():
        global running_task
        with run_lock:
            running_task = {"active": True, "task": task_name,
                            "weekday": weekday or "",
                            "start_time": datetime.now().strftime("%H:%M:%S"),
                            "pid": None, "output": ""}
        try:
            cmd = ["/bin/bash", f"{SCRIPT_DIR}/maa-run.sh", task_name, "--debug"]
            if weekday is not None:
                cmd.extend(["--weekday", weekday])
            if infrast_now:
                cmd.append("--infrast-now")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=get_env())
            with run_lock: running_task["pid"] = proc.pid
            buf = []
            for line in proc.stdout:
                buf.append(line.rstrip())
                if len(buf) > 300: buf = buf[-300:]
                with run_lock: running_task["output"] = "\n".join(buf)
            proc.wait()
            with run_lock:
                running_task["output"] += f"\n\n--- 结束 (exit={proc.returncode}) ---"
                running_task["active"] = False
        except Exception as e:
            with run_lock:
                running_task["output"] += f"\n--- 异常: {e} ---"
                running_task["active"] = False
    threading.Thread(target=worker, daemon=True).start()
    return True


def run_operator_scan_bg(config_dir=CONFIG_DIR):
    global running_task
    with run_lock:
        if running_task["active"]:
            return False
    _ensure_operator_scan_task(config_dir)

    def worker():
        global running_task
        with run_lock:
            running_task = {
                "active": True,
                "task": "干员扫描",
                "weekday": "",
                "start_time": datetime.now().strftime("%H:%M:%S"),
                "pid": None,
                "output": "开始受控干员扫描...\n",
            }
        return_code = None
        try:
            proc = subprocess.Popen(
                [
                    "python3",
                    f"{SCRIPT_DIR}/maa-operator-scan.py",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=get_env(),
            )
            with run_lock:
                running_task["pid"] = proc.pid
            recent = []
            for line in proc.stdout:
                clean = line.rstrip()
                recent.append(clean)
                if len(recent) > 300:
                    recent = recent[-300:]
                with run_lock:
                    running_task["output"] = "\n".join(recent)
            return_code = proc.wait()
            if return_code != 0:
                raise OperatorBoxError(
                    "runner_failed",
                    "$",
                    f"扫描执行失败，exit={return_code}",
                )
            suffix = "\n\n--- 干员扫描完成，快照已更新 ---"
        except Exception as error:
            message = (
                error.message
                if isinstance(error, OperatorBoxError)
                else "扫描结果处理失败"
            )
            suffix = f"\n\n--- 干员扫描未完成：{message} ---"
        with run_lock:
            running_task["output"] += suffix
            if return_code is not None:
                running_task["output"] += f"\n--- 结束 (exit={return_code}) ---"
            running_task["active"] = False

    threading.Thread(target=worker, daemon=True).start()
    return True


# ═══════════════════════════════════════
# Cultivation goals (service module owned by maa_cultivation_service.py)
# ═══════════════════════════════════════
_CULTIVATION_SERVICE = None
_CULTIVATION_SERVICE_ERROR = None


class CultivationRequestError(ValueError):
    """Malformed cultivation request body; carries its own HTTP status/code."""

    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


CULTIVATION_CONFLICT_CODES = {
    "conflict",
    "duplicate",
    "already_exists",
    "goal_exists",
    "stale",
    "locked",
    "busy",
}


def cultivation_service():
    global _CULTIVATION_SERVICE, _CULTIVATION_SERVICE_ERROR
    if _CULTIVATION_SERVICE is not None:
        return _CULTIVATION_SERVICE
    if _CULTIVATION_SERVICE_ERROR is not None:
        raise _CULTIVATION_SERVICE_ERROR
    try:
        import maa_cultivation_service as service
    except Exception as error:
        _CULTIVATION_SERVICE_ERROR = error
        raise
    _CULTIVATION_SERVICE = service
    return service


def cultivation_error_status(error):
    """Map a service ValueError to 400 (bad request) or 409 (conflict).

    The service may attach a status or code attribute to its ValueError, or
    prefix the message with "conflict:" / "409:". Everything else is a bad
    request.
    """
    status = getattr(error, "status", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return status
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in CULTIVATION_CONFLICT_CODES:
        return 409
    message = str(error)
    lowered = message.lower()
    if lowered.startswith("conflict") or lowered.startswith("409"):
        return 409
    if any(
        token in message
        for token in (
            "冲突",
            "已存在",
            "重复",
            "已被占用",
            "已被其他操作修改",
            "正在运行",
            "刷新后重试",
        )
    ):
        return 409
    return 400


def cultivation_error_payload(error, status):
    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = "conflict" if status == 409 else "invalid_request"
    message = str(error)
    for prefix in ("conflict:", "409:"):
        if message.lower().startswith(prefix):
            message = message[len(prefix):].strip() or str(error)
            break
    return {"ok": False, "error": {"code": code, "message": message}}


def cultivation_dispatch(action, body=None):
    """Call the cultivation service and translate its result to (status, json).

    Request errors are ValueError; the service may signal a conflict with a
    code/status attribute or a "conflict:" message prefix, otherwise the error
    maps to 400. A missing service module maps to 503 so the page can say the
    feature is not deployed yet.
    """
    try:
        service = cultivation_service()
    except Exception:
        return 503, {
            "ok": False,
            "error": {
                "code": "service_unavailable",
                "message": "培养目标服务尚未部署",
            },
        }
    try:
        if action == "payload":
            result = service.get_payload(CONFIG_DIR, CULTIVATION_CATALOG_FILE)
        elif action == "settings":
            result = service.update_settings(
                CONFIG_DIR, CULTIVATION_CATALOG_FILE, body
            )
        elif action == "goal":
            result = service.save_goal(CONFIG_DIR, CULTIVATION_CATALOG_FILE, body)
        elif action == "goal-state":
            result = service.set_goal_state(
                CONFIG_DIR, CULTIVATION_CATALOG_FILE, body
            )
        elif action == "balance":
            confirm_balance = getattr(service, "confirm_balance", None)
            if not callable(confirm_balance):
                return 503, {
                    "ok": False,
                    "error": {
                        "code": "service_unavailable",
                        "message": "龙门币余额确认接口尚未部署",
                    },
                }
            result = confirm_balance(CONFIG_DIR, CULTIVATION_CATALOG_FILE, body)
        elif action == "mastery-settings":
            update_mastery_settings = getattr(service, "update_mastery_settings", None)
            if not callable(update_mastery_settings):
                return 503, {
                    "ok": False,
                    "error": {
                        "code": "service_unavailable",
                        "message": "技能专精设置接口尚未部署",
                    },
                }
            result = update_mastery_settings(CONFIG_DIR, CULTIVATION_CATALOG_FILE, body)
        elif action == "mastery-plan":
            save_mastery_plan = getattr(service, "save_mastery_plan", None)
            if not callable(save_mastery_plan):
                return 503, {
                    "ok": False,
                    "error": {
                        "code": "service_unavailable",
                        "message": "技能专精计划接口尚未部署",
                    },
                }
            result = save_mastery_plan(CONFIG_DIR, CULTIVATION_CATALOG_FILE, body)
        elif action == "mastery-plan-state":
            set_mastery_plan_state = getattr(service, "set_mastery_plan_state", None)
            if not callable(set_mastery_plan_state):
                return 503, {
                    "ok": False,
                    "error": {
                        "code": "service_unavailable",
                        "message": "技能专精状态接口尚未部署",
                    },
                }
            result = set_mastery_plan_state(CONFIG_DIR, CULTIVATION_CATALOG_FILE, body)
        else:
            raise ValueError("unsupported cultivation action")
    except ValueError as error:
        status = cultivation_error_status(error)
        return status, cultivation_error_payload(error, status)
    except Exception:
        return 500, {
            "ok": False,
            "error": {"code": "server_error", "message": "培养服务暂时不可用"},
        }
    if not isinstance(result, dict):
        result = {"ok": True, "result": result}
    return 200, result


# ═══════════════════════════════════════
# Medicine plan (service module owned by maa_medicine_service.py)
# ═══════════════════════════════════════
_MEDICINE_SERVICE = None
_MEDICINE_SERVICE_ERROR = None


def medicine_service():
    global _MEDICINE_SERVICE, _MEDICINE_SERVICE_ERROR
    if _MEDICINE_SERVICE is not None:
        return _MEDICINE_SERVICE
    if _MEDICINE_SERVICE_ERROR is not None:
        raise _MEDICINE_SERVICE_ERROR
    try:
        import maa_medicine_service as service
    except Exception as error:
        _MEDICINE_SERVICE_ERROR = error
        raise
    _MEDICINE_SERVICE = service
    return service


def medicine_error_payload(error, status):
    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = "service_unavailable" if status == 503 else (
            "server_error" if status >= 500 else "invalid_request"
        )
    return {"ok": False, "error": {"code": code, "message": str(error)}}


def medicine_dispatch(action, body=None):
    """Call the existing read-only/settings medicine service."""
    try:
        service = medicine_service()
    except Exception:
        return 503, {
            "ok": False,
            "error": {
                "code": "service_unavailable",
                "message": "理智药服务尚未部署",
            },
        }
    try:
        if action == "payload":
            result = service.get_payload(CONFIG_DIR)
        elif action == "settings":
            result = service.update_settings(CONFIG_DIR, body)
        else:
            raise ValueError("unsupported medicine action")
    except ValueError as error:
        return 400, medicine_error_payload(error, 400)
    except Exception:
        return 500, {
            "ok": False,
            "error": {"code": "server_error", "message": "理智药服务暂时不可用"},
        }
    if not isinstance(result, dict):
        result = {"ok": True, "result": result}
    if action == "settings":
        result = {"ok": True, **result}
    return 200, result


def cultivation_run_gate():
    """Return (status, payload) when run mode must not start, else None.

    Run mode is only allowed while the feature switch is on; scanning stays
    available with the switch off so the page can refresh inventory first.
    """
    try:
        service = cultivation_service()
    except Exception:
        return 503, {
            "ok": False,
            "error": {
                "code": "service_unavailable",
                "message": "培养目标服务尚未部署",
            },
        }
    try:
        payload = service.get_payload(CONFIG_DIR, CULTIVATION_CATALOG_FILE)
    except ValueError as error:
        status = cultivation_error_status(error)
        return status, cultivation_error_payload(error, status)
    except Exception:
        return 500, {
            "ok": False,
            "error": {"code": "server_error", "message": "培养服务暂时不可用"},
        }
    settings = (payload or {}).get("settings") or {}
    if not settings.get("enabled"):
        return 409, {
            "ok": False,
            "error": {
                "code": "disabled",
                "message": "培养刷取未启用：请先打开“启用培养刷取”并保存设置",
            },
        }
    return None


def cultivation_mastery_run_gate():
    """Return (status, payload) when mastery run mode has no active uncompleted plans, else None."""
    try:
        service = cultivation_service()
    except Exception:
        return 503, {
            "ok": False,
            "error": {
                "code": "service_unavailable",
                "message": "技能专精服务尚未部署",
            },
        }
    try:
        payload = service.get_payload(CONFIG_DIR, CULTIVATION_CATALOG_FILE)
    except ValueError as error:
        status = cultivation_error_status(error)
        return status, cultivation_error_payload(error, status)
    except Exception:
        return 500, {
            "ok": False,
            "error": {"code": "server_error", "message": "培养服务暂时不可用"},
        }
    mastery = (payload or {}).get("mastery") or {}
    plans = mastery.get("plans") or []
    active_plans = [
        p for p in plans
        if isinstance(p, dict) and p.get("enabled", True) and not p.get("completed", False)
    ]
    if not active_plans:
        return 409, {
            "ok": False,
            "error": {
                "code": "disabled",
                "message": "没有待执行的技能专精计划：请先添加或启用至少一个未完成的专精计划",
            },
        }
    return None


def run_cultivation_bg(mode):
    """Start the cultivation runner through the shared WebUI task lock."""
    global running_task
    if mode not in ("scan", "run", "mastery"):
        return False
    label = "库存识别" if mode == "scan" else ("技能专精" if mode == "mastery" else "培养刷取")
    # Claim the slot inside the same lock that guards the check so two
    # concurrent requests cannot both start a worker.
    with run_lock:
        if running_task["active"]:
            return False
        running_task = {
            "active": True,
            "task": label,
            "weekday": "",
            "start_time": datetime.now().strftime("%H:%M:%S"),
            "pid": None,
            "output": f"开始{label}...\n",
        }

    def worker():
        global running_task
        try:
            proc = subprocess.Popen(
                ["python3", CULTIVATION_RUNNER, "--mode", mode],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=get_env(),
            )
            with run_lock:
                running_task["pid"] = proc.pid
            recent = []
            for line in proc.stdout:
                recent.append(line.rstrip())
                if len(recent) > 300:
                    recent = recent[-300:]
                with run_lock:
                    running_task["output"] = "\n".join(recent)
            return_code = proc.wait()
            suffix = f"\n\n--- {label}结束 (exit={return_code}) ---"
        except Exception as error:
            suffix = f"\n\n--- {label}未完成：{error} ---"
        with run_lock:
            running_task["output"] += suffix
            running_task["active"] = False

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception as error:
        with run_lock:
            running_task["output"] += f"\n\n--- {label}线程启动失败：{error} ---"
            running_task["active"] = False
        return False
    return True


def startup_health_bg():
    global running_task
    with run_lock:
        if running_task["active"]: return False
    def worker():
        global running_task
        with run_lock:
            running_task = {"active": True, "task": "启动健康检查",
                            "start_time": datetime.now().strftime("%H:%M:%S"),
                            "pid": None, "output": "开始启动健康检查...\n"}
        try:
            proc = subprocess.Popen(
                ["/bin/bash", f"{SCRIPT_DIR}/maa-run.sh", "--startup-health-check", "--debug"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=get_env())
            with run_lock: running_task["pid"] = proc.pid
            buf = []
            for line in proc.stdout:
                buf.append(line.rstrip())
                if len(buf) > 300: buf = buf[-300:]
                with run_lock: running_task["output"] = "\n".join(buf)
            proc.wait()
            with run_lock:
                running_task["output"] += f"\n\n--- 结束 (exit={proc.returncode}) ---"
                running_task["active"] = False
        except Exception as e:
            with run_lock:
                running_task["output"] += f"\n异常: {e}"
                running_task["active"] = False
    threading.Thread(target=worker, daemon=True).start()
    return True

def update_maa_bg():
    global running_task
    with run_lock:
        if running_task["active"]: return False
    def worker():
        global running_task
        with run_lock:
            running_task = {"active": True, "task": "更新MAA",
                            "start_time": datetime.now().strftime("%H:%M:%S"),
                            "pid": None, "output": "开始更新...\n"}
        try:
            env = get_env()
            env["MAA_SELF_UPDATE"] = "1"
            proc = subprocess.Popen(
                ["/bin/bash", f"{SCRIPT_DIR}/maa-auto-update.sh"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            with run_lock: running_task["pid"] = proc.pid
            buf = []
            for line in proc.stdout:
                buf.append(line.rstrip())
                if len(buf) > 300: buf = buf[-300:]
                with run_lock: running_task["output"] = "\n".join(buf)
            proc.wait()
            with run_lock:
                running_task["output"] += f"\n\n--- 结束 (exit={proc.returncode}) ---"
                running_task["active"] = False
        except Exception as e:
            with run_lock:
                running_task["output"] += f"\n异常: {e}"
                running_task["active"] = False
    threading.Thread(target=worker, daemon=True).start()
    return True


# ═══════════════════════════════════════
# HTML - single giant template
# ═══════════════════════════════════════
def get_html():
    return r'''<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MAA WebUI</title>
<script>
(() => {
  let saved = null;
  try { saved = localStorage.getItem('maa-webui-theme'); } catch (_) {}
  const preferred = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.dataset.theme = saved === 'light' || saved === 'dark' ? saved : preferred;
})();
</script>
<style>
:root{--bg:#0c0e14;--s1:#141722;--s2:#1c2030;--s3:#252a3a;--bd:#2e3450;--t1:#e8ecf4;--t2:#8890a8;--ac:#5b7fff;--ac2:#4466ee;--gn:#4ade80;--rd:#f87171;--yl:#fbbf24;--or:#fb923c;--r:10px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--t1);line-height:1.5;overflow-x:hidden}
.hdr{background:linear-gradient(135deg,#161b2e,#1e2440);border-bottom:1px solid var(--bd);padding:12px 20px;display:flex;align-items:center;gap:14px}
.hdr h1{font-size:18px;font-weight:600}.hdr h1 em{color:var(--ac);font-style:normal}
.hdr .ver{color:var(--t2);font-size:12px;margin-left:auto}
.nav{display:flex;gap:3px;padding:8px 20px;background:var(--s1);border-bottom:1px solid var(--bd);flex-wrap:wrap}
.nav a{padding:7px 16px;border-radius:7px;text-decoration:none;color:var(--t2);font-size:13px;font-weight:500;transition:.2s}
.nav a:hover{background:var(--s2);color:var(--t1)}.nav a.on{background:var(--ac2);color:#fff}
.wrap{max-width:1200px;margin:0 auto;padding:20px}
.card{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);padding:18px;margin-bottom:16px}
.card h2{font-size:15px;font-weight:600;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:8px}
.row{display:flex;gap:16px;flex-wrap:wrap}
.col-l{flex:0 0 340px;min-width:280px}.col-r{flex:1;min-width:300px}
@media(max-width:750px){.col-l,.col-r{flex:1 1 100%}}
/* task list */
.tl{list-style:none}.tl li{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;cursor:pointer;transition:.15s;border:2px solid transparent}
.tl li:hover{background:var(--s2)}.tl li.sel{background:var(--s2);border-color:var(--ac)}
.tl li .cb{width:18px;height:18px;accent-color:var(--ac);cursor:pointer;flex-shrink:0}
.tl li .tn{flex:1;font-size:14px;user-select:none}.tl li .tn.off{color:var(--t2);text-decoration:line-through}
.tl li .gear{width:28px;height:28px;border:none;background:none;color:var(--t2);cursor:pointer;font-size:16px;border-radius:6px;transition:.15s;display:flex;align-items:center;justify-content:center}
.tl li .gear:hover{background:var(--s3);color:var(--t1)}
.tl li .arrows{display:flex;flex-direction:column;gap:1px}
.tl li .arrows button{border:none;background:none;color:var(--t2);cursor:pointer;font-size:10px;padding:0 3px;line-height:1}
.tl li .arrows button:hover{color:var(--t1)}
.tbar{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
.tbar select,.tbar button{padding:6px 12px;border-radius:6px;font-size:13px;border:1px solid var(--bd);background:var(--s2);color:var(--t1);cursor:pointer}
.tbar button:hover{background:var(--s3)}
.tbar .start{background:var(--ac2);color:#fff;border:none;padding:8px 24px;font-size:14px;font-weight:600;border-radius:8px;margin-top:6px;width:100%}
.tbar .start:hover{background:var(--ac)}.tbar .start:disabled{opacity:.5;cursor:not-allowed}
/* settings panel */
.sp{animation:fadeIn .2s}.sp h3{font-size:14px;color:var(--ac);margin-bottom:12px}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.fg{margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.fg label{font-size:13px;color:var(--t2);min-width:80px}
.fg input[type=number],.fg input[type=text],.fg select{padding:6px 10px;border-radius:6px;border:1px solid var(--bd);background:var(--s3);color:var(--t1);font-size:13px;width:160px}
.fg input[type=checkbox]{accent-color:var(--ac);width:16px;height:16px}
.fg .hint{font-size:11px;color:var(--t2);width:100%;margin-left:90px;margin-top:-4px}
/* activity tip */
.atip{background:linear-gradient(135deg,rgba(91,127,255,.08),rgba(74,222,128,.06));border:1px solid rgba(91,127,255,.2);border-radius:8px;padding:12px 14px;margin-top:12px;font-size:12px;color:var(--t2);line-height:1.7}
.atip b{color:var(--t1)}.atip .tag{display:inline-block;background:var(--ac2);color:#fff;padding:1px 8px;border-radius:4px;font-size:11px;margin-right:6px}
/* status */
.sg{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.si{background:var(--s2);border-radius:8px;padding:12px}.si .lb{font-size:11px;color:var(--t2);margin-bottom:3px}.si .vl{font-size:13px;font-weight:500}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:11px;font-weight:600}
.badge.ok{background:rgba(74,222,128,.15);color:var(--gn)}.badge.err{background:rgba(248,113,113,.15);color:var(--rd)}.badge.run{background:rgba(91,127,255,.15);color:var(--ac)}
.btn-row{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.btn{padding:8px 18px;border:none;border-radius:7px;font-size:13px;font-weight:500;cursor:pointer;transition:.2s;display:inline-flex;align-items:center;gap:5px}
.btn-p{background:var(--ac2);color:#fff}.btn-p:hover{background:var(--ac)}
.btn-s{background:var(--s2);color:var(--t2);border:1px solid var(--bd)}.btn-s:hover{background:var(--s3);color:var(--t1)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.logbox{width:100%;max-height:420px;overflow:auto;padding:12px;background:#080a10;color:#8a92a8;border:1px solid var(--bd);border-radius:8px;font-family:Consolas,monospace;font-size:11px;line-height:1.6;white-space:pre-wrap;word-break:break-all}
textarea{width:100%;min-height:300px;padding:14px;background:#0a0d16;color:#b8c0d6;border:1px solid var(--bd);border-radius:8px;font-family:'JetBrains Mono',Consolas,monospace;font-size:12px;line-height:1.7;resize:vertical}
textarea:focus{outline:none;border-color:var(--ac)}
.msg{padding:10px 14px;border-radius:7px;margin-bottom:14px;font-size:13px}
.msg.ok{background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.25);color:var(--gn)}
.msg.err{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.25);color:var(--rd)}
.tabs{display:flex;gap:2px;margin-bottom:14px}.tabs button{padding:6px 16px;border:1px solid var(--bd);background:var(--s2);color:var(--t2);border-radius:7px 7px 0 0;cursor:pointer;font-size:13px;border-bottom:none}
.tabs button.on{background:var(--s1);color:var(--ac);font-weight:600}
.help{margin-top:10px;padding:10px;background:var(--s2);border-radius:7px;font-size:11px;color:var(--t2);line-height:1.8}
/* infrast plan library */
.infrast-library,.infrast-details{min-width:0;max-width:100%;overflow-wrap:anywhere}
.infrast-library-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:14px}
.infrast-file{background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:14px;min-width:0;text-align:left;color:var(--t1)}
button.infrast-file{cursor:pointer;transition:.15s}button.infrast-file:hover{border-color:var(--ac);background:var(--s3)}button.infrast-file.on{border-color:var(--ac);box-shadow:0 0 0 1px var(--ac)}
.infrast-file .meta,.infrast-plan-meta,.infrast-room-meta{font-size:11px;color:var(--t2);line-height:1.8}
.infrast-file .title{font-size:14px;font-weight:600;margin:4px 0}.infrast-file.invalid{border-color:rgba(248,113,113,.45)}
.infrast-shifts{display:flex;gap:8px;overflow-x:auto;max-width:100%;padding:2px 0 10px;scrollbar-width:thin}
.infrast-shifts .btn{flex:0 0 auto}
.infrast-facility{margin-top:16px}.infrast-facility h3{font-size:13px;color:var(--ac);margin-bottom:8px}
.infrast-room-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.infrast-room{background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:12px;min-width:0}
.infrast-room h4{font-size:12px;margin-bottom:8px}.infrast-room dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:5px 9px;font-size:11px}
.infrast-room dt{color:var(--t2)}.infrast-room dd{min-width:0;overflow-wrap:anywhere}
.infrast-plan-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:14px}
.infrast-option{background:var(--s2);border-radius:8px;padding:12px;font-size:11px;line-height:1.8;min-width:0}
@media(max-width:750px){.infrast-room-grid{grid-template-columns:1fr}.infrast-library-grid,.infrast-plan-options{grid-template-columns:1fr}}
/* Stage C weekly schedule */
.isched-shell{position:relative}.isched-toolbar{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-top:14px}
.isched-field{display:grid;gap:5px;min-width:150px}.isched-field span{font-size:11px;color:var(--t2)}
.isched-field input,.isched-field select{min-height:38px;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--s2);color:var(--t1)}
.isched-choice{display:flex;gap:6px;padding:4px;border:1px solid var(--bd);border-radius:10px;background:var(--s2)}
.isched-choice button{border:0;border-radius:7px;padding:7px 12px;background:transparent;color:var(--t2);cursor:pointer}
.isched-choice button[aria-pressed=true]{background:var(--ac2);color:#fff}
.isched-confirm{display:grid;gap:8px;margin:12px 0}.isched-confirm label{display:flex;gap:8px;align-items:flex-start;font-size:12px;color:var(--t1)}
.isched-alert{padding:10px 12px;border-radius:10px;border:1px solid rgba(248,113,113,.45);background:rgba(248,113,113,.08);color:var(--rd);font-size:12px}
.isched-status{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:10px 0}.isched-badge{border-radius:999px;padding:3px 9px;font-size:11px;border:1px solid var(--bd);color:var(--t2)}
.isched-badge.is-active{border-color:rgba(74,222,128,.4);color:var(--gn);background:rgba(74,222,128,.08)}
.isched-badge.is-warning{border-color:rgba(251,191,36,.4);color:var(--yl);background:rgba(251,191,36,.08)}
.isched-grid-scroll{overflow-x:auto;max-width:100%;border:1px solid var(--bd);border-radius:12px;background:var(--s2);scrollbar-width:thin}
.infrast-schedule-grid{display:grid;grid-template-columns:70px repeat(7,minmax(156px,1fr));min-width:1162px}
.isched-grid-head,.isched-time,.isched-cell{min-width:0;border-right:1px solid var(--bd);border-bottom:1px solid var(--bd)}
.isched-grid-head{position:sticky;top:0;z-index:2;padding:9px;background:#202538;font-size:12px;font-weight:600}
.isched-time{padding:10px 8px;color:var(--t2);font:11px Consolas,monospace;background:#181c2a}
.isched-cell{min-height:96px;padding:7px;background:transparent;color:var(--t1);text-align:left;cursor:pointer}
.isched-cell:hover,.isched-cell:focus-visible{outline:0;background:var(--s3);box-shadow:inset 0 0 0 2px var(--ac)}
.isched-cell strong{display:block;font-size:12px;margin-bottom:4px}.isched-cell small{display:block;color:var(--t2);line-height:1.5}
.isched-empty{padding:28px;text-align:center;color:var(--t2);font-size:12px}
.isched-diff{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:10px}
.isched-diff code{display:block;padding:9px;border-radius:8px;background:#0a0d16;color:var(--t2);font-size:10px;overflow-wrap:anywhere}
.isched-drawer-backdrop{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.45)}
.isched-drawer{position:fixed;z-index:21;right:0;top:0;width:min(520px,92vw);height:100dvh;overflow:auto;padding:20px;background:var(--s1);border-left:1px solid var(--bd);box-shadow:-18px 0 54px rgba(0,0,0,.32)}
.isched-drawer-head{display:flex;align-items:start;justify-content:space-between;gap:12px;margin-bottom:14px}.isched-drawer-head button{flex:0 0 auto}
.isched-adoption{display:grid;gap:7px;margin-top:8px}.isched-adoption label{display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px;align-items:start;font-size:11px}
.isched-adoption code{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--t2)}
.isched-loading{min-height:120px;padding:30px;background:linear-gradient(90deg,var(--s2),var(--s3),var(--s2));background-size:220% 100%;color:var(--t2)}
@media(max-width:750px){.isched-drawer{width:100vw}.isched-toolbar{align-items:stretch}.isched-field{width:100%}.isched-choice{width:100%}.isched-choice button{flex:1}}
@media(prefers-reduced-motion:reduce){.isched-cell,.isched-choice button{transition:none}}
/* 2026 control-console redesign */
:root{
  color-scheme:dark;
  --page-bg:#0a1422;--sidebar-bg:#0c1725;--surface:#101c2c;--surface-raised:#142235;--surface-muted:#19283c;
  --border:#2a3b50;--text:#f4f7fb;--text-muted:#a4b0c1;--accent:#3976f6;--accent-strong:#2862df;
  --accent-soft:#18345f;--code-bg:#08111e;
  --success:#42c477;--danger:#ef6b73;--warning:#e9b64f;--shadow:0 18px 48px rgba(0,0,0,.18);
  --bg:var(--page-bg);--s1:var(--surface);--s2:var(--surface-raised);--s3:var(--surface-muted);--bd:var(--border);
  --t1:var(--text);--t2:var(--text-muted);--ac:var(--accent);--ac2:var(--accent-strong);--gn:var(--success);--rd:var(--danger);--yl:var(--warning);--r:8px;
}
[data-theme="light"]{
  color-scheme:light;
  --page-bg:#f6f8fb;--sidebar-bg:#ffffff;--surface:#ffffff;--surface-raised:#f8fafc;--surface-muted:#eef2f7;
  --border:#d9e0ea;--text:#101722;--text-muted:#637083;--accent:#2f6df6;--accent-strong:#225bd7;
  --accent-soft:#e7efff;--code-bg:#f4f6f9;
  --success:#239a56;--danger:#d44752;--warning:#9d6e13;--shadow:0 18px 48px rgba(20,35,60,.08);
}
html,body{min-height:100%;background:var(--page-bg)}
body{font-family:"Segoe UI","Microsoft YaHei UI",system-ui,-apple-system,sans-serif;letter-spacing:.01em}
button,input,select,textarea{font:inherit}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.app-shell{min-height:100vh;display:grid;grid-template-columns:224px minmax(0,1fr)}
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;background:var(--sidebar-bg);border-right:1px solid var(--border);z-index:10}
.brand{height:88px;display:flex;align-items:center;padding:0 28px;border-bottom:1px solid var(--border);font-size:22px;font-weight:720;letter-spacing:.02em;white-space:nowrap}
.nav{display:flex;flex-direction:column;gap:8px;padding:22px 14px;background:transparent;border:0;flex-wrap:nowrap;overflow:auto}
.nav a{display:block;padding:13px 16px;border-radius:7px;color:var(--text-muted);font-size:15px;font-weight:520;white-space:nowrap}
.nav a:hover{background:var(--surface-raised);color:var(--text)}
.nav a.on{background:var(--accent-strong);color:#fff;box-shadow:0 8px 24px rgba(47,109,246,.24)}
.sidebar-foot{margin-top:auto;padding:18px 28px;color:var(--text-muted);font-size:11px;border-top:1px solid var(--border)}
.workspace{min-width:0}
.topbar{min-height:88px;padding:18px 32px;display:flex;align-items:center;justify-content:space-between;gap:24px;border-bottom:1px solid var(--border);background:var(--page-bg)}
.page-kicker{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.13em;margin-bottom:3px}
.topbar h1{font-size:24px;font-weight:680;line-height:1.2}
.topbar-meta{display:flex;align-items:center;justify-content:flex-end;gap:14px;color:var(--text-muted);font-size:13px;text-align:right}
.theme-toggle{min-width:74px;padding:8px 12px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--text);cursor:pointer}
.theme-toggle:hover{border-color:var(--accent);color:var(--accent)}
.wrap{max-width:1320px;margin:0 auto;padding:28px 32px 48px}
.card{box-shadow:none;padding:20px;border-radius:8px;margin-bottom:18px}
.card h2{font-size:16px;margin-bottom:16px;padding-bottom:12px}
.atip{background:var(--surface-raised);border-color:var(--border)}
.logbox,textarea,.isched-diff code{background:var(--code-bg);color:var(--text-muted)}
[data-theme="light"] .logbox,[data-theme="light"] textarea,[data-theme="light"] .isched-diff code{background:#f4f6f9;color:#39465a}
.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px}
.page-head h2{font-size:28px;line-height:1.2}.page-head p{margin-top:7px;color:var(--text-muted);font-size:13px}
.eyebrow{font-size:13px;color:var(--text-muted);margin-bottom:8px}
.hero-stage{font-size:52px;font-weight:760;line-height:1.08;letter-spacing:-.03em;margin-bottom:10px}
.hero-sub{color:var(--text-muted);font-size:15px}
.hero-actions{margin-top:20px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.hero-actions select{min-height:38px;padding:7px 10px;border:1px solid var(--border);border-radius:7px;background:var(--surface-raised);color:var(--text)}
.week-strip{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));border:1px solid var(--border);border-radius:8px;overflow:hidden}
.week-day{position:relative;min-width:0;padding:16px 10px;text-align:center;background:var(--surface);border:0;border-right:1px solid var(--border);color:var(--text);cursor:pointer}
.week-day:last-child{border-right:0}.week-day:hover{background:var(--surface-raised)}
.week-day.is-today .day-name{color:var(--accent);font-weight:650}
.week-day.is-selected{background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--accent)}
.week-day .day-name,.week-day .day-date{display:block;font-size:12px;color:var(--text-muted)}
.week-day .day-stage{display:block;margin-top:12px;font-size:21px;font-weight:720;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.week-day .day-drop{display:block;margin-top:3px;font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dashboard-grid{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(280px,.7fr);gap:18px}
.shift-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0}
.shift-block{padding:4px 18px 4px 0}.shift-block+.shift-block{padding-left:22px;border-left:1px solid var(--border)}
.shift-label{font-size:12px;color:var(--text-muted)}.shift-name{font-size:30px;font-weight:720;margin-top:8px}.shift-time{font-size:12px;color:var(--text-muted);margin-top:4px}
.status-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--border);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.status-item{padding:16px;background:var(--surface);text-align:center}.status-item strong{display:block;font-size:13px}.status-item span{display:block;margin-top:4px;font-size:11px;color:var(--text-muted)}
.weekly-toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.switch-label{display:flex;align-items:center;gap:10px;color:var(--text-muted);font-size:13px}.switch-label input{width:38px;height:20px;accent-color:var(--accent)}
.weekly-editor{display:grid;grid-template-columns:minmax(260px,.9fr) minmax(320px,1.4fr);gap:0;padding:0;overflow:hidden}
.weekly-editor>section{padding:22px}.weekly-editor>section+section{border-left:1px solid var(--border)}
.section-title{font-size:18px;font-weight:650;margin-bottom:18px;padding-left:12px;border-left:4px solid var(--accent)}
.stage-select{width:100%;min-height:44px;padding:9px 12px;border:1px solid var(--border);border-radius:7px;background:var(--surface-raised);color:var(--text)}
.stage-validation{margin-top:9px;font-size:12px}.stage-validation.open{color:var(--success)}.stage-validation.invalid{color:var(--danger)}.stage-validation.unknown{color:var(--warning)}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.info-box{padding:14px;border:1px solid var(--border);border-radius:7px;background:var(--surface-raised)}.info-box span{display:block;font-size:11px;color:var(--text-muted)}.info-box strong{display:block;margin-top:5px;font-size:14px}
.weekly-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin:18px 0}
.advanced-panel{margin-top:18px}.advanced-panel summary{cursor:pointer;padding:16px 20px;border:1px solid var(--border);border-radius:8px;background:var(--surface);font-weight:600}.advanced-panel[open] summary{border-radius:8px 8px 0 0}.advanced-panel .advanced-body{padding-top:16px}
.isched-overview{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,.8fr);gap:18px}
.isched-days{display:grid;grid-template-columns:repeat(7,1fr);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:20px}
.isched-day{padding:12px 8px;border:0;border-right:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer}.isched-day:last-child{border-right:0}.isched-day.is-selected{color:var(--accent);background:var(--accent-soft);box-shadow:inset 0 -3px var(--accent)}
.isched-timeline{padding:18px 14px 24px;border:1px solid var(--border);border-radius:8px;background:var(--surface-raised);overflow:hidden}
.timeline-scale{display:flex;justify-content:space-between;color:var(--text-muted);font-size:10px}.timeline-track{position:relative;height:112px;margin:12px 2px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.timeline-line{position:absolute;left:0;right:0;top:48px;height:5px;background:var(--accent)}
.timeline-marker{position:absolute;top:41px;width:16px;height:16px;border:3px solid var(--surface);border-radius:50%;background:var(--accent);transform:translateX(-50%);box-shadow:0 0 0 1px var(--accent);cursor:pointer}
.timeline-marker span{position:absolute;top:22px;left:50%;transform:translateX(-50%);width:100px;text-align:center;color:var(--text);font-size:11px}.timeline-marker small{display:block;color:var(--accent);font-size:10px;margin-top:2px}
.personnel-panel{padding:22px}.personnel-panel h3{font-size:25px;color:var(--accent);margin-bottom:4px}.personnel-panel .infrast-facility{margin-top:18px}
.personnel-note{padding:12px;border:1px solid var(--border);border-radius:7px;color:var(--text-muted);font-size:11px;line-height:1.7;margin-top:16px}
.personnel-room{margin-top:14px}.personnel-room h4{font-size:12px;color:var(--text-muted);margin-bottom:7px}.personnel-line{display:flex;align-items:flex-start;gap:8px;margin-top:6px;font-size:12px}.personnel-line>span:first-child{flex:0 0 54px;color:var(--text-muted)}.operator-chips{display:flex;gap:5px;flex-wrap:wrap}.operator-chip{padding:3px 7px;border:1px solid var(--border);border-radius:5px;background:var(--surface-raised);white-space:nowrap}
/* Cultivation goals */
.cult-toolbar{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
.cult-field{display:flex;flex-direction:column;gap:6px;min-width:150px;flex:1 1 150px}
.cult-field label{font-size:12px;color:var(--text-muted)}
.cult-field input[type=text],.cult-field input[type=search],.cult-field select{padding:10px 12px;border:1px solid var(--border);border-radius:7px;background:var(--surface-raised);color:var(--text);font-size:14px;font-family:inherit}
.cult-switch{display:flex;align-items:center;gap:9px;font-size:14px}
.cult-grid{display:grid;grid-template-columns:minmax(300px,1fr) minmax(320px,1.25fr);gap:18px;align-items:start}
.cult-card{border:1px solid var(--border);border-radius:9px;padding:16px;background:var(--surface)}
.cult-section-title{font-size:15px;font-weight:600;margin-bottom:10px}
.cult-goal{border:1px solid var(--border);border-radius:9px;padding:14px;background:var(--surface-raised);margin-top:12px}
.cult-goal.is-completed{opacity:.65}
.cult-goal-head{display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap}
.cult-goal-name{font-size:16px;font-weight:600}
.cult-goal-meta{font-size:12px;color:var(--text-muted);margin-top:4px;line-height:1.8}
.cult-goal-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.cult-tag{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--border);font-size:11px;color:var(--text-muted);margin-left:6px}
.cult-tag.is-ready{border-color:#2eb67d;background:rgba(46,182,125,.15);color:#2eb67d;font-weight:600}
.cult-tag.is-craft{border-color:#38bdf8;background:rgba(56,189,248,.15);color:#38bdf8;font-weight:600}
.cult-tag.is-lack{border-color:#e5484d;background:rgba(229,72,77,.2);color:#ff6b6b;font-weight:700}
.cult-lack-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:6px;background:rgba(229,72,77,.2);border:1px solid #e5484d;color:#ff6b6b;font-weight:700;font-size:12px;margin:2px 6px 2px 0}
.cult-craft-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:6px;background:rgba(56,189,248,.14);border:1px solid rgba(56,189,248,.6);color:#38bdf8;font-weight:600;font-size:12px;margin:2px 6px 2px 0}
.cult-ok-pill{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:6px;background:rgba(46,182,125,.14);border:1px solid rgba(46,182,125,.55);color:#2eb67d;font-weight:600;font-size:12px;margin:2px 6px 2px 0}
.cult-empty{padding:22px;border:1px dashed var(--border);border-radius:9px;color:var(--text-muted);font-size:13px;text-align:center}
.cult-list{margin:8px 0 0;padding-left:18px;font-size:13px;line-height:1.9}
.cult-warn{margin-top:14px;padding:12px 14px;border:1px solid var(--border);border-left:3px solid var(--accent-strong);border-radius:7px;background:var(--surface-raised);font-size:12px;line-height:1.85;color:var(--text-muted)}
.cult-note{font-size:12px;color:var(--text-muted);line-height:1.85}
.cult-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
.cult-error{color:#e5484d;font-size:13px;margin-top:10px}
.cult-result{padding:9px 11px;border-radius:7px;background:var(--surface-raised);border:1px solid var(--border);font-size:12px;margin-top:10px}
.cult-picker{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:10px;margin-top:12px;max-height:430px;overflow:auto}
.cult-pick-card,.cult-mastery-card{display:flex;flex-direction:column;align-items:flex-start;gap:4px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface-raised);color:var(--text);cursor:pointer;text-align:left;font-family:inherit}
.cult-pick-card:hover:not([disabled]),.cult-pick-card.is-selected,.cult-mastery-card:hover:not([disabled]),.cult-mastery-card.is-selected{border-color:var(--accent-strong);box-shadow:0 0 0 1px var(--accent-strong)}
.cult-pick-card[disabled],.cult-mastery-card[disabled]{opacity:.55;cursor:not-allowed}
.cult-pick-head{display:flex;align-items:center;gap:10px;width:100%}
.cult-avatar{width:40px;height:40px;border-radius:8px;object-fit:cover;flex-shrink:0;background:var(--surface);border:1px solid var(--border)}
.cult-pick-name{font-size:14px;font-weight:600}
.cult-pick-meta{font-size:11px;color:var(--text-muted)}
.cult-mat-box{margin-top:10px;padding:12px;border-radius:9px;border:1px solid var(--border);background:var(--surface-raised);font-size:12px;line-height:1.65}
.cult-mat-box.is-ok{border-left:4px solid #2eb67d;background:rgba(46,182,125,.05)}
.cult-mat-box.is-craft{border-left:4px solid #38bdf8;background:rgba(56,189,248,.05)}
.cult-mat-box.is-lack{border:1.5px solid rgba(229,72,77,.65);border-left:5px solid #e5484d;background:rgba(229,72,77,.08)}
.cult-mat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:8px;margin-top:8px}
.cult-mat-item{padding:9px 11px;border-radius:8px;border:1px solid var(--border);background:var(--surface);display:flex;flex-direction:column;gap:4px}
.cult-mat-item.is-lack{border:1.5px solid #e5484d;background:rgba(229,72,77,.14);box-shadow:0 0 0 1px rgba(229,72,77,.2)}
.cult-mat-item.is-craft{border:1px solid rgba(56,189,248,.55);background:rgba(56,189,248,.09)}
.cult-mat-item.is-ok{border:1px solid rgba(46,182,125,.45);background:rgba(46,182,125,.07)}
.cult-mat-item-head{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:13px;font-weight:650}
.cult-mat-badge{padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}
.cult-mat-badge.is-lack{background:#e5484d;color:#fff}
.cult-mat-badge.is-craft{background:rgba(56,189,248,.22);border:1px solid #38bdf8;color:#38bdf8}
.cult-mat-badge.is-ok{background:rgba(46,182,125,.2);border:1px solid #2eb67d;color:#2eb67d}
.cult-mat-item-sub{font-size:11.5px;color:var(--text-muted);line-height:1.45}
.cult-mat-item-lack{font-size:11.5px;color:#ff6b6b;font-weight:700;line-height:1.45}
.cult-step-bar{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);align-items:center}
@media(max-width:900px){.cult-grid{grid-template-columns:1fr}}
@media(max-width:1100px){.week-strip{grid-template-columns:repeat(4,1fr)}.week-day:nth-child(4){border-right:0}.week-day:nth-child(-n+4){border-bottom:1px solid var(--border)}.isched-overview,.dashboard-grid{grid-template-columns:1fr}}
@media(max-width:900px){.app-shell{display:block}.sidebar{position:sticky;height:auto}.brand{height:60px;padding:0 20px}.nav{flex-direction:row;padding:8px 12px;overflow-x:auto;border-top:1px solid var(--border)}.nav a{padding:9px 13px}.sidebar-foot{display:none}.topbar{min-height:72px;padding:14px 20px}.wrap{padding:22px 20px 40px}}
@media(max-width:640px){.topbar{align-items:flex-start}.topbar-meta{align-items:flex-end;flex-direction:column;gap:7px}.page-kicker{display:none}.topbar h1{font-size:20px}.wrap{padding:18px 14px 36px}.page-head{display:block}.page-head h2{font-size:24px}.hero-stage{font-size:38px}.week-strip{grid-template-columns:repeat(2,1fr)}.week-day{border-bottom:1px solid var(--border)}.week-day:nth-child(even){border-right:0}.week-day:nth-child(7){grid-column:1/-1}.dashboard-grid,.weekly-editor,.info-grid{grid-template-columns:1fr}.weekly-editor>section+section{border-left:0;border-top:1px solid var(--border)}.shift-summary,.status-row{grid-template-columns:1fr}.shift-block+.shift-block{padding:16px 0 0;border-left:0;border-top:1px solid var(--border)}.status-item{text-align:left}.isched-days{grid-template-columns:repeat(4,1fr)}.isched-day{border-bottom:1px solid var(--border)}.isched-day:nth-child(4){border-right:0}.isched-day:nth-child(7){grid-column:3/5}.hero-actions,.weekly-actions{align-items:stretch;flex-direction:column}.hero-actions .btn,.weekly-actions .btn,.hero-actions select{width:100%;justify-content:center}}
</style>
</head>
<body>

<div class="app-shell">
  <aside class="sidebar">
    <div class="brand">MAA 控制台</div>
    <nav class="nav" aria-label="主导航">
      <a href="#" onclick="go('home');return false" id="n_home">首页</a>
      <a href="#" onclick="go('weekly');return false" id="n_weekly">周计划</a>
      <a href="#" onclick="go('infrast-plans');return false" id="n_infrast-plans">基建排班</a>
      <a href="#" onclick="go('cultivation');return false" id="n_cultivation">培养目标</a>
      <a href="#" onclick="go('tasks');return false" id="n_tasks">任务配置</a>
      <a href="#" onclick="go('logs');return false" id="n_logs">日志</a>
      <a href="#" onclick="go('cron');return false" id="n_cron">定时</a>
      <a href="#" onclick="go('conn');return false" id="n_conn">设置</a>
    </nav>
    <div class="sidebar-foot">MAA <span id="ver"></span></div>
  </aside>
  <section class="workspace">
    <header class="topbar">
      <div><div class="page-kicker">MAA 自动化</div><h1 id="pageTitle">任务配置</h1></div>
      <div class="topbar-meta"><span id="dateContext"></span><button class="theme-toggle" id="themeToggle" onclick="toggleTheme()">浅色</button></div>
    </header>
    <main class="wrap" id="main"></main>
  </section>
</div>

<script>
// ═══ State ═══
let S = {
  tasks: [],           // task list from server
  dailyTasks: [],
  sel: -1,             // selected task index
  stages: [],          // active event stages
  commonStages: [],    // fast local catalog for permanent/supply stages
  resourceStages: [],  // resource JSON candidates (opt-in display)
  showResourceStages: false,
  stageDiag: null,      // stage API/resource diagnostics
  status: null,        // running task status
  version: '',
  adb: false,
  page: 'tasks',
  weeklyPlan: {enabled:false, initialized:false, days:[], current_weekday:'mon', selection:{}},
  weeklyDay: '',
  weeklyDays: {},
  weeklyDirty: false,
  weeklySaving: false,
  weeklyBusy: false,
  runPending: false,
  weeklyAdvancedOpen: false,
  runWeekday: 'current',
  infrastTab: 'gen',
  infrastPlans: [],
  infrastSelected: null,
  infrastSelectedPlan: 0,
  infrastLibraryError: '',
  infrastPlansLoaded: false,
  operatorBox: {loaded:false, busy:false, data:null, error:'', weeklyPreview:null, confirmedConflicts:false},
  medicine: {loaded:false, loading:false, data:null, error:'', draft:{enabled:false, threshold:80}, draftDirty:false, saving:false},
  infrastImport: {open:false, source:'file', filename:'', content:'', preview:null, busy:false, replaceConfirmed:false, error:''},
  infrastSchedule: {
    filename: '', analysis: null, details: null, mode: 'preserve', days: {},
    preview: null, busy: false, selectedCell: null, drawerOpen: false,
    inferenceConfirmed: false, anchorDate: '', anchorTime: '02:00',
    enableWeeklyPlan: false, adoptedLineHashes: [],
    unsafeDailyBlocked: false, previewRequestFingerprint: '',
    restorePreview: null, commitConfirmed: false, restoreConfirmed: false,
    cron: null, manifest: null, manifestCurrent: false, weekly: null, error: ''
  },
  rogueTab: 'gen',
  cultivation: {
    loaded: false, loading: false, data: null, error: '', notice: '',
    draft: null, settingsDraft: null, settingsDirty: false,
    saving: false, settingsSaving: false, runPending: false, fetchSeq: 0,
    operatorQuery: '', operatorOpen: false
  }
};

// ═══ Constants ═══
const TASK_TYPES = [
  {type:'StartUp', name:'开始唤醒', icon:'🌅'},
  {type:'Recruit', name:'自动公招', icon:'📋'},
  {type:'Infrast', name:'基建换班', icon:'🏗️'},
  {type:'Fight', name:'理智作战', icon:'⚔️'},
  {type:'Mall', name:'信用收支', icon:'🛒'},
  {type:'Award', name:'领取奖励', icon:'🎁'},
  {type:'CloseDown', name:'关闭游戏', icon:'🔌'},
  {type:'Roguelike', name:'自动肉鸽', icon:'🎲'},
  {type:'Reclamation', name:'生息演算', icon:'🌿'},
  {type:'Copilot', name:'自动抄作业', icon:'📄'},
  {type:'SSSCopilot', name:'自动保全', icon:'🛡️'},
  {type:'ParadoxCopilot', name:'悖论模拟', icon:'🔮'},
];

const FACILITIES = ['Trade','Reception','Mfg','Control','Power','Office','Dorm'];
const FACILITY_MAP = {
  'Mfg': '制造站',
  'Trade': '贸易站',
  'Control': '控制中枢',
  'Power': '发电站',
  'Reception': '会客室',
  'Office': '办公室',
  'Dorm': '宿舍'
};
const WEEKDAYS = [
  {v:'Mon',n:'周一'},{v:'Tue',n:'周二'},{v:'Wed',n:'周三'},{v:'Thu',n:'周四'},
  {v:'Fri',n:'周五'},{v:'Sat',n:'周六'},{v:'Sun',n:'周日'}
];
const PLAN_WEEKDAYS = [
  {v:'mon',n:'周一'},{v:'tue',n:'周二'},{v:'wed',n:'周三'},{v:'thu',n:'周四'},
  {v:'fri',n:'周五'},{v:'sat',n:'周六'},{v:'sun',n:'周日'}
];
const INFRAST_FACILITY_ORDER = [
  ['control', '控制中枢'], ['manufacture', '制造站'],
  ['trading', '贸易站'], ['power', '发电站'],
  ['meeting', '会客室'], ['hire', '办公室'],
  ['processing', '加工站'], ['dormitory', '宿舍']
];

const FALLBACK_COMMON_STAGES = [
  {group:'龙门币/经验', items:['CE-5','CE-6','LS-5','LS-6']},
  {group:'红票/技能/碳', items:['AP-5','CA-5','SK-5']},
  {group:'芯片', items:['PR-A-1','PR-A-2','PR-B-1','PR-B-2','PR-C-1','PR-C-2','PR-D-1','PR-D-2']},
  {group:'剿灭', items:['Annihilation']},
  {group:'主线', items:['1-7','R8-11','12-17-HARD','JT8-3']},
];

const PAGE_META = {
  home: '首页', weekly: '周计划', 'infrast-plans': '周基建排班',
  cultivation: '干员培养目标',
  tasks: '任务配置', logs: '日志', cron: '定时任务', conn: '设置'
};
let stageLoadPromise = null;
const STAGE_OPEN_WEEKDAYS = {
  'CE-5':['tue','thu','sat','sun'], 'CE-6':['tue','thu','sat','sun'],
  'LS-5':['mon','thu','sat','sun'], 'LS-6':['mon','thu','sat','sun'],
  'AP-5':['mon','thu','sat','sun'], 'CA-5':['tue','wed','fri','sun'],
  'SK-5':['mon','wed','fri','sat'], 'PR-A-1':['mon','thu','fri','sun'],
  'PR-A-2':['mon','thu','fri','sun'], 'PR-B-1':['mon','tue','fri','sat'],
  'PR-B-2':['mon','tue','fri','sat'], 'PR-C-1':['wed','thu','sat','sun'],
  'PR-C-2':['wed','thu','sat','sun'], 'PR-D-1':['tue','wed','sat','sun'],
  'PR-D-2':['tue','wed','sat','sun']
};

function toggleTheme() {
  const current = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('maa-webui-theme', next); } catch (_) {}
  updateThemeLabel();
}

function updateThemeLabel() {
  const button = document.getElementById('themeToggle');
  if (button) button.textContent = document.documentElement.dataset.theme === 'light' ? '深色' : '浅色';
}

function formatCalendarDate(date, withYear=false) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
  const options = withYear
    ? {year:'numeric', month:'long', day:'numeric', weekday:'long'}
    : {month:'numeric', day:'numeric'};
  return new Intl.DateTimeFormat('zh-CN', options).format(date);
}

function updateShellContext() {
  const title = PAGE_META[S.page] || 'MAA 控制台';
  const titleElement = document.getElementById('pageTitle');
  const dateElement = document.getElementById('dateContext');
  if (titleElement) titleElement.textContent = title;
  if (dateElement) dateElement.textContent = formatCalendarDate(new Date(), true);
  document.title = `${title} · MAA 控制台`;
  updateThemeLabel();
}

function taskName(type) {
  const t = TASK_TYPES.find(x => x.type === type);
  return t ? t.name : type;
}
function taskIcon(type) {
  const t = TASK_TYPES.find(x => x.type === type);
  return t ? t.icon : '📌';
}

// ═══ API ═══
async function api(path, opts) {
  const r = await fetch('/api/' + path, opts);
  return r.json();
}
async function apiResponse(path, options) {
  const response = await fetch('/api/' + path, options);
  let data;
  try { data = await response.json(); }
  catch (_) { data = {ok:false, error:{code:'invalid_response', message:'服务返回无效响应'}}; }
  return {status: response.status, ok: response.ok, data};
}
function applyStageCatalog(stages) {
  S.stageDiag = stages || {};
  S.stages = [...((stages && stages.active) || [])];
  S.resourceStages = [...((stages && stages.resource_candidates) || [])];
}

function loadStageCatalogInBackground() {
  if (stageLoadPromise) return stageLoadPromise;
  stageLoadPromise = api('stages')
    .then(stages => {
      applyStageCatalog(stages);
      if (S.page === 'home' || (S.page === 'weekly' && !S.weeklyDirty)) render();
    })
    .catch(() => {})
    .finally(() => { stageLoadPromise = null; });
  return stageLoadPromise;
}

async function loadAll() {
  const [cfg, status, weekly, infrastPlans, medicine] = await Promise.all([
    api('config'), api('status'), api('weekly-plan').catch(() => null),
    api('infrast/plans').catch(() => null),
    api('medicine-plan').catch(error => ({ok:false, error:{code:'load_failed', message:error.message || '读取理智药计划失败'}}))
  ]);
  S.dailyTasks = cfg.tasks || [];
  S.commonStages = Array.isArray(cfg.common_stages) ? cfg.common_stages : [];
  const keepWeeklyDraft = S.page === 'weekly' && (S.weeklyDirty || S.weeklySaving || S.weeklyBusy);
  if (!keepWeeklyDraft) {
    if (weekly && Array.isArray(weekly.days)) applyWeeklyPayload(weekly);
    S.tasks = S.page === 'weekly' ? weeklyDraft(S.weeklyDay) : S.dailyTasks;
  }
  S.version = cfg.version || '';
  S.adb = cfg.adb || false;
  S.status = status;
  if (medicine && medicine.ok === false) {
    S.medicine.error = medicineErrorMessage(medicine);
    S.medicine.loaded = !!S.medicine.data;
  } else if (medicine && typeof medicine === 'object') {
    applyMedicinePayload(medicine);
  }
  if (infrastPlans && Array.isArray(infrastPlans.plans)) {
    S.infrastPlans = infrastPlans.plans;
    S.infrastPlansLoaded = true;
    const activePlan = infrastPlans.plans.find(plan => plan.schedule_active === true);
    if (activePlan && activePlan.filename) {
      await preloadInfrastSchedule(activePlan.filename);
    }
  }
  document.getElementById('ver').textContent = S.version;
  if (S.sel >= S.tasks.length) S.sel = S.tasks.length - 1;
  loadStageCatalogInBackground();
}

function visibleStageGroups() {
  return [...S.stages, ...(S.showResourceStages ? S.resourceStages : [])];
}
function setShowResourceStages(on) {
  S.showResourceStages = !!on;
  render();
}

async function saveTasks() {
  const r = await api('config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tasks: S.tasks})
  });
  if (r.ok) showMsg('ok', '配置已保存 ✓');
  else showMsg('err', '保存失败: ' + (r.error || ''));
}

// ═══ Render ═══
async function go(page) {
  if (S.runPending) return;
  if (S.page === 'weekly' && !(await resolveWeeklyDraft())) return;
  S.page = page;
  S.tasks = page === 'weekly' ? weeklyDraft(S.weeklyDay) : S.dailyTasks;
  document.querySelectorAll('.nav a').forEach(a => a.classList.remove('on'));
  const el = document.getElementById('n_' + page);
  if (el) el.classList.add('on');
  updateShellContext();
  render();
  if (page === 'infrast-plans') {
    if (!S.infrastPlansLoaded) await loadInfrastPlans();
    if (!S.operatorBox.loaded) await loadOperatorBox();
  }
  if (page === 'cultivation') {
    cultivationStartPolling();
    if (!S.cultivation.loaded) await loadCultivation();
  } else {
    cultivationStopPolling();
  }
}

function showMsg(cls, txt) {
  const d = document.createElement('div');
  d.className = 'msg ' + cls;
  d.textContent = txt;
  const main = document.getElementById('main');
  main.insertBefore(d, main.firstChild);
  setTimeout(() => d.remove(), 3000);
}

function render() {
  const m = document.getElementById('main');
  switch(S.page) {
    case 'home': m.innerHTML = renderHome(); break;
    case 'tasks': m.innerHTML = renderTasks(); bindTaskEvents(); break;
    case 'weekly': m.innerHTML = renderWeeklyPlan(); bindTaskEvents(); break;
    case 'infrast-plans': m.innerHTML = renderInfrastPlans(); break;
    case 'cultivation': m.innerHTML = renderCultivation(); bindCultivationEvents(); break;
    case 'logs': m.innerHTML = renderLogs(); loadLogs(); break;
    case 'cron': m.innerHTML = renderCron(); loadCron(); break;
    case 'conn': m.innerHTML = renderConn(); loadConn(); break;
  }
}

// ─── Home ───
function currentPlanWeekday(date=new Date()) {
  return PLAN_WEEKDAYS[(date.getDay() + 6) % 7].v;
}

function gamePlanCalendarDate(instant=new Date()) {
  // CN game date: UTC+8, with a 04:00 reset. Return a local calendar date for display.
  const shifted = new Date(instant.getTime() + (8 - 4) * 60 * 60 * 1000);
  return new Date(shifted.getUTCFullYear(), shifted.getUTCMonth(), shifted.getUTCDate(), 12);
}

function gamePlanDateKey(instant=new Date()) {
  const date = gamePlanCalendarDate(instant);
  const pad = value => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`;
}

let weeklyGameDayKey = '';
function refreshWeeklyGameDay(force=false) {
  updateShellContext();
  const gameDate = gamePlanCalendarDate();
  const next = currentPlanWeekday(gameDate);
  const gameDateKey = gamePlanDateKey(gameDate);
  const previous = S.weeklyPlan.current_weekday || next;
  if (!force && weeklyGameDayKey === gameDateKey) return;
  weeklyGameDayKey = gameDateKey;
  S.weeklyPlan.current_weekday = next;
  if (S.page === 'weekly' || S.page === 'home') {
    if (!S.weeklyDirty && !S.weeklySaving && !S.weeklyBusy
        && S.page === 'weekly' && (!S.weeklyDay || S.weeklyDay === previous)) {
      S.weeklyDay = next;
      S.tasks = weeklyDraft(next);
      S.sel = S.tasks.length ? 0 : -1;
    }
    render();
  }
}
if (typeof setInterval === 'function') setInterval(refreshWeeklyGameDay, 60000);

function renderWeekStrip({clickable=false, selected='', preferConfigured=true}={}) {
  const today = S.weeklyPlan.current_weekday || currentPlanWeekday(gamePlanCalendarDate());
  return `<div class="week-strip">${PLAN_WEEKDAYS.map(day => {
    const date = dateForPlanWeekday(day.v);
    const summary = getWeekdayStageSummary(day.v, date, preferConfigured);
    const classes = ['week-day', day.v===today?'is-today':'', day.v===selected?'is-selected':''].filter(Boolean).join(' ');
    const attrs = clickable ? `type="button" onclick="switchWeeklyDay('${day.v}')"` : 'type="button" disabled';
    const detail = [summary.checksAnnihilation && !isAnnihilationStage(summary.stage) ? '先检查剿灭' : '', summary.drop || summary.availability.label].filter(Boolean).join(' · ');
    return `<button class="${classes}" ${attrs}><span class="day-name">${esc(day.n)}${day.v===today?' · 今天':''}</span><span class="day-date">${esc(formatCalendarDate(date))}</span><span class="day-stage">${esc(stageDisplayLabel(summary.stage))}</span><span class="day-drop">${esc(detail)}</span></button>`;
  }).join('')}</div>`;
}

function getInfrastShiftSummary(date=new Date()) {
  const state = S.infrastSchedule || {};
  if (!state.manifestCurrent) return {active:false,current:null,next:null};
  const weekday = currentPlanWeekday(date);
  const slots = [...((state.days && state.days[weekday]) || [])]
    .filter(slot => timeToMinutes(slot.time) !== null)
    .sort((left,right) => timeToMinutes(left.time) - timeToMinutes(right.time));
  if (!slots.length) return {active:true,current:null,next:null};
  const nowMinutes = date.getHours() * 60 + date.getMinutes();
  let currentIndex = -1;
  for (let index = slots.length - 1; index >= 0; index -= 1) {
    if (timeToMinutes(slots[index].time) <= nowMinutes) {
      currentIndex = index;
      break;
    }
  }
  if (currentIndex < 0) currentIndex = slots.length - 1;
  const current = slots[currentIndex];
  const next = slots[(currentIndex + 1) % slots.length];
  return {active:true,current,next};
}

function renderHome() {
  const st = S.status || {};
  const skip = st.skip_next_daily || {active:false,scheduled_for:''};
  const sh = st.startup_health || {};
  const taskState = st.active ? `${esc(st.task)} · ${esc(st.start_time || '')}` : '任务空闲';
  const healthClass = sh.status === 'ok' ? 'ok' : (sh.status === 'failed' || sh.status === 'error' ? 'err' : (sh.status === 'running' ? 'run' : ''));
  const healthText = sh.exists
    ? `${esc(sh.status || 'unknown')}${sh.reason ? ' / ' + esc(sh.reason) : ''}${sh.screen && sh.screen !== 'unknown' ? ' / ' + esc(sh.screen) : ''}`
    : '暂无';
  const wp = S.weeklyPlan || {};
  const selection = wp.selection || {};
  const todayWeekday = wp.current_weekday || currentPlanWeekday();
  const todayName = planWeekdayName(todayWeekday);
  const todaySummary = getWeekdayStageSummary(todayWeekday, new Date(), !!wp.enabled);
  let weeklyMode = wp.enabled ? `周计划已启用，今天运行${todayName}配置` : '周计划未启用，本次运行 daily.toml';
  if (wp.enabled && selection.fallback_reason) weeklyMode = `${todayName}配置异常，本次将回退 daily.toml`;
  const annihilationNote = todaySummary.checksAnnihilation && !isAnnihilationStage(todaySummary.stage) ? ' · 先检查剿灭，满额自动跳过' : '';
  const shift = getInfrastShiftSummary();
  const runOptions = [`<option value="current">按当前规则</option>`].concat(
    PLAN_WEEKDAYS.map(day => `<option value="${day.v}" ${S.runWeekday===day.v?'selected':''}>${day.n}（仅本次测试）</option>`)
  ).join('');
  let h = `<section class="card"><div class="eyebrow">今天</div><div class="hero-stage">今天刷 ${esc(stageDisplayLabel(todaySummary.stage))}</div>
    <div class="hero-sub">${esc(weeklyMode)}${annihilationNote}${todaySummary.drop?' · '+esc(todaySummary.drop):''}</div>
    <div class="hero-actions"><button class="btn btn-p" onclick="doRun(S.runWeekday)" ${st.active?'disabled':''}>运行今日日常</button><button class="btn btn-s" onclick="go('weekly')">编辑今日计划</button>
    <button class="btn btn-s" onclick="toggleSkipNextDaily()">${skip.active?'已跳过下次日常 · 撤销':'跳过下次日常'}</button>
    <label class="manual-weekday"><select aria-label="运行配置" onchange="S.runWeekday=this.value">${runOptions}</select></label></div></section>`;
  if (skip.active) {
    const target = skip.scheduled_for ? new Date(skip.scheduled_for).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '下一次定时';
    h += `<div class="info-box"><span>一次性跳过已启用</span><strong>${esc(target)}</strong><div class="infrast-plan-meta">只跳过该次定时日常；资源更新和手动运行不受影响。</div></div>`;
  }
  h += `<section class="card"><h2>本周计划</h2>${renderWeekStrip({preferConfigured:!!wp.enabled})}</section>`;
  h += `<div class="dashboard-grid"><section class="card"><h2>基建班次</h2><div class="shift-summary">
    <div class="shift-block"><div class="shift-label">当前班次</div><div class="shift-name">${esc(shift.current ? shift.current.name : (shift.active?'暂无班次':'未启用'))}</div><div class="shift-time">${shift.current?esc(shift.current.time):'使用基础任务配置中的 Infrast'}</div></div>
    <div class="shift-block"><div class="shift-label">下一班</div><div class="shift-name">${esc(shift.next ? shift.next.name : '—')}</div><div class="shift-time">${shift.next?esc(shift.next.time):'独立排班未接管'}</div></div>
  </div>${shift.active?'<div class="hero-actions"><button class="btn btn-s" onclick="go(\'infrast-plans\')">查看周基建排班</button></div>':''}</section>
  <section class="card"><h2>运行状态</h2><div class="status-row"><div class="status-item"><strong>${S.adb?'设备在线':'设备离线'}</strong><span>ADB 连接</span></div><div class="status-item"><strong>${taskState}</strong><span>当前任务</span></div><div class="status-item"><strong class="${healthClass}">${healthText}</strong><span>启动检查</span></div></div></section></div>`;
  h += `<details class="advanced-panel"><summary>维护操作</summary><div class="advanced-body card"><div class="btn-row">
    <button class="btn btn-s" onclick="doStartupHealth()" ${st.active?'disabled':''}>启动健康检查</button>
    <button class="btn btn-s" onclick="doUpdate()" ${st.active?'disabled':''}>更新 MAA</button>
    <button class="btn btn-s" onclick="doGame('start')" ${st.active?'disabled':''}>启动游戏</button>
    <button class="btn btn-s" onclick="doGame('stop')" ${st.active?'disabled':''}>关闭游戏</button>
    <button class="btn btn-s" onclick="doKill()" ${!st.active?'disabled':''} style="color:var(--rd);border-color:var(--rd)">停止任务</button>
  </div></div></details>`;
  if (st.active) {
    h += `<div class="card"><h2>运行输出</h2><div class="logbox" id="liveout"></div></div>`;
    setTimeout(pollOutput, 500);
  }
  return h;
}

async function doRun(weekday='current') {
  if (S.runPending || S.weeklyBusy || S.weeklySaving) return false;
  if (S.page === 'weekly' && !(await resolveWeeklyDraft())) return false;
  const body = {task:'daily'};
  if (weekday && weekday !== 'current') body.weekday = weekday;
  S.runPending = true;
  S.weeklyBusy = S.page === 'weekly';
  if (S.weeklyBusy) render();
  let errorMessage = '';
  try {
    const result = await api('run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (!result.ok) throw new Error(result.error || '服务未接受运行请求');
    await loadAll();
    S.runPending = false;
    S.weeklyBusy = false;
    await go('home');
    return true;
  } catch (error) {
    errorMessage = '运行请求未确认: ' + error.message;
    return false;
  } finally {
    S.runPending = false;
    S.weeklyBusy = false;
    if (S.page === 'weekly') render();
    if (errorMessage) showMsg('err', errorMessage);
  }
}
async function doUpdate() {
  await api('update', {method:'POST'});
  await loadAll(); go('home');
}
async function doStartupHealth() {
  await api('startup-health', {method:'POST'});
  await loadAll(); go('home');
}
async function doGame(act) {
  const r = await api('game', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action:act})});
  if (r.ok) showMsg('ok', (act==='start'?'游戏启动成功':'游戏已关闭') + ' ✓');
  else showMsg('err', '操作失败: ' + (r.error || ''));
}
async function doKill() {
  if (!confirm('确认强制停止当前运行中的 MAA 挂机任务吗？')) return;
  const r = await api('kill', {method:'POST'});
  if (r.ok) showMsg('ok', '任务已终止 ✓');
  else showMsg('err', '终止失败');
  await loadAll(); go('home');
}
async function toggleSkipNextDaily() {
  const current = (S.status && S.status.skip_next_daily) || {active:false};
  const action = current.active ? 'cancel-skip-next-daily' : 'skip-next-daily';
  const prompt = current.active
    ? '撤销跳过，让下一次定时日常正常运行？'
    : '确认只跳过下一次 02:00 / 10:00 / 16:00 定时日常吗？资源更新和手动运行不受影响。';
  if (!confirm(prompt)) return;
  const r = await api('run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})});
  if (!r.ok) {
    showMsg('err', '操作失败: ' + (r.error || ''));
    return;
  }
  await loadAll();
  render();
  showMsg('ok', current.active ? '已撤销，下次日常将正常运行 ✓' : '已设置：跳过下一次定时日常 ✓');
}
async function pollOutput() {
  if (S.page !== 'home') return;
  const st = await api('status');
  S.status = st;
  const el = document.getElementById('liveout');
  if (el) { el.textContent = st.output || '等待...'; el.scrollTop = el.scrollHeight; }
  if (st.active) setTimeout(pollOutput, 2000);
  else { await loadAll(); go('home'); }
}

function planWeekdayName(weekday) {
  const day = PLAN_WEEKDAYS.find(item => item.v === weekday);
  return day ? day.n : weekday;
}

function weekdayToken(weekday) {
  const index = PLAN_WEEKDAYS.findIndex(day => day.v === weekday);
  return index >= 0 ? WEEKDAYS[index].v : '';
}

function dateForPlanWeekday(weekday, anchor=new Date()) {
  const index = PLAN_WEEKDAYS.findIndex(day => day.v === weekday);
  if (index < 0 || Number.isNaN(anchor.getTime())) return new Date(anchor);
  const date = gamePlanCalendarDate(anchor);
  // Each weekday previews its next game date, rather than a past day of this week.
  const currentIndex = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() + (index - currentIndex + 7) % 7);
  return date;
}

function parseConditionDateTime(value) {
  if (!value) return null;
  const normalized = String(value).replace(' ', 'T');
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dateTimeConditionMatches(condition, date) {
  const start = parseConditionDateTime(condition && condition.start);
  const end = parseConditionDateTime(condition && condition.end);
  if (!start || !end || !(date instanceof Date) || Number.isNaN(date.getTime())) return false;
  return date >= start && date <= end;
}

function getFightStageForDate(task, weekday, date=dateForPlanWeekday(weekday)) {
  if (!task || task.type !== 'Fight') return {stage:'', source:'none'};
  const variants = Array.isArray(task.variants) ? task.variants : [];
  const dateTimeMatches = variants.filter(variant => {
    const condition = variant.condition || {};
    return condition.type === 'DateTime' && typeof (variant.params || {}).stage === 'string'
      && dateTimeConditionMatches(condition, date);
  });
  if (dateTimeMatches.length) {
    const winner = dateTimeMatches[dateTimeMatches.length - 1];
    return {stage:winner.params.stage, source:'activity', condition:winner.condition};
  }
  const token = weekdayToken(weekday);
  const weekdayMatches = variants.filter(variant => {
    const condition = variant.condition || {};
    return condition.type === 'Weekday' && Array.isArray(condition.weekdays)
      && condition.weekdays.includes(token) && typeof (variant.params || {}).stage === 'string';
  });
  if (weekdayMatches.length) {
    const winner = weekdayMatches[weekdayMatches.length - 1];
    return {stage:winner.params.stage, source:'weekday', condition:winner.condition};
  }
  const baseMatches = variants.filter(variant => !variant.condition
    && typeof (variant.params || {}).stage === 'string');
  if (baseMatches.length) {
    const winner = baseMatches[baseMatches.length - 1];
    return {stage:winner.params.stage, source:'base'};
  }
  return {stage:String((task.params || {}).stage || ''), source:'base'};
}

function stageGroupFor(stage) {
  const groups = [
    ...(S.stages || []), ...(S.resourceStages || []),
    ...((S.stageDiag && S.stageDiag.expired) || [])
  ];
  return groups.find(group => (group.stages || []).some(item => item.Value === stage)) || null;
}

function stageDisplayLabel(stage) {
  return stage === 'Annihilation' ? '剿灭' : stage;
}

function commonStageEntries() {
  if (Array.isArray(S.commonStages) && S.commonStages.length) return S.commonStages;
  return FALLBACK_COMMON_STAGES.flatMap(group => group.items.map(value => ({
    value, group:group.group, drop:'', open_weekdays:STAGE_OPEN_WEEKDAYS[value] || []
  })));
}

function commonStageEntry(stage) {
  return commonStageEntries().find(item => item.value === stage) || null;
}

function stageDropLabel(stage) {
  const group = stageGroupFor(stage);
  const item = group && (group.stages || []).find(candidate => candidate.Value === stage);
  if (item && item.Drop) return String(item.Drop);
  const commonDrops = (S.stageDiag && S.stageDiag.common_drops) || {};
  if (commonDrops[stage]) return String(commonDrops[stage]);
  const common = commonStageEntry(stage);
  if (common && common.drop) return String(common.drop);
  const labels = {
    'CE-6':'龙门币', 'LS-6':'作战记录', 'AP-5':'采购凭证',
    'CA-5':'技能概要', 'SK-5':'碳素', '1-7':'固源岩'
  };
  return labels[stage] || '';
}

function getStageAvailability(stage, weekday, date=dateForPlanWeekday(weekday)) {
  if (!stage) return {state:'unknown', label:'开放状态未知'};
  const common = commonStageEntry(stage);
  const openWeekdays = common && Array.isArray(common.open_weekdays) && common.open_weekdays.length
    ? common.open_weekdays : STAGE_OPEN_WEEKDAYS[stage];
  if (openWeekdays) {
    return openWeekdays.includes(weekday)
      ? {state:'open', label:'已校验：当天开放'}
      : {state:'closed', label:'当天未开放'};
  }
  // A catalog activity open now may have ended by the weekday being edited.
  const window = findActivityWindowForStage(stage);
  if (window) {
    const start = parseConditionDateTime(window.start);
    const end = parseConditionDateTime(window.end);
    if (start && date < start) return {state:'closed', label:'所选日期活动尚未开放'};
    if (end && date > end) return {state:'expired', label:'所选日期活动已结束'};
  }
  const status = activityStageStatus(stage);
  if (status && status.state === 'expired') return {state:'expired', label:'活动已结束'};
  if (status && status.state === 'active') {
    return status.navigable
      ? {state:'open', label:'已校验：活动开放且可导航'}
      : {state:'unverified', label:'活动开放；专用导航配置未确认，可选择'};
  }
  const group = stageGroupFor(stage);
  if (group && group.source !== 'resource-index') {
    const start = parseConditionDateTime(group.start_cli || group.start);
    const end = parseConditionDateTime(group.end_cli || group.end);
    if (start && end && (date < start || date > end)) return {state:'expired', label:'活动已结束'};
    return group.runtime_navigable === false
      ? {state:'unsupported', label:'运行资源不可导航'}
      : {state:'open', label:'已校验：活动开放且可导航'};
  }
  const alwaysOpen = new Set(['Annihilation','1-7','R8-11','12-17-HARD','JT8-3']);
  if (alwaysOpen.has(stage)) return {state:'open', label:'已校验：常驻关卡'};
  return {state:'unknown', label:'开放状态未知'};
}

function renderCommonStageOptions(currentStage, weekday) {
  const groups = new Map();
  commonStageEntries().forEach(item => {
    const group = item.group || '其他';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(item);
  });
  let html = '';
  groups.forEach((items, group) => {
    html += `<optgroup label="${esc(group)}">`;
    items.forEach(item => {
      const availability = getStageAvailability(item.value, weekday);
      const selected = item.value === currentStage ? ' selected' : '';
      const disabled = isInvalidStageAvailability(availability) ? ' disabled' : '';
      const suffix = availability.state === 'closed' ? '（当天未开放）' : availability.state === 'unverified' ? '（导航待确认）' : availability.state === 'unsupported' ? '（不可导航）' : availability.state === 'expired' ? '（已结束）' : '';
      const drop = item.drop || stageDropLabel(item.value);
      const label = `${stageDisplayLabel(item.value)}${drop ? ' · ' + drop : ''}${suffix}`;
      html += `<option value="${esc(item.value)}"${selected}${disabled}>${esc(label)}</option>`;
    });
    html += '</optgroup>';
  });
  return html;
}

function isAnnihilationStage(stage) {
  const value = String(stage || '');
  return value === 'Annihilation' || value.endsWith('@Annihilation');
}

function getWeeklyFarmFightIndex(tasks, weekday, date=dateForPlanWeekday(weekday)) {
  const enabled = (tasks || []).map((task, index) => ({task,index}))
    .filter(item => item.task.type === 'Fight' && item.task._enabled !== false);
  if (!enabled.length) return -1;
  const named = enabled.find(item => String(item.task.name || '').trim() === '理智作战');
  if (named) return named.index;
  const regular = enabled.find(item => !isAnnihilationStage(getFightStageForDate(item.task, weekday, date).stage));
  return regular ? regular.index : enabled[0].index;
}

function hasWeeklyAnnihilationFight(tasks, weekday, date=dateForPlanWeekday(weekday)) {
  return (tasks || []).some(task => task.type === 'Fight' && task._enabled !== false
    && isAnnihilationStage(getFightStageForDate(task, weekday, date).stage));
}

function getWeekdayStageSummary(weekday, date=dateForPlanWeekday(weekday), preferConfigured=true) {
  const weeklyTasks = S.page === 'weekly' && S.weeklyDirty && weekday === S.weeklyDay
    ? S.tasks : S.weeklyDays && S.weeklyDays[weekday];
  const tasks = preferConfigured && Array.isArray(weeklyTasks) && weeklyTasks.length ? weeklyTasks : (S.dailyTasks || []);
  const fightIndex = getWeeklyFarmFightIndex(tasks, weekday, date);
  const task = fightIndex >= 0 ? tasks[fightIndex] : null;
  if (!task) return {stage:'未设置', drop:'', task:'无作战任务', source:'none', availability:{state:'unknown',label:'开放状态未知'}};
  const resolved = getFightStageForDate(task, weekday, date);
  const stage = resolved.stage || '当前/上次关卡';
  return {
    stage, drop:stageDropLabel(stage), task:task.name || taskName(task.type),
    source:resolved.source, checksAnnihilation:hasWeeklyAnnihilationFight(tasks, weekday, date),
    availability:getStageAvailability(stage, weekday, date)
  };
}

function isInvalidStageAvailability(availability) {
  return ['closed','expired','unsupported'].includes((availability || {}).state);
}

function markTaskDirty() {
  if (S.page === 'weekly') S.weeklyDirty = true;
}

/* __WEEKLY_EDITOR_SCRIPT__ */

/* __CULTIVATION_UI_SCRIPT__ */

// ─── Infrast plan library ───
function emptyInfrastSchedule(filename='') {
  return {
    filename, analysis:null, details:null, mode:'preserve', days:{},
    preview:null, busy:false, selectedCell:null, drawerOpen:false,
    inferenceConfirmed:false, anchorDate:'', anchorTime:'02:00',
    enableWeeklyPlan:false, adoptedLineHashes:[],
    unsafeDailyBlocked:false, previewRequestFingerprint:'',
    restorePreview:null, commitConfirmed:false, restoreConfirmed:false,
    cron:null, manifest:null, manifestCurrent:false, weekly:null, error:''
  };
}

function scheduleSlot(slot) {
  return {
    time:String(slot.time || ''), plan_index:Number(slot.plan_index),
    logical_date:String(slot.logical_date || ''),
    name:String(slot.name || `班次 ${Number(slot.plan_index)+1}`),
    source_time:String(slot.source_time || slot.time || ''),
    source:String(slot.source || 'manual'),
    merge_status:String(slot.merge_status || '待合并'),
    validation_badge:String(slot.validation_badge || '有效')
  };
}

function localIsoDate() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0,10);
}

function buildCyclePreviewDays(analysis, anchorDate, anchorTime) {
  const days = Object.fromEntries(PLAN_WEEKDAYS.map(day => [day.v, []]));
  if (!analysis || !analysis.cross_day_cycle || !/^\d{4}-\d{2}-\d{2}$/.test(anchorDate)) return days;
  const anchorMinutes = timeToMinutes(anchorTime);
  const cycleMinutes = Number(analysis.cycle_hours) * 60;
  const durations = Array.isArray(analysis.durations_hours) ? analysis.durations_hours.map(Number) : [];
  if (anchorMinutes === null || !Number.isInteger(cycleMinutes) || cycleMinutes <= 0 || durations.some(value => !Number.isInteger(value) || value <= 0)) return days;
  const anchor = new Date(anchorDate + 'T00:00:00Z');
  if (Number.isNaN(anchor.getTime())) return days;
  const anchorWeekday = (anchor.getUTCDay() + 6) % 7;
  const monday = new Date(anchor.getTime() - anchorWeekday * 86400000);
  const boundaries = [];
  let elapsed = 0;
  durations.forEach((duration, planIndex) => {
    boundaries.push({minute:elapsed, plan_index:planIndex});
    elapsed += duration * 60;
  });
  const gcd = (left, right) => {
    let a = left;
    let b = right;
    while (b) [a, b] = [b, a % b];
    return a;
  };
  const repeatMinutes = cycleMinutes * 1440 / gcd(cycleMinutes, 1440);
  const triggerTimes = [...new Set(
    Array.from({length:repeatMinutes / cycleMinutes}, (_, cycleIndex) =>
      boundaries.map(boundary =>
        minutesToTime(
          anchorMinutes + cycleIndex * cycleMinutes + boundary.minute
        )
      )
    ).flat()
  )].sort();
  for (let offset=0; offset<7; offset++) {
    const currentDate = new Date(monday.getTime() + offset * 86400000);
    const logicalDate = currentDate.toISOString().slice(0,10);
    const weekday = PLAN_WEEKDAYS[offset].v;
    const dayDelta = Math.round((currentDate - anchor) / 86400000);
    triggerTimes.forEach(time => {
      const minute = timeToMinutes(time);
      if (minute === null) return;
      const relative = dayDelta * 1440 + minute - anchorMinutes;
      const remainder = ((relative % cycleMinutes) + cycleMinutes) % cycleMinutes;
      const boundary = boundaries.find(item => item.minute === remainder);
      if (!boundary) return;
      const sourceSlot = (analysis.slots || []).find(item => Number(item.plan_index) === boundary.plan_index) || {};
      days[weekday].push(scheduleSlot({
        time,
        logical_date:logicalDate,
        plan_index:boundary.plan_index,
        name:sourceSlot.name || `班次 ${boundary.plan_index + 1}`,
        source_time:anchorTime,
        source:'cycle',
        merge_status:'已合并',
        validation_badge:'有效'
      }));
    });
    days[weekday].sort((a,b) => a.time.localeCompare(b.time));
  }
  return days;
}

function validManifestScheduleDays(days, planCount) {
  if (!days || typeof days !== 'object' || Array.isArray(days)) return false;
  const keys = Object.keys(days).sort();
  const expected = PLAN_WEEKDAYS.map(day => day.v).sort();
  if (keys.length !== expected.length || keys.some((key,index) => key !== expected[index])) return false;
  return PLAN_WEEKDAYS.every(day => {
    const slots = days[day.v];
    if (!Array.isArray(slots) || slots.length !== planCount) return false;
    const indexes = new Set();
    const times = new Set();
    for (const slot of slots) {
      if (!slot || typeof slot !== 'object' || Array.isArray(slot)) return false;
      if (!Number.isInteger(slot.plan_index) || slot.plan_index < 0 || slot.plan_index >= planCount) return false;
      if (timeToMinutes(slot.time) === null || timeToMinutes(slot.source_time) === null) return false;
      if (typeof slot.name !== 'string' || !slot.name || !['period','inferred','manual'].includes(slot.source)) return false;
      if (indexes.has(slot.plan_index) || times.has(slot.time)) return false;
      indexes.add(slot.plan_index);
      times.add(slot.time);
    }
    return indexes.size === planCount;
  });
}

function initializeScheduleDays(payload) {
  const state = S.infrastSchedule;
  const manifest = payload.manifest;
  const sourceSlots = (payload.analysis && payload.analysis.slots) || [];
  if (payload.analysis && payload.analysis.cross_day_cycle) {
    const cycle = manifest && manifest.schema_version === 2 && manifest.cycle;
    state.manifestCurrent = !!(
      cycle
      && manifest.enabled === true
      && manifest.filename === state.filename
      && manifest.json_sha256 === payload.analysis.sha256
      && sourceSlots.length > 0
      && sourceSlots.every(slot => slot.merge_status === '已合并')
    );
    state.anchorDate = state.manifestCurrent ? cycle.anchor_date : localIsoDate();
    state.anchorTime = state.manifestCurrent ? cycle.anchor_time : '02:00';
    state.mode = 'replace';
    state.days = buildCyclePreviewDays(
      payload.analysis, state.anchorDate, state.anchorTime
    );
    return;
  }
  const fromManifest = !!(
    manifest
    && manifest.enabled === true
    && manifest.filename === state.filename
    && manifest.json_sha256 === (payload.analysis && payload.analysis.sha256)
    && validManifestScheduleDays(manifest.days, sourceSlots.length)
  ) ? manifest.days : null;
  state.manifestCurrent = !!fromManifest;
  const days = {};
  PLAN_WEEKDAYS.forEach(day => {
    const slots = fromManifest && Array.isArray(fromManifest[day.v])
      ? fromManifest[day.v] : sourceSlots;
    days[day.v] = slots.map(slot => scheduleSlot({
      ...slot,
      merge_status:slot.merge_status || (fromManifest ? '已合并' : '待合并'),
      validation_badge:slot.validation_badge || '有效'
    }));
  });
  state.days = days;
}

function scheduleRequest() {
  const state = S.infrastSchedule;
  const analysis = state.analysis || {};
  const days = {};
  PLAN_WEEKDAYS.forEach(day => {
    days[day.v] = (state.days[day.v] || []).map(slot => ({
      time:String(slot.time || ''),
      plan_index:Number(slot.plan_index),
      name:String(slot.name || `班次 ${Number(slot.plan_index)+1}`),
      source_time:String(slot.source_time || slot.time || ''),
      source:String(slot.source || 'manual')
    }));
  });
  return {
    filename:state.filename,
    sha256:analysis.sha256 || '',
    mode:state.mode,
    anchor_date:state.anchorDate || localIsoDate(),
    anchor_time:state.anchorTime,
    confirmed_inference:state.inferenceConfirmed,
    enable_weekly_plan:state.enableWeeklyPlan,
    days:analysis.cross_day_cycle ? {} : days,
    adopted_line_hashes:[...state.adoptedLineHashes].sort()
  };
}

function scheduleFingerprint(request=scheduleRequest()) {
  return JSON.stringify(request);
}

function invalidateInfrastSchedulePreview() {
  const state = S.infrastSchedule;
  state.preview = null;
  state.previewRequestFingerprint = '';
  state.commitConfirmed = false;
  state.error = '';
}

function applyInfrastSchedulePayload(state, payload) {
  state.analysis = payload.analysis;
  state.details = payload.details;
  state.manifest = payload.manifest;
  state.weekly = payload.weekly;
  state.cron = payload.cron;
  state.inferenceConfirmed = !(payload.analysis && payload.analysis.requires_confirmation);
  state.unsafeDailyBlocked = !!(payload.cron && payload.cron.unsafe_daily && payload.cron.unsafe_daily.blocked);
  initializeScheduleDays(payload);
  if (!(payload.analysis && payload.analysis.cross_day_cycle)) {
    state.mode = state.manifestCurrent && payload.manifest.mode === 'replace' ? 'replace' : 'preserve';
    state.anchorTime = '02:00';
    state.anchorDate = localIsoDate();
  }
  state.enableWeeklyPlan = state.manifestCurrent;
  if (!state.selectedCell) {
    const weekday = currentPlanWeekday();
    const first = (state.days[weekday] || [])[0];
    if (first) state.selectedCell = {weekday, planIndex:first.plan_index};
  }
}

async function preloadInfrastSchedule(filename) {
  if (!filename || (S.infrastSchedule.manifestCurrent && S.infrastSchedule.filename === filename)) return;
  const response = await apiResponse('infrast/schedule?filename=' + encodeURIComponent(filename));
  if (!response.ok || response.data.error) return;
  const state = emptyInfrastSchedule(filename);
  applyInfrastSchedulePayload(state, response.data);
  S.infrastSchedule = state;
}

async function loadInfrastSchedule(filename) {
  const state = emptyInfrastSchedule(filename);
  state.busy = true;
  S.infrastSchedule = state;
  render();
  try {
    const response = await apiResponse(
      'infrast/schedule?filename=' + encodeURIComponent(filename)
    );
    if (!response.ok || response.data.error) {
      throw new Error(infrastImportError(response.data, '读取周排班失败'));
    }
    applyInfrastSchedulePayload(state, response.data);
  } catch (error) {
    state.error = error && error.message ? error.message : '读取周排班失败';
  } finally {
    state.busy = false;
    render();
  }
}

function setInfrastScheduleMode(mode) {
  if (S.infrastSchedule.analysis && S.infrastSchedule.analysis.cross_day_cycle) return;
  if (!['preserve','replace'].includes(mode) || S.infrastSchedule.mode === mode) return;
  S.infrastSchedule.mode = mode;
  invalidateInfrastSchedulePreview();
  render();
}

function setInfrastScheduleConfirmation(field, checked) {
  if (!['inferenceConfirmed','enableWeeklyPlan','commitConfirmed'].includes(field)) return;
  S.infrastSchedule[field] = !!checked;
  if (field !== 'commitConfirmed') invalidateInfrastSchedulePreview();
  render();
}

function minutesToTime(value) {
  const minute = ((value % 1440) + 1440) % 1440;
  return String(Math.floor(minute / 60)).padStart(2,'0') + ':' + String(minute % 60).padStart(2,'0');
}

function timeToMinutes(value) {
  if (!/^\d{2}:\d{2}$/.test(value)) return null;
  const [hour, minute] = value.split(':').map(Number);
  return hour < 24 && minute < 60 ? hour * 60 + minute : null;
}

function setInfrastAnchorTime(value) {
  const next = timeToMinutes(value);
  const previous = timeToMinutes(S.infrastSchedule.anchorTime);
  if (next === null || previous === null || next === previous) return;
  if (S.infrastSchedule.analysis && S.infrastSchedule.analysis.cross_day_cycle) {
    S.infrastSchedule.anchorTime = value;
    S.infrastSchedule.days = buildCyclePreviewDays(
      S.infrastSchedule.analysis,
      S.infrastSchedule.anchorDate || localIsoDate(),
      value
    );
    invalidateInfrastSchedulePreview();
    render();
    return;
  }
  const delta = next - previous;
  PLAN_WEEKDAYS.forEach(day => {
    (S.infrastSchedule.days[day.v] || []).forEach(slot => {
      const current = timeToMinutes(slot.time);
      if (current !== null) {
        slot.time = minutesToTime(current + delta);
        slot.source_time = slot.time;
      }
    });
  });
  S.infrastSchedule.anchorTime = value;
  invalidateInfrastSchedulePreview();
  render();
}

function setInfrastAnchorDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value === S.infrastSchedule.anchorDate) return;
  S.infrastSchedule.anchorDate = value;
  S.infrastSchedule.days = buildCyclePreviewDays(
    S.infrastSchedule.analysis,
    value,
    S.infrastSchedule.anchorTime
  );
  invalidateInfrastSchedulePreview();
  render();
}

function setInfrastSlotTime(weekday, planIndex, value) {
  if (S.infrastSchedule.analysis && S.infrastSchedule.analysis.cross_day_cycle) return;
  if (!PLAN_WEEKDAYS.some(day => day.v === weekday) || timeToMinutes(value) === null) return;
  const slot = (S.infrastSchedule.days[weekday] || []).find(item => item.plan_index === planIndex);
  if (!slot || slot.time === value) return;
  slot.time = value;
  slot.source = 'manual';
  invalidateInfrastSchedulePreview();
  S.infrastSchedule.selectedCell = {weekday, planIndex};
  render();
}

function setInfrastAdoption(lineHash, checked) {
  const allowed = ((S.infrastSchedule.cron && S.infrastSchedule.cron.safe_daily_entries) || [])
    .some(entry => entry.line_hash === lineHash);
  if (!allowed) return;
  const selected = new Set(S.infrastSchedule.adoptedLineHashes);
  if (checked) selected.add(lineHash); else selected.delete(lineHash);
  S.infrastSchedule.adoptedLineHashes = [...selected];
  invalidateInfrastSchedulePreview();
  render();
}

function scheduleConfirmationsComplete() {
  const state = S.infrastSchedule;
  const candidates = (state.cron && state.cron.safe_daily_entries) || [];
  return (!state.analysis || !state.analysis.requires_confirmation || state.inferenceConfirmed)
    && state.enableWeeklyPlan
    && !state.unsafeDailyBlocked
    && candidates.every(entry => state.adoptedLineHashes.includes(entry.line_hash));
}

async function previewInfrastSchedule() {
  const state = S.infrastSchedule;
  if (state.busy || !scheduleConfirmationsComplete()) return;
  const request = scheduleRequest();
  state.busy = true;
  state.error = '';
  render();
  try {
    const response = await apiResponse('infrast/schedule/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(request)
    });
    if (!response.ok || response.data.error) {
      throw new Error(infrastImportError(response.data, '预览排班失败'));
    }
    state.preview = response.data;
    state.previewRequestFingerprint = scheduleFingerprint(request);
    state.commitConfirmed = false;
  } catch (error) {
    state.error = error && error.message ? error.message : '预览排班失败';
  } finally {
    state.busy = false;
    render();
  }
}

function schedulePreviewCurrent() {
  const state = S.infrastSchedule;
  return !!state.preview
    && state.previewRequestFingerprint === scheduleFingerprint()
    && scheduleConfirmationsComplete();
}

async function commitInfrastSchedule() {
  const state = S.infrastSchedule;
  const preview = state.preview;
  if (state.busy || !schedulePreviewCurrent() || !state.commitConfirmed || preview.status !== 'changed') return;
  state.busy = true;
  render();
  try {
    const response = await apiResponse('infrast/schedule/commit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        request:preview.request,
        expected_hashes:preview.source_hashes,
        expected_target_hashes:preview.target_hashes,
        transaction_id:preview.transaction_id,
        generated_at:preview.generated_at
      })
    });
    if (response.status === 409) {
      invalidateInfrastSchedulePreview();
      state.error = '来源已变化，已重新加载，请再次预览';
      await loadInfrastSchedule(state.filename);
      return;
    }
    if (!response.ok || !response.data.ok) throw new Error(infrastImportError(response.data, '保存排班失败'));
    const filename = state.filename;
    await loadInfrastSchedule(filename);
    showMsg('ok', '周排班已保存');
  } catch (error) {
    state.error = error && error.message ? error.message : '保存排班失败';
  } finally {
    state.busy = false;
    render();
  }
}

async function runCurrentInfrast() {
  const state = S.infrastSchedule;
  if (state.busy || !state.manifestCurrent) return;
  if (!confirm('立即按当前日期和时间执行本应生效的基建班次？普通日常仍会自动跳过基建。')) return;
  state.busy = true;
  render();
  try {
    const response = await apiResponse('run', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task:'daily', infrast_now:true})
    });
    if (!response.ok || !response.data.ok) throw new Error(infrastImportError(response.data, '启动当前班次失败'));
    showMsg('ok', '已启动当前班次，重复班次会由执行记录自动跳过');
    await loadAll();
    go('home');
  } catch (error) {
    state.error = error && error.message ? error.message : '启动当前班次失败';
  } finally {
    state.busy = false;
    render();
  }
}

async function previewInfrastRestore() {
  const state = S.infrastSchedule;
  if (state.busy) return;
  state.busy = true;
  state.restorePreview = null;
  state.restoreConfirmed = false;
  render();
  try {
    const response = await apiResponse('infrast/schedule/restore/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({snapshot_id:null})
    });
    if (!response.ok || response.data.error) throw new Error(infrastImportError(response.data, '恢复预览失败'));
    state.restorePreview = response.data;
  } catch (error) {
    state.error = error && error.message ? error.message : '恢复预览失败';
  } finally {
    state.busy = false;
    render();
  }
}

async function commitInfrastRestore() {
  const state = S.infrastSchedule;
  const preview = state.restorePreview;
  if (state.busy || !preview || preview.status !== 'changed' || !state.restoreConfirmed) return;
  state.busy = true;
  render();
  try {
    const response = await apiResponse('infrast/schedule/restore', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        snapshot_id:preview.snapshot_id,
        expected_hashes:preview.source_hashes,
        expected_snapshot_sha:preview.snapshot_sha256
      })
    });
    if (response.status === 409) {
      state.restorePreview = null;
      state.restoreConfirmed = false;
      await loadInfrastSchedule(state.filename);
      return;
    }
    if (!response.ok || !response.data.ok) throw new Error(infrastImportError(response.data, '恢复失败'));
    const filename = state.filename;
    await loadInfrastSchedule(filename);
    showMsg('ok', '已恢复原始日常与定时配置');
  } catch (error) {
    state.error = error && error.message ? error.message : '恢复失败';
  } finally {
    state.busy = false;
    render();
  }
}

function openInfrastScheduleCell(weekday, planIndex) {
  S.infrastSchedule.selectedCell = {weekday, planIndex};
  S.infrastSchedule.drawerOpen = true;
  render();
}

function closeInfrastScheduleDrawer() {
  S.infrastSchedule.drawerOpen = false;
  render();
}

function selectInfrastScheduleSlot(weekday, planIndex) {
  S.infrastSchedule.selectedCell = {weekday, planIndex};
  S.infrastSchedule.drawerOpen = false;
  render();
}

function renderInfrastScheduleDrawer() {
  const state = S.infrastSchedule;
  const selected = state.selectedCell;
  if (!selected || state.drawerOpen === false) return '';
  const slot = (state.days[selected.weekday] || []).find(item => item.plan_index === selected.planIndex);
  const plans = (state.details && state.details.plans) || [];
  const plan = plans.find(item => item.index === selected.planIndex);
  if (!slot || !plan) return '';
  let rooms = '';
  INFRAST_FACILITY_ORDER.forEach(([facility,label]) => {
    const items = (plan.rooms && plan.rooms[facility]) || [];
    if (!items.length) return;
    rooms += `<section class="infrast-facility"><h3>${esc(label)}</h3><div class="infrast-room-grid">`;
    items.forEach(room => {
      rooms += `<article class="infrast-room"><h4>房间 ${esc(room.index)}</h4>
        <div class="infrast-room-meta">固定干员：${esc((room.operators || []).join('、') || '无')}<br>
        候选干员：${esc((room.candidates || []).join('、') || '无')}</div></article>`;
    });
    rooms += '</div></section>';
  });
  return `<div class="isched-drawer-backdrop" onclick="closeInfrastScheduleDrawer()"></div>
    <aside class="isched-drawer" role="dialog" aria-modal="true" aria-label="班次详情" onkeydown="if(event.key==='Escape')closeInfrastScheduleDrawer()">
      <div class="isched-drawer-head"><div><h2>${esc(plan.name)}</h2><div class="infrast-plan-meta">${esc(planWeekdayName(selected.weekday))} · plan_index=${esc(plan.index)}</div></div>
      <button class="btn btn-s" onclick="closeInfrastScheduleDrawer()" aria-label="关闭详情">关闭</button></div>
      ${state.analysis && state.analysis.cross_day_cycle
        ? `<div class="help">循环班次：${esc(slot.logical_date || planWeekdayName(selected.weekday))} ${esc(slot.time)}，修改锚点即可整体调整。</div>`
        : `<label class="isched-field"><span>该日执行时间</span><input type="time" value="${esc(slot.time)}" onchange="setInfrastSlotTime('${esc(selected.weekday)}',${Number(plan.index)},this.value)"></label>`}
      ${plan.description ? `<div class="help">${esc(plan.description)}</div>` : ''}
      ${rooms || '<div class="isched-empty">该班次没有房间明细</div>'}
    </aside>`;
}

function selectedInfrastWeekday() {
  const selected = S.infrastSchedule && S.infrastSchedule.selectedCell;
  return selected && PLAN_WEEKDAYS.some(day => day.v === selected.weekday)
    ? selected.weekday : currentPlanWeekday();
}

function selectInfrastScheduleDay(weekday) {
  const state = S.infrastSchedule;
  const slots = (state.days && state.days[weekday]) || [];
  const currentPlanIndex = state.selectedCell && state.selectedCell.planIndex;
  const selected = slots.find(slot => slot.plan_index === currentPlanIndex) || slots[0];
  state.selectedCell = selected ? {weekday, planIndex:selected.plan_index} : {weekday, planIndex:-1};
  state.drawerOpen = false;
  render();
}

function renderInfrastDayTabs() {
  const selectedWeekday = selectedInfrastWeekday();
  return `<div class="isched-days">${PLAN_WEEKDAYS.map(day => {
    const date = dateForPlanWeekday(day.v);
    return `<button type="button" class="isched-day ${day.v===selectedWeekday?'is-selected':''}" onclick="selectInfrastScheduleDay('${day.v}')"><strong>${esc(day.n)}</strong><br><small>${esc(formatCalendarDate(date))}</small></button>`;
  }).join('')}</div>`;
}

function renderInfrastTimeline() {
  const state = S.infrastSchedule;
  const weekday = selectedInfrastWeekday();
  const slots = [...((state.days && state.days[weekday]) || [])]
    .filter(slot => timeToMinutes(slot.time) !== null)
    .sort((left,right) => timeToMinutes(left.time) - timeToMinutes(right.time));
  if (!slots.length) return '<div class="isched-empty">当天没有可显示的班次</div>';
  const selectedPlanIndex = state.selectedCell && state.selectedCell.planIndex;
  const markers = slots.map(slot => {
    const left = Math.max(0, Math.min(100, timeToMinutes(slot.time) / 1440 * 100));
    return `<button type="button" class="timeline-marker" style="left:${left}%" onclick="selectInfrastScheduleSlot('${weekday}',${Number(slot.plan_index)})" aria-label="${esc(slot.time+' '+slot.name)}"><span>${esc(slot.time)}<small>${esc(slot.name)}</small></span></button>`;
  }).join('');
  let selectedIndex = slots.findIndex(slot => slot.plan_index === selectedPlanIndex);
  if (selectedIndex < 0) selectedIndex = 0;
  const current = slots[selectedIndex];
  const next = slots[(selectedIndex + 1) % slots.length];
  return `<div class="isched-timeline"><div class="timeline-scale"><span>00:00</span><span>04:00</span><span>08:00</span><span>12:00</span><span>16:00</span><span>20:00</span><span>24:00</span></div>
    <div class="timeline-track"><div class="timeline-line"></div>${markers}</div>
    <div class="shift-summary"><div class="shift-block"><div class="shift-label">当前班次</div><div class="shift-name">${esc(current.name)}</div><div class="shift-time">开始 ${esc(current.time)}</div></div>
    <div class="shift-block"><div class="shift-label">下一班</div><div class="shift-name">${esc(next.time)} ${esc(next.name)}</div><div class="shift-time">${timeToMinutes(next.time)<=timeToMinutes(current.time)?'次日 ':''}${esc(next.time)}</div></div></div></div>`;
}

function renderInfrastPersonnel() {
  const state = S.infrastSchedule;
  const weekday = selectedInfrastWeekday();
  const slots = (state.days && state.days[weekday]) || [];
  const selectedPlanIndex = state.selectedCell && state.selectedCell.planIndex;
  const slot = slots.find(item => item.plan_index === selectedPlanIndex) || slots[0];
  const plans = (state.details && state.details.plans) || [];
  const plan = slot && plans.find(item => item.index === slot.plan_index);
  if (!plan) return '<aside class="card personnel-panel"><h2>人员配置</h2><div class="isched-empty">选择班次后查看人员</div></aside>';
  let rooms = '';
  INFRAST_FACILITY_ORDER.forEach(([facility,label]) => {
    const items = (plan.rooms && plan.rooms[facility]) || [];
    items.forEach(room => {
      const chips = values => (values || []).map(name => `<span class="operator-chip">${esc(name)}</span>`).join('') || '<span class="infrast-plan-meta">无</span>';
      rooms += `<section class="personnel-room"><h4>${esc(label)}${items.length>1?' '+(Number(room.index)+1):''}</h4>
        <div class="personnel-line"><span>固定干员</span><div class="operator-chips">${chips(room.operators)}</div></div>
        ${(room.candidates || []).length ? `<div class="personnel-line"><span>候选</span><div class="operator-chips">${chips(room.candidates)}</div></div>` : ''}</section>`;
    });
  });
  const extra = [];
  if (plan.groups && plan.groups.length) extra.push(`干员组 ${plan.groups.map(group => `${group.name}：${(group.operators||[]).join('、')}`).join('；')}`);
  if (plan.drones && plan.drones.enable) extra.push(`无人机 ${plan.drones.room || '未指定'} ${plan.drones.index || ''}`);
  if (plan.Fiammetta && plan.Fiammetta.enable) extra.push(`菲亚梅塔 → ${plan.Fiammetta.target || '未指定'}`);
  return `<aside class="card personnel-panel"><h3>${esc(plan.name)}</h3><div class="infrast-plan-meta">${esc(planWeekdayName(weekday))} · ${esc(slot.time)} · 人员配置</div>
    ${rooms || '<div class="isched-empty">该班次没有房间人员</div>'}
    ${extra.length?`<div class="personnel-note">${extra.map(esc).join('<br>')}</div>`:''}
    <div class="personnel-note">人员内容来自当前排班 JSON。需要调整时请编辑并重新导入完整排班文件，保存仍使用既有预览与确认流程。</div></aside>`;
}

function renderInfrastSchedule() {
  const state = S.infrastSchedule;
  if (!state.filename) return '';
  if (state.busy && !state.analysis) return '<div class="card isched-loading">正在读取周排班...</div>';
  if (state.error && !state.analysis) return `<div class="card"><div class="msg err">${esc(state.error)}</div></div>`;
  if (!state.analysis) return '';
  const times = [...new Set(PLAN_WEEKDAYS.flatMap(day => (state.days[day.v] || []).map(slot => slot.time)))].sort();
  const safeEntries = (state.cron && state.cron.safe_daily_entries) || [];
  const managed = state.cron && state.cron.has_managed_block;
  const crossDay = !!(state.analysis && state.analysis.cross_day_cycle);
  let grid = '<div class="infrast-schedule-grid"><div class="isched-grid-head">时间</div>';
  PLAN_WEEKDAYS.forEach(day => { grid += `<div class="isched-grid-head">${esc(day.n)}</div>`; });
  times.forEach(time => {
    grid += `<div class="isched-time">${esc(time)}</div>`;
    PLAN_WEEKDAYS.forEach(day => {
      const slot = (state.days[day.v] || []).find(item => item.time === time);
      grid += slot
        ? `<button class="isched-cell" onclick="openInfrastScheduleCell('${day.v}',${Number(slot.plan_index)})"><strong>${esc(slot.name)}</strong><span class="isched-badge ${slot.merge_status==='已合并'?'is-active':'is-warning'}">${esc(slot.merge_status)}</span><span class="isched-badge is-active">${esc(slot.validation_badge)}</span><small>plan_index=${esc(slot.plan_index)}<br>${esc(slot.source)} · ${esc(slot.logical_date || '')} ${esc(slot.time)}</small></button>`
        : '<div class="isched-cell"></div>';
    });
  });
  grid += '</div>';
  const adoption = safeEntries.length
    ? `<div class="isched-adoption"><strong>发现可安全纳管的旧 daily 定时</strong>${safeEntries.map(entry =>
        `<label><input type="checkbox" ${state.adoptedLineHashes.includes(entry.line_hash)?'checked':''} onchange="setInfrastAdoption('${esc(entry.line_hash)}',this.checked)"><code>${esc(entry.display)}</code></label>`
      ).join('')}</div>` : '<div class="help">没有待纳管的旧 daily 定时。</div>';
  const preview = state.preview;
  const current = schedulePreviewCurrent();
  let previewBlock = '';
  if (preview) {
    const taskDiffs = Array.isArray(preview.task_diff) ? preview.task_diff : [];
    previewBlock = `<div class="card"><h2>排班变更预览</h2>
      <div class="isched-status"><span class="isched-badge ${preview.status==='unchanged'?'is-active':'is-warning'}">${esc(preview.status)}</span><span class="isched-badge">${esc(taskDiffs.length)} 个任务文件变化</span></div>
      <div class="isched-diff"><code>任务：${esc(JSON.stringify(taskDiffs))}</code><code>定时：${esc(JSON.stringify(preview.cron_diff || {}))}</code></div>
      ${preview.status === 'changed' ? `<div class="isched-confirm"><label><input type="checkbox" ${state.commitConfirmed?'checked':''} onchange="setInfrastScheduleConfirmation('commitConfirmed',this.checked)"> 我已核对七天班次、MAA 定时和旧 daily 纳管结果</label></div>
      <button class="btn btn-p" onclick="commitInfrastSchedule()" ${current && state.commitConfirmed && !state.busy?'':'disabled'}>确认保存</button>` : '<div class="help">当前配置与预览一致，无需重复保存。</div>'}</div>`;
  }
  const restore = state.restorePreview;
  const restoreDiff = restore ? `<div class="isched-diff" data-restore-diff="visible">
      <code>全部目标：${esc(JSON.stringify(restore.target_diff || []))}</code>
      <code>任务文件：${esc(JSON.stringify(restore.task_diff || []))}</code>
      <code>周计划：${esc(JSON.stringify(restore.weekly_diff || []))}</code>
      <code>MAA 定时：${esc(JSON.stringify(restore.cron_diff || {}))}</code>
    </div>` : '';
  const restoreBlock = `<div class="card"><h2>恢复原始配置</h2><div class="help">预览首次启用 Stage C 前的任务文件、周计划设置和定时内容。恢复不会运行 MAA。</div>
    <button class="btn btn-s" onclick="previewInfrastRestore()" ${state.busy?'disabled':''}>预览恢复</button>
    ${restore ? `<div class="isched-diff"><code>snapshot_id=${esc(restore.snapshot_id)}</code><code>snapshot_sha256=${esc(restore.snapshot_sha256)}</code></div>${restoreDiff}
      ${restore.status === 'changed' ? `<div class="isched-confirm"><label><input type="checkbox" onchange="S.infrastSchedule.restoreConfirmed=this.checked;render()" ${state.restoreConfirmed?'checked':''}> 我确认恢复原始配置并移除 Stage C 受管排班</label></div><button class="btn btn-p" onclick="commitInfrastRestore()" ${state.restoreConfirmed && !state.busy?'':'disabled'}>确认恢复</button>` : '<div class="help">已经是原始配置，无需恢复。</div>'}` : ''}</div>`;
  return `<section class="isched-shell"><div class="page-head"><div><h2>周基建排班</h2><p>${esc((state.details && state.details.title) || state.filename)}</p></div></div>
    <div class="isched-overview"><div><section class="card">${renderInfrastDayTabs()}<h2>24 小时排班时间轴</h2>${renderInfrastTimeline()}</section></div>${renderInfrastPersonnel()}</div>
    <div class="card"><h2>排班设置</h2>
    <div class="isched-status"><span class="isched-badge ${state.manifestCurrent?'is-active':''}">${state.manifestCurrent?'已启用':'未启用'}</span><span class="isched-badge">${managed?'MAA 定时已受管':'MAA 定时未受管'}</span><span class="isched-badge">${esc((state.cron && state.cron.managed_entry_count) || 0)} 条受管定时</span></div>
    <div class="help">${crossDay
      ? `这是 ${esc(state.analysis.cycle_hours)} 小时跨日循环。普通“运行日常”会自动跳过基建；只有受管换班时间或“立即执行当前班次”会运行 Infrast。`
      : '启用后，普通“运行日常”会自动跳过基建；受管换班时间仍按下方周计划执行。'}</div>
    <div class="isched-toolbar"><div><span class="infrast-plan-meta">策略</span><div class="isched-choice" aria-label="排班策略"><button aria-pressed="${state.mode==='preserve'}" onclick="setInfrastScheduleMode('preserve')" ${crossDay?'disabled':''}>保持原任务</button><button aria-pressed="${state.mode==='replace'}" onclick="setInfrastScheduleMode('replace')">独立基建接管</button></div></div>
    ${crossDay ? `<label class="isched-field"><span>循环锚点日期</span><input type="date" value="${esc(state.anchorDate || localIsoDate())}" onchange="setInfrastAnchorDate(this.value)"></label>` : ''}
    <label class="isched-field"><span>首班时间</span><input type="time" value="${esc(state.anchorTime)}" onchange="setInfrastAnchorTime(this.value)"></label>
    ${state.manifestCurrent ? `<button class="btn btn-s" onclick="runCurrentInfrast()" ${state.busy?'disabled':''}>立即执行当前班次</button>` : ''}</div>
    <div class="isched-confirm">
      ${state.analysis.requires_confirmation ? `<label><input type="checkbox" ${state.inferenceConfirmed?'checked':''} onchange="setInfrastScheduleConfirmation('inferenceConfirmed',this.checked)"> 我确认班次时间由文件名称或描述推测</label>` : '<div class="help">JSON 提供了明确班次时间，无需推测确认。</div>'}
      <label><input type="checkbox" ${state.enableWeeklyPlan?'checked':''} onchange="setInfrastScheduleConfirmation('enableWeeklyPlan',this.checked)"> 我确认启用 MAA 周计划并同步调整定时任务</label>
    </div>
    ${state.unsafeDailyBlocked ? `<div class="isched-alert">检测到 ${esc(state.cron.unsafe_daily.count)} 条包装器或复杂 daily 定时。为防止重复执行，保存已阻止，请先在“定时”页人工处理。</div>` : ''}
    ${adoption}
    ${state.error ? `<div class="msg err">${esc(state.error)}</div>` : ''}
    <div class="btn-row"><button class="btn btn-p" onclick="previewInfrastSchedule()" ${scheduleConfirmationsComplete() && !state.busy?'':'disabled'}>${state.busy?'处理中...':'预览排班'}</button></div>
  </div><details class="advanced-panel"><summary>高级星期网格与事务详情</summary><div class="advanced-body"><div class="card"><h2>星期视图</h2><div class="isched-grid-scroll">${times.length?grid:'<div class="isched-empty">没有可显示的班次</div>'}</div></div>
  ${previewBlock}${restoreBlock}</div></details>${renderInfrastScheduleDrawer()}</section>`;
}

function resetInfrastImportPreview() {
  S.infrastImport.preview = null;
  S.infrastImport.replaceConfirmed = false;
  S.infrastImport.error = '';
  S.infrastImport.previewSelectedPlan = 0;
}

function infrastImportPreviewDisabled(input) {
  return !!input.busy || !/\.json$/.test(input.filename) || !input.content;
}

function syncInfrastImportPreviewControl() {
  const button = document.getElementById('infrast-import-preview');
  if (button) button.disabled = infrastImportPreviewDisabled(S.infrastImport);
}

function setInfrastImportOpen(open) {
  if (S.infrastImport.busy) return;
  S.infrastImport.open = !!open;
  render();
}

function setInfrastImportSource(source) {
  if (S.infrastImport.busy || !['file', 'paste'].includes(source) || source === S.infrastImport.source) return;
  S.infrastImport.source = source;
  S.infrastImport.filename = '';
  S.infrastImport.content = '';
  resetInfrastImportPreview();
  render();
}

function setInfrastImportInput(field, value) {
  if (S.infrastImport.busy || !['filename', 'content'].includes(field) || S.infrastImport[field] === value) return;
  const needsRender = !!S.infrastImport.preview || !!S.infrastImport.replaceConfirmed || !!S.infrastImport.error;
  S.infrastImport[field] = value;
  resetInfrastImportPreview();
  if (needsRender) render();
  else syncInfrastImportPreviewControl();
}

function setInfrastImportFilename(value) {
  setInfrastImportInput('filename', value);
}

function setInfrastImportContent(value) {
  setInfrastImportInput('content', value);
}

async function readInfrastImportFile(file) {
  if (!file || S.infrastImport.busy) return;
  const input = S.infrastImport;
  input.source = 'file';
  input.filename = file.name || '';
  input.content = '';
  resetInfrastImportPreview();
  if (!/\.json$/.test(input.filename)) {
    input.error = '只接受 .json 文件';
    render();
    return;
  }
  if (file.size > 1048576) {
    input.error = '文件超过 1 MiB 限制';
    render();
    return;
  }
  input.busy = true;
  render();
  try {
    const buffer = await file.arrayBuffer();
    const decoder = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true});
    input.content = decoder.decode(buffer);
  } catch (_) {
    input.content = '';
    input.error = '文件不是有效的 UTF-8 文本';
  } finally {
    input.busy = false;
    render();
  }
}

function infrastImportError(data, fallback) {
  const error = data && data.error;
  return error && error.message ? error.message : fallback;
}

async function previewInfrastImport() {
  const input = S.infrastImport;
  if (input.busy) return;
  resetInfrastImportPreview();
  if (!/\.json$/.test(input.filename)) {
    input.error = '文件名必须以 .json 结尾';
    render();
    return;
  }
  if (!input.content) {
    input.error = '请选择文件或粘贴 JSON 文本';
    render();
    return;
  }
  input.busy = true;
  render();
  try {
    const response = await apiResponse('infrast/import/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filename:input.filename, content:input.content})
    });
    if (!response.ok || !response.data.ok) {
      input.error = infrastImportError(response.data, '预览失败');
      return;
    }
    input.preview = response.data.preview;
    input.previewSelectedPlan = 0;
  } catch (_) {
    input.error = '预览请求失败，请检查 WebUI 连接';
  } finally {
    input.busy = false;
    render();
  }
}

async function commitInfrastImport() {
  const input = S.infrastImport;
  const preview = input.preview;
  if (input.busy || !preview || preview.state === 'identical') return;
  let payload;
  if (preview.state === 'new') {
    payload = {
      filename: input.filename, content: input.content,
      preview_sha256: preview.sha256,
      expected_current_sha256: null,
      replace: false
    };
  } else if (preview.state === 'conflict') {
    if (!input.replaceConfirmed) return;
    payload = {
      filename: input.filename, content: input.content,
      preview_sha256: preview.sha256,
      expected_current_sha256: preview.current_sha256,
      replace: true
    };
  } else {
    resetInfrastImportPreview();
    input.error = '预览状态无效，请重新预览';
    render();
    return;
  }

  input.busy = true;
  input.error = '';
  render();
  try {
    const response = await apiResponse('infrast/import', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    if (!response.ok || !response.data.ok) {
      const code = response.data && response.data.error && response.data.error.code;
      const message = infrastImportError(response.data, '导入失败');
      if (response.status === 409 && ['stale_preview', 'preview_changed'].includes(code)) {
        resetInfrastImportPreview();
        input.error = message + '，请重新预览后再确认';
      } else {
        input.error = message;
      }
      return;
    }

    const filename = input.filename;
    resetInfrastImportPreview();
    const libraryLoaded = await loadInfrastPlans();
    const planIndex = S.infrastPlans.findIndex(plan => plan.valid && plan.filename === filename);
    if (!libraryLoaded || planIndex < 0) {
      input.error = '导入已完成，但重新读取文件库失败，请手动刷新';
      return;
    }
    const detailsLoaded = await selectInfrastPlan(planIndex);
    if (!detailsLoaded) {
      input.error = '导入已完成，但重新读取排班详情失败，请手动刷新';
      return;
    }
    input.open = false;
    showMsg('ok', '排班 JSON 已导入并重新读取 ✓');
  } catch (_) {
    input.error = '导入请求失败，请检查 WebUI 连接';
  } finally {
    input.busy = false;
    render();
  }
}

function setInfrastReplaceConfirmed(confirmed) {
  if (S.infrastImport.busy) return;
  S.infrastImport.replaceConfirmed = !!confirmed;
  render();
}

function selectInfrastPreviewPlanIndex(index) {
  const plans = (S.infrastImport.preview && S.infrastImport.preview.details && S.infrastImport.preview.details.plans) || [];
  if (!Number.isInteger(index) || index < 0 || index >= plans.length) return;
  S.infrastImport.previewSelectedPlan = index;
  render();
}

async function loadOperatorBox(filename) {
  const state = S.operatorBox;
  state.error = '';
  const selected = filename || (S.infrastSelected && S.infrastSelected.filename) || '';
  try {
    const suffix = selected ? '?filename=' + encodeURIComponent(selected) : '';
    const payload = await api('infrast/operators' + suffix);
    if (payload.error) throw new Error(payload.error.message || '读取干员扫描结果失败');
    state.data = payload;
    state.loaded = true;
  } catch (error) {
    state.error = error && error.message ? error.message : '读取干员扫描结果失败';
    state.loaded = true;
  }
  if (S.page === 'infrast-plans') render();
  return !state.error;
}

async function startOperatorScan() {
  const state = S.operatorBox;
  if (state.busy || (S.status && S.status.active)) return;
  if (!confirm('扫描会启动游戏并通过 MAA 读取干员列表，确认现在开始吗？')) return;
  state.busy = true;
  state.error = '';
  render();
  try {
    const response = await apiResponse('infrast/operators/scan', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:'{}'
    });
    if (!response.data || !response.data.ok) {
      throw new Error((response.data && (response.data.error || response.data.message)) || '扫描未启动');
    }
    showMsg('ok', '干员扫描已启动；只有 done=true 才会更新快照');
    setTimeout(pollOperatorScan, 1000);
  } catch (error) {
    state.busy = false;
    state.error = error && error.message ? error.message : '扫描启动失败';
    render();
  }
}

async function pollOperatorScan() {
  const status = await api('status').catch(() => null);
  if (status) S.status = status;
  if (status && status.active) {
    if (S.page === 'infrast-plans') render();
    setTimeout(pollOperatorScan, 2000);
    return;
  }
  S.operatorBox.busy = false;
  await loadOperatorBox();
}

function setOperatorWeekly(field, value) {
  if (!S.operatorBox.data) return;
  S.operatorBox.data.weekly = S.operatorBox.data.weekly || {};
  S.operatorBox.data.weekly[field] = value;
  S.operatorBox.weeklyPreview = null;
  S.operatorBox.confirmedConflicts = false;
  render();
}

function operatorWeeklyRequest() {
  const weekly = (S.operatorBox.data && S.operatorBox.data.weekly) || {};
  return {
    enabled: !!weekly.enabled,
    weekday: weekly.weekday || 'sun',
    time: weekly.time || '07:30',
    confirmed_conflicts: !!S.operatorBox.confirmedConflicts
  };
}

async function previewOperatorWeekly() {
  S.operatorBox.error = '';
  try {
    const response = await apiResponse('infrast/operators/weekly/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(operatorWeeklyRequest())
    });
    if (!response.ok) throw new Error((response.data.error && response.data.error.message) || '预览失败');
    S.operatorBox.weeklyPreview = response.data;
  } catch (error) {
    S.operatorBox.error = error && error.message ? error.message : '每周扫描预览失败';
  }
  render();
}

async function commitOperatorWeekly() {
  const preview = S.operatorBox.weeklyPreview;
  if (!preview || preview.status === 'conflict') return;
  try {
    const response = await apiResponse('infrast/operators/weekly', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        request:operatorWeeklyRequest(),
        expected_crontab_sha256:preview.expected_crontab_sha256
      })
    });
    if (!response.ok || !response.data.ok) throw new Error((response.data.error && response.data.error.message) || '保存失败');
    showMsg('ok', '每周干员扫描设置已保存');
    S.operatorBox.weeklyPreview = null;
    await loadOperatorBox();
  } catch (error) {
    S.operatorBox.error = error && error.message ? error.message : '保存每周扫描失败';
    S.operatorBox.weeklyPreview = null;
    render();
  }
}

async function loadInfrastPlans() {
  S.infrastLibraryError = '';
  try {
    const payload = await api('infrast/plans');
    if (payload.error) throw new Error(payload.error.message || '读取排班文件库失败');
    S.infrastPlans = Array.isArray(payload.plans) ? payload.plans : [];
    S.infrastPlansLoaded = true;
    if (S.infrastSelected) {
      const selected = S.infrastPlans.find(plan => plan.valid && plan.filename === S.infrastSelected.filename);
      if (!selected) S.infrastSelected = null;
    }
  } catch (error) {
    S.infrastLibraryError = error && error.message ? error.message : '读取排班文件库失败';
  }
  if (S.page === 'infrast-plans') render();
  return !S.infrastLibraryError;
}

async function selectInfrastPlan(planIndex) {
  const summary = S.infrastPlans[planIndex];
  if (!summary || !summary.valid) return;
  const filename = summary.filename;
  S.infrastLibraryError = '';
  try {
    const details = await api('infrast/plan?filename=' + encodeURIComponent(filename));
    if (details.error) throw new Error(details.error.message || '读取排班详情失败');
    S.infrastSelected = details;
    S.infrastSelectedPlan = 0;
    await loadInfrastSchedule(filename);
    await loadOperatorBox(filename);
  } catch (error) {
    S.infrastSelected = null;
    S.infrastLibraryError = error && error.message ? error.message : '读取排班详情失败';
  }
  render();
  return !!S.infrastSelected && S.infrastSelected.filename === filename;
}

function selectInfrastPlanIndex(index) {
  const plans = (S.infrastSelected && S.infrastSelected.plans) || [];
  if (!Number.isInteger(index) || index < 0 || index >= plans.length) return;
  S.infrastSelectedPlan = index;
  render();
}

function renderInfrastImport() {
  const input = S.infrastImport;
  if (!input.open) return '';
  const sourceButtons = `<div class="btn-row">
    <button class="btn btn-s" onclick="setInfrastImportSource('file')" style="${input.source==='file'?'border-color:var(--ac);color:var(--ac)':''}" ${input.busy?'disabled':''}>选择文件</button>
    <button class="btn btn-s" onclick="setInfrastImportSource('paste')" style="${input.source==='paste'?'border-color:var(--ac);color:var(--ac)':''}" ${input.busy?'disabled':''}>粘贴 JSON</button>
    <button class="btn btn-s" onclick="setInfrastImportOpen(false)" ${input.busy?'disabled':''}>关闭</button>
  </div>`;
  let form;
  if (input.source === 'file') {
    form = `<div class="fg"><label>JSON 文件</label><input type="file" accept=".json,application/json" onchange="readInfrastImportFile(this.files && this.files[0])" ${input.busy?'disabled':''}></div>
      <div class="help">先检查 1 MiB 大小限制，再以严格 UTF-8 解码。已读取：${esc(input.filename || '尚未选择')}</div>`;
  } else {
    form = `<div class="fg"><label>文件名</label><input type="text" value="${esc(input.filename)}" placeholder="plan.json" oninput="setInfrastImportFilename(this.value)" ${input.busy?'disabled':''}></div>
      <div class="fg"><label>JSON 文本</label><textarea style="min-height:180px" spellcheck="false" placeholder="粘贴 custom_infrast JSON" oninput="setInfrastImportContent(this.value)" ${input.busy?'disabled':''}>${esc(input.content)}</textarea></div>`;
  }
  const previewDisabled = infrastImportPreviewDisabled(input) ? 'disabled' : '';
  let h = `<div class="card"><h2>📥 导入排班 JSON</h2>${sourceButtons}${form}
    ${input.error ? `<div class="msg err">${esc(input.error)}</div>` : ''}
    <div class="btn-row"><button id="infrast-import-preview" class="btn btn-p" onclick="previewInfrastImport()" ${previewDisabled}>${input.busy?'处理中...':'预览导入'}</button></div></div>`;
  if (input.preview) h += renderInfrastImportPreview();
  return h;
}

function renderInfrastImportPreview() {
  const input = S.infrastImport;
  const preview = input.preview;
  if (!preview) return '';
  let confirmation = '';
  if (preview.state === 'new') {
    confirmation = `<div class="msg ok" data-import-state="new">这是新文件，可直接确认导入。</div>
      <div class="btn-row"><button class="btn btn-p" onclick="commitInfrastImport()" ${input.busy?'disabled':''}>确认导入</button></div>`;
  } else if (preview.state === 'identical') {
    confirmation = '<div class="msg ok" data-import-state="identical">相同内容已存在，无需再次导入。</div>';
  } else if (preview.state === 'conflict') {
    confirmation = `<div class="msg err" data-import-state="conflict">同名文件内容不同。<br>当前 SHA-256：${esc(preview.current_sha256)}<br>新 SHA-256：${esc(preview.sha256)}</div>
      <div class="fg"><label style="min-width:auto"><input type="checkbox" onchange="setInfrastReplaceConfirmed(this.checked)" ${input.replaceConfirmed?'checked':''} ${input.busy?'disabled':''}> 我确认覆盖现有同名文件</label></div>
      <div class="btn-row"><button class="btn btn-p" onclick="commitInfrastImport()" ${input.replaceConfirmed && !input.busy ? '' : 'disabled'}>确认覆盖</button></div>`;
  } else {
    confirmation = '<div class="msg err">服务返回了未知预览状态，请重新预览。</div>';
  }
  const details = preview.details || {};
  return `<div class="card"><h2>🔎 导入预览</h2>
    <div class="infrast-plan-meta">文件 ${esc(preview.filename)} · 状态 ${esc(preview.state)} · 大小 ${esc(preview.size)} bytes<br>新 SHA-256 ${esc(preview.sha256)}</div>
    ${confirmation}</div>` + renderInfrastPlanDetails(details, input.previewSelectedPlan || 0);
}

function renderOperatorWeekly() {
  const state = S.operatorBox;
  const data = state.data || {};
  const weekly = data.weekly || {enabled:false, weekday:'sun', time:'07:30', available:false};
  const preview = state.weeklyPreview;
  const days = PLAN_WEEKDAYS.map(day =>
    `<option value="${day.v}" ${weekly.weekday===day.v?'selected':''}>${day.n}</option>`
  ).join('');
  let h = `<h3>每周自动扫描</h3><div class="help">${weekly.available?'默认关闭；使用同一 MAA 任务锁。':'首次完整扫描成功后才能启用。'}</div>
    <div class="btn-row"><label><input type="checkbox" onchange="setOperatorWeekly('enabled',this.checked)" ${weekly.enabled?'checked':''} ${weekly.available?'':'disabled'}> 启用</label>
    <select onchange="setOperatorWeekly('weekday',this.value)" ${weekly.available?'':'disabled'}>${days}</select>
    <input type="time" value="${esc(weekly.time || '07:30')}" onchange="setOperatorWeekly('time',this.value)" ${weekly.available?'':'disabled'}>
    <button class="btn btn-s" onclick="previewOperatorWeekly()" ${weekly.available?'':'disabled'}>预览定时修改</button></div>`;
  if (preview) {
    const conflicts = preview.conflicts || [];
    if (preview.status === 'conflict') {
      h += `<div class="msg err">这个时刻距离 ${esc(conflicts.length)} 个现有任务不足 2 小时，请确认或修改时间。${conflicts.map(item=>`<br>${esc(item.distance_minutes)} 分钟：${esc(item.line)}`).join('')}</div>
        <label><input type="checkbox" onchange="S.operatorBox.confirmedConflicts=this.checked;S.operatorBox.weeklyPreview=null;render()"> 我已了解冲突提醒，仍使用此时间</label>
        <button class="btn btn-s" onclick="previewOperatorWeekly()">重新预览</button>`;
    } else {
      h += `<div class="msg ok">${preview.status==='unchanged'?'定时无需修改。':'预览完成，不会产生重复扫描项。'}</div>
        <button class="btn btn-p" onclick="commitOperatorWeekly()">${weekly.enabled?'确认启用/更新':'确认关闭'}</button>`;
    }
  }
  return h;
}

function renderOperatorTraining(training) {
  if (!training) return '';
  const summary = training.summary || {};
  const rows = training.operators || [];
  const facilityNames = {
    control:'控制中枢', power:'发电站', manufacture:'制造站',
    trading:'贸易站', meeting:'会客室', hire:'办公室',
    dormitory:'宿舍', processing:'加工站'
  };
  const statusNames = {missing:'未拥有', below:'待提升', unknown:'门槛未知', ready:'已达到'};
  const level = value => value ? `E${esc(value.elite)} ${esc(value.level)}` : '—';
  let h = `<h3>${esc(training.label || '完整基建技能建议门槛')}</h3>
    <div class="help">按干员在当前排班中的设施，提示完整基建技能的最高解锁等级；只提示，不阻止排班执行。</div>
    <div class="help">共 ${esc(summary.total || 0)} · 未拥有 ${esc(summary.missing || 0)} · 待提升 ${esc(summary.below || 0)} · 门槛未知 ${esc(summary.unknown || 0)} · 已达到 ${esc(summary.ready || 0)}</div>`;
  if (!training.available) {
    h += '<div class="msg err">门槛索引暂不可用；当前排班仍可正常执行。</div>';
  }
  if (rows.length) {
    h += '<div class="infrast-table-wrap"><table class="infrast-table"><thead><tr><th>干员</th><th>设施</th><th>当前</th><th>建议</th><th>状态</th></tr></thead><tbody>';
    h += rows.map(item => {
      const facilities = (item.facilities || []).map(value => facilityNames[value] || value).map(esc).join('、');
      return `<tr><td>${esc(item.name)}</td><td>${facilities || '—'}</td><td>${level(item.current)}</td><td>${item.recommended?'建议 '+level(item.recommended):'门槛未知'}</td><td>${esc(statusNames[item.status] || item.status)}</td></tr>`;
    }).join('');
    h += '</tbody></table></div>';
  }
  return h;
}

function renderOperatorBox() {
  const state = S.operatorBox;
  const data = state.data || {};
  const snapshot = data.snapshot || null;
  const attempt = data.last_attempt || null;
  const comparison = data.comparison || null;
  const running = state.busy || !!(S.status && S.status.active && S.status.task === '干员扫描');
  let h = `<div class="card"><h2>👥 基建干员检查</h2>
    <div class="help">使用 MAA OperBox 扫描；仅收到最终 <code>done=true</code> 才覆盖快照。扫描失败不会阻止日常或基建任务。</div>
    <div class="btn-row"><button class="btn btn-p" onclick="startOperatorScan()" ${running || (S.status&&S.status.active)?'disabled':''}>${running?'扫描中...':'扫描干员'}</button>
    <button class="btn btn-s" onclick="loadOperatorBox()" ${running?'disabled':''}>刷新结果</button></div>`;
  if (state.error) h += `<div class="msg err">${esc(state.error)}</div>`;
  if (!state.loaded) return h + '<div class="help">正在读取扫描状态...</div></div>';
  if (attempt && attempt.status === 'failed') {
    h += `<div class="msg err">最近一次扫描未完成：${esc(attempt.message || attempt.code)}。旧的成功快照${snapshot?'仍保留':'不存在'}。</div>`;
  }
  if (!snapshot) {
    h += `<div class="help">尚无完整扫描快照。每周扫描保持禁用，先完成一次手动扫描。</div>${renderOperatorWeekly()}</div>`;
    return h;
  }
  h += `<div class="help">扫描时间 ${esc(snapshot.scanned_at)} · 已拥有 ${esc(snapshot.owned_count)} / 全部 ${esc(snapshot.all_count)}
    · ${data.stale?'<span class="badge err">数据过期</span>':'<span class="badge ok">数据有效</span>'}</div>`;
  if (comparison) {
    const required = comparison.required_missing || [];
    const candidates = comparison.candidate_missing || [];
    h += `<h3>当前排班缺失</h3>
      <div class="${required.length?'msg err':'msg ok'}">必需缺失 ${esc(required.length)}：${required.length?required.map(esc).join('、'):'无'}</div>
      <div class="help">候选缺失 ${esc(candidates.length)}：${candidates.length?candidates.map(esc).join('、'):'无'}</div>`;
    h += renderOperatorTraining(data.training);
  } else {
    h += '<div class="help">选择一个排班文件后显示必需与候选缺失对比。</div>';
  }
  const ownedNames = (data.owned || []).map(item => item.name).filter(Boolean);
  const unownedNames = (data.unowned || []).map(item => item.name).filter(Boolean);
  h += `<details><summary>已拥有干员（${esc(ownedNames.length)}）</summary><div class="help">${ownedNames.map(esc).join('、') || '无'}</div></details>
    <details><summary>未拥有干员（${esc(unownedNames.length)}）</summary><div class="help">${unownedNames.map(esc).join('、') || '无'}</div></details>
    <div class="help">培养提示来自 MAA 扫描的当前精英/等级与同源游戏资源门槛；不会改动排班 JSON。</div>
    ${renderOperatorWeekly()}</div>`;
  return h;
}

function renderInfrastPlans() {
  const validCount = S.infrastPlans.filter(plan => plan.valid).length;
  let h = '<div class="infrast-library">';
  if (S.infrastSchedule && S.infrastSchedule.analysis) {
    h += renderInfrastSchedule();
  } else {
    h += `<div class="page-head"><div><div class="eyebrow">INFRASTRUCTURE</div><h2>周基建排班</h2><p>选择排班文件后，直接查看七天班次、24 小时时间轴和人员配置。</p></div></div>
      <div class="card"><div class="help">当前没有可展示的生效排班。请在下方“排班文件与维护”中选择、导入并配置排班。</div></div>`;
  }
  h += `<details class="advanced-panel"><summary>排班文件与维护</summary><div class="advanced-body">
    <div class="card"><div class="weekly-toolbar"><div><h2>排班文件库</h2><div class="infrast-plan-meta">这里仅负责选择、导入和维护排班文件，不会改动周计划任务。</div></div>
    <div class="btn-row"><a class="btn btn-s" href="https://ark.yituliu.cn/tools/scheduleV2" target="_blank" rel="noopener noreferrer">↗ 打开一图流排班生成器</a>
    <button class="btn btn-s" onclick="setInfrastImportOpen(true)">导入 JSON</button>
    <button class="btn btn-s" onclick="loadInfrastPlans()">刷新文件库</button></div></div></div>`;
  h += renderOperatorBox();
  h += renderInfrastImport();
  if (S.infrastLibraryError) h += `<div class="msg err">${esc(S.infrastLibraryError)}</div>`;
  if (!S.infrastPlansLoaded && !S.infrastLibraryError) {
    h += '<div class="card">正在读取排班文件库...</div>';
  } else if (!S.infrastPlans.length) {
    h += '<div class="card"><div class="help">暂无排班 JSON。可从生成器下载后选择文件导入，也可粘贴 JSON 文本预览。</div></div>';
  } else {
    h += `<div class="card"><h2>排班文件库</h2><div class="help">共 ${esc(S.infrastPlans.length)} 个文件 · ${esc(validCount)} 个有效 · ${esc(S.infrastPlans.length-validCount)} 个无效</div><div class="infrast-library-grid">`;
    S.infrastPlans.forEach((plan, planIndex) => {
      if (!plan.valid) {
        const error = plan.error || {};
        h += `<div class="infrast-file invalid"><span class="badge err">无效</span><div class="meta">${esc(plan.filename)}</div><div class="title">无效排班</div><div class="meta">${esc(error.code)} · ${esc(error.path)}<br>${esc(error.message)}</div></div>`;
        return;
      }
      const selected = S.infrastSelected && S.infrastSelected.filename === plan.filename ? ' on' : '';
      h += `<button type="button" class="infrast-file${selected}" onclick="selectInfrastPlan(${planIndex})">
        <span class="badge ok">有效</span><div class="meta">${esc(plan.filename)}</div><div class="title">${esc(plan.title)}</div>
        <div class="meta">作者 ${esc(plan.author || '未注明')}<br>SHA ${esc((plan.sha256 || '').slice(0,8))} · ${esc(plan.plan_count)} 班</div></button>`;
    });
    h += '</div></div>';
  }
  if (S.infrastSelected) h += renderInfrastPlanDetails(S.infrastSelected, S.infrastSelectedPlan);
  return h + '</div></details></div>';
}

function renderInfrastPlanDetails(details, selectedIndex) {
  const plans = Array.isArray(details.plans) ? details.plans : [];
  const safeIndex = Number.isInteger(selectedIndex) && selectedIndex >= 0 && selectedIndex < plans.length ? selectedIndex : 0;
  const plan = plans[safeIndex];
  if (!plan) return '<div class="card"><div class="help">排班文件没有可显示的班次。</div></div>';
  const selectAction = S.infrastImport.preview && details === S.infrastImport.preview.details
    ? 'selectInfrastPreviewPlanIndex' : 'selectInfrastPlanIndex';
  const shifts = plans.map((item, index) =>
    `<button class="btn btn-s" onclick="${selectAction}(${index})" style="${index===safeIndex?'border-color:var(--ac);color:var(--ac)':''}">${esc(item.name || `班次 ${index+1}`)}</button>`
  ).join('');
  let h = `<div class="infrast-details"><div class="card"><h2>🧭 ${esc(details.title)}</h2>
    <div class="infrast-plan-meta">文件 ${esc(details.filename)} · 作者 ${esc(details.author || '未注明')} · SHA-256 ${esc(details.sha256)} · 共 ${esc(details.plan_count)} 班</div>
    ${details.description ? `<div class="help">${esc(details.description)}</div>` : ''}
    <div class="infrast-shifts">${shifts}</div>
    <h3>${esc(plan.name)} <span class="infrast-plan-meta">plan_index=${esc(plan.index)}</span></h3>
    ${plan.description ? `<div class="help">${esc(plan.description)}</div>` : ''}`;
  INFRAST_FACILITY_ORDER.forEach(([facility, label]) => {
    const rooms = (plan.rooms && plan.rooms[facility]) || [];
    if (!rooms.length) return;
    h += `<section class="infrast-facility"><h3>${label} · ${esc(rooms.length)} 间</h3><div class="infrast-room-grid">`;
    rooms.forEach(room => {
      const operatorGroups = room.use_operator_groups === undefined ? '未提供' : String(!!room.use_operator_groups);
      h += `<article class="infrast-room"><h4>房间 ${esc(room.index)}</h4><dl>
        <dt>固定干员</dt><dd>${esc((room.operators || []).join('、') || '无')}</dd>
        <dt>候选干员</dt><dd>${esc((room.candidates || []).join('、') || '无')}</dd>
        <dt>产品</dt><dd>${esc(room.product || '未设置')}</dd>
        <dt>skip</dt><dd>${esc(String(!!room.skip))}</dd>
        <dt>autofill</dt><dd>${esc(String(!!room.autofill))}</dd>
        <dt>sort</dt><dd>${esc(String(!!room.sort))}</dd>
        <dt>use_operator_groups</dt><dd>${esc(operatorGroups)}</dd>
      </dl></article>`;
    });
    h += '</div></section>';
  });
  const groups = (plan.groups || []).map(group => `${esc(group.name)}：${esc((group.operators || []).join('、') || '无')}`).join('<br>') || '无';
  const drones = Object.entries(plan.drones || {}).map(([key, value]) => `${esc(key)}=${esc(String(value))}`).join(' · ') || '未设置';
  const fiammetta = Object.entries(plan.Fiammetta || {}).map(([key, value]) => `${esc(key)}=${esc(String(value))}`).join(' · ') || '未设置';
  h += `<div class="infrast-plan-options">
    <div class="infrast-option"><b>干员组 groups</b><br>${groups}</div>
    <div class="infrast-option"><b>无人机 drones</b><br>${drones}</div>
    <div class="infrast-option"><b>菲亚梅塔 Fiammetta</b><br>${fiammetta}</div>
  </div></div></div>`;
  return h;
}

// ─── Tasks (main GUI) ───
function renderTasks(isWeekly=false) {
  let h = '<div class="row"><div class="col-l">';
  // task list card
  h += `<div class="card"><h2>${isWeekly ? '📅 '+planWeekdayName(S.weeklyDay)+'完整配置' : '🎮 一键长草'}</h2><ul class="tl" id="tlist">`;
  S.tasks.forEach((t, i) => {
    const enabled = t._enabled !== false;
    const sel = i === S.sel ? ' sel' : '';
    const icon = taskIcon(t.type);
    const name = t.name || taskName(t.type);
    h += `<li class="${sel}" data-i="${i}">
      <div class="arrows"><button onclick="moveTask(${i},-1);ev(event)">▲</button><button onclick="moveTask(${i},1);ev(event)">▼</button></div>
      <input type="checkbox" class="cb" ${enabled?'checked':''} onchange="toggleTask(${i},this.checked);ev(event)">
      <span class="tn${enabled?'':' off'}" onclick="selTask(${i})">${icon} ${esc(name)}</span>
      <button class="gear" onclick="selTask(${i});ev(event)" title="设置">⚙</button>
      <button class="gear" onclick="rmTask(${i});ev(event)" title="删除" style="color:var(--rd)">✕</button>
    </li>`;
  });
  h += '</ul>';
  // toolbar
  h += `<div class="tbar">
    <select id="addType">${TASK_TYPES.map(t => `<option value="${t.type}">${t.icon} ${t.name}</option>`).join('')}</select>
    <button onclick="addTask()">➕ 添加</button>
    <button onclick="allTasks(true)">全选</button>
    <button onclick="allTasks(false)">清空</button>
  </div>`;
  h += `<div class="tbar">
    <button class="btn btn-p" onclick="${isWeekly?'saveWeeklyDay()':'saveTasks()'}" style="flex:1">💾 保存配置</button>
  </div>`;
  h += `<div class="tbar">
    <button class="start" onclick="doRun('${isWeekly ? S.weeklyDay : 'current'}')" ${S.status&&S.status.active?'disabled':''}>Link Start!</button>
  </div>`;
  h += `<div class="help" style="margin-top:10px"><label style="min-width:auto"><input type="checkbox" ${S.showResourceStages?'checked':''} onchange="setShowResourceStages(this.checked)"> 显示资源 JSON 中的其他关卡</label></div>`;
  const stageGroups = visibleStageGroups();
  if (stageGroups.length) {
    h += '<div class="atip"><b>📢 活动/资源关卡提示</b><br>';
    stageGroups.forEach(a => {
      const src = a.source === 'resource-index' ? '资源索引' : '活动API';
      const left = Number.isInteger(a.days_left) ? ` 剩余 <b>${a.days_left}</b> 天` : '';
      const sync = a.source === 'resource-index' ? (a.runtime_synced ? ' 已同步' : ' 未同步到运行资源') : '';
      const nav = (a.runtime_navigable === undefined) ? ''
        : (a.runtime_navigable ? ' 可导航'
           : (a.source === 'resource-index' ? ' 仅地图索引/未注册导航' : ' 导航待确认'));
      h += `<span class="tag">${esc(a.name)}</span>${esc(a.tip)} <b>${src}</b>${left}${sync}${nav}<br>`;
      a.stages.forEach(s => {
        const drop = s.Drop || '';
        h += `&nbsp;&nbsp;▸ ${esc(s.Display)}${drop ? ' — '+esc(drop) : ''}<br>`;
      });
    });
    h += '</div>';
  }
  if (S.stageDiag && S.stageDiag.warnings && S.stageDiag.warnings.length) {
    h += '<div class="help" style="margin-top:10px"><b>资源诊断</b><br>';
    S.stageDiag.warnings.forEach(w => h += `▸ ${esc(w)}<br>`);
    const v = S.stageDiag.versions || {};
    if (v.runtime || v.clone) {
      h += `运行资源: ${esc((v.runtime||{}).last_updated || 'unknown')} / clone: ${esc((v.clone||{}).last_updated || 'unknown')}<br>`;
    }
    h += '</div>';
  }
  h += '</div></div>';

  h += '<div class="col-r">';
  if (S.sel >= 0 && S.sel < S.tasks.length) {
    h += renderSettingsPanel(S.tasks[S.sel], S.sel);
  } else {
    h += '<div class="card"><h2>⚙ 任务设置</h2><p style="color:var(--t2);font-size:13px">← 点击任务的 ⚙ 查看设置</p></div>';
  }
  h += '</div></div>';
  return h;
}

function renderSettingsPanel(task, idx) {
  const t = task.type;
  const p = task.params || {};
  let h = `<div class="card sp"><h2>⚙ ${taskIcon(t)} ${esc(task.name || taskName(t))} 设置</h2>`;
  h += `<div class="fg"><label>任务名</label><input type="text" value="${esc(task.name||'')}" onchange="S.tasks[${idx}].name=this.value;markTaskDirty();render()"></div>`;

  switch(t) {
    case 'StartUp':
      h += field_select(idx, 'client_type', '客户端', [['Official','官服'],['Bilibili','B服'],['YoStarEN','国际服'],['YoStarJP','日服'],['YoStarKR','韩服']], p.client_type||'Official');
      h += field_check(idx, 'start_game_enabled', '启动游戏', p.start_game_enabled!==false);
      h += field_text(idx, 'account_name', '账号切换', p.account_name||'', '留空不切换');
      break;
    case 'Fight':
      h += renderFightPanel(idx, p, task);
      break;
    case 'Recruit':
      h += field_check(idx, 'refresh', '刷新标签', p.refresh!==false);
      h += field_text(idx, 'select', '自动选择(星级)', JSON.stringify(p.select||[4,5]), '如 [4,5]');
      h += field_text(idx, 'confirm', '确认(星级)', JSON.stringify(p.confirm||[3,4,5]), '如 [3,4,5]');
      h += field_num(idx, 'times', '招募次数', p.times===undefined?4:p.times, 0, 99);
      h += field_check(idx, 'set_time', '设置时间', p.set_time!==false);
      h += field_check(idx, 'expedite', '使用加急许可', p.expedite===true);
      h += field_check(idx, 'skip_robot', '识别到小车词条时跳过', p.skip_robot!==false);
      h += field_select_num(idx, 'extra_tags_mode', '选择更多 Tags 模式', [[0,'默认行为'],[1,'尽量选3个(可能冲突)'],[2,'多高星Tag组合']], p.extra_tags_mode||0);
      h += field_text(idx, 'first_tags', '首选 Tags', JSON.stringify(p.first_tags||[]), '如 ["高级资深干员"]');
      h += field_select(idx, 'server', '服务器', [['CN','国服/官服'],['US','美服'],['JP','日服'],['KR','韩服']], p.server||'CN');
      h += field_check(idx, 'report_to_penguin', '汇报数据到企鹅物流', p.report_to_penguin===true);
      h += field_check(idx, 'report_to_yituliu', '汇报数据到一图流', p.report_to_yituliu===true);
      break;
    case 'Infrast':
      {
        const activeTab = S.infrastTab || 'gen';
        h += `<div style="display:flex;gap:12px;border-bottom:1px solid var(--bd);margin-bottom:12px;padding-bottom:6px">
          <button class="btn btn-s${activeTab==='gen'?' active':''}" onclick="S.infrastTab='gen';render()" style="${activeTab==='gen'?'border-color:var(--yl);color:var(--yl);font-weight:600':''}">常规设置</button>
          <button class="btn btn-s${activeTab==='adv'?' active':''}" onclick="S.infrastTab='adv';render()" style="${activeTab==='adv'?'border-color:var(--yl);color:var(--yl);font-weight:600':''}">高级设置</button>
        </div>`;

        if (activeTab === 'gen') {
          h += field_select_num(idx, 'mode', '基建模式', [[0,'常规模式'],[20000,'队列轮换模式'],[10000,'自定义配置模式(JSON)']], p.mode===undefined?0:p.mode);
          h += field_select(idx, 'drones', '无人机用途', [
            ['_NotUse','不使用无人机'],
            ['Money','贸易站-龙门币'],
            ['SyntheticJade','贸易站-合成玉'],
            ['CombatRecord','制造站-经验书'],
            ['PureGold','制造站-赤金'],
            ['OriginStone','制造站-源石碎片'],
            ['Chip','制造站-芯片组']
          ], p.drones||'_NotUse');

          const thVal = Math.round((p.threshold===undefined?0.3:p.threshold) * 100);
          h += `<div class="fg">
            <label>心情心情阈值: <span id="th_val">${thVal}%</span></label>
            <input type="range" min="0" max="100" step="5" value="${thVal}" oninput="document.getElementById('th_val').textContent=this.value+'%'" onchange="setParam(${idx},'threshold',parseFloat(this.value)/100.0)" style="width:200px">
          </div>`;

          h += `<div class="fg"><label>设施选择</label><div style="display:flex;flex-wrap:wrap;gap:6px;max-width:240px;background:var(--s2);padding:8px;border-radius:6px">`;
          const fac = p.facility || FACILITIES;
          FACILITIES.forEach(f => {
            const ck = fac.includes(f) ? 'checked' : '';
            const fName = FACILITY_MAP[f] || f;
            h += `<label style="font-size:12px;display:flex;align-items:center;gap:3px;width:95px;color:var(--t1)"><input type="checkbox" ${ck} onchange="toggleFac(${idx},'${f}',this.checked)">${fName}</label>`;
          });
          h += `</div></div>`;

          if (p.mode == 10000) {
            h += field_text(idx, 'filename', '排班文件', p.filename||'', 'normal.json');
            h += field_num(idx, 'plan_index', '计划索引', p.plan_index||0, 0, 100);
          }
        } else {
          h += field_check(idx, 'dorm_trust_enabled', '宿舍空余位置蹭信赖', p.dorm_trust_enabled!==false);
          h += field_check(idx, 'dorm_notstationed_enabled', '不将已进驻的干员放入宿舍', p.dorm_notstationed_enabled===true);
          h += field_check(idx, 'replenish', '源石碎片自动补货', p.replenish===true);
          h += field_check(idx, 'reception_message_board', '会客室信息板收取信用', p.reception_message_board!==false);
          h += field_check(idx, 'reception_clue_exchange', '进行线索交流', p.reception_clue_exchange!==false);
          h += field_check(idx, 'reception_send_clue', '赠送线索', p.reception_send_clue!==false);
          h += field_check(idx, 'continue_training', '训练完成后继续尝试专精当前技能', p.continue_training===true);
        }
      }
      break;
    case 'Mall':
      h += field_check(idx, 'shopping', '启用购物', p.shopping!==false);
      h += field_check(idx, 'visit_friends', '访问好友基建获取信用', p.visit_friends!==false);
      h += field_text(idx, 'buy_first', '优先购买', JSON.stringify(p.buy_first||[]), '如 ["招聘许可","龙门币"]');
      h += field_text(idx, 'blacklist', '黑名单', JSON.stringify(p.blacklist||[]), '如 ["家具零件"]');
      h += field_check(idx, 'force_shopping_if_credit_full', '信用满购物时无视黑名单', p.force_shopping_if_credit_full!==false);
      h += field_check(idx, 'only_buy_discount', '只购买折扣物品 (第二轮)', p.only_buy_discount===true);
      h += field_check(idx, 'reserve_max_credit', '信用点低于 300 停止购买', p.reserve_max_credit===true);
      h += field_check(idx, 'credit_fight', '借助战打一局 OF-1 赚取信用', p.credit_fight===true);
      h += field_select_num(idx, 'formation_index', 'OF-1 编队栏位', [[0,'当前编队'],[1,'第一编队'],[2,'第二编队'],[3,'第三编队'],[4,'第四编队']], p.formation_index||0);
      break;
    case 'Award':
      h += field_check(idx, 'award', '领取每日/每周任务奖励', p.award!==false);
      h += field_check(idx, 'mail', '领取所有邮件奖励', p.mail!==false);
      h += field_check(idx, 'recruit', '进行限定池赠送的每日免费单抽', p.recruit===true);
      h += field_check(idx, 'orundum', '领取幸运墙的每日合成玉奖励', p.orundum===true);
      h += field_check(idx, 'mining', '领取限时开采许可的每日合成玉奖励', p.mining===true);
      h += field_check(idx, 'specialaccess', '领取周年赠送月卡奖励', p.specialaccess===true);
      break;
    case 'CloseDown':
      h += field_select(idx, 'client_type', '客户端', [['Official','官服'],['Bilibili','B服'],['YoStarEN','国际服'],['YoStarJP','日服'],['YoStarKR','韩服']], p.client_type||'Official');
      break;
    case 'Roguelike':
      {
        const activeTab = S.rogueTab || 'gen';
        h += `<div style="display:flex;gap:12px;border-bottom:1px solid var(--bd);margin-bottom:12px;padding-bottom:6px">
          <button class="btn btn-s${activeTab==='gen'?' active':''}" onclick="S.rogueTab='gen';render()" style="${activeTab==='gen'?'border-color:var(--yl);color:var(--yl);font-weight:600':''}">常规设置</button>
          <button class="btn btn-s${activeTab==='adv'?' active':''}" onclick="S.rogueTab='adv';render()" style="${activeTab==='adv'?'border-color:var(--yl);color:var(--yl);font-weight:600':''}">高级设置</button>
        </div>`;

        if (activeTab === 'gen') {
          h += field_select(idx, 'theme', '肉鸽主题', [
            ['Phantom','傀影与猩红血钻'],
            ['Mizuki','水月与深蓝之树'],
            ['Sami','探索者的银凇止境'],
            ['Sarkaz','萨卡兹的无终奇语'],
            ['JieGarden','岁的界园志异']
          ], p.theme||'Sami');
          h += field_select_num(idx, 'mode', '肉鸽模式', [
            [0,'刷分/奖励点数'],
            [1,'刷源石锭(第一层重开)'],
            [4,'凹开局(模式4)'],
            [5,'刷工作坍缩范式(模式5)'],
            [6,'刷月度小队蚊子腿'],
            [7,'刷深入调查小队']
          ], p.mode===undefined?0:p.mode);
          h += field_num(idx, 'difficulty', '指定难度', p.difficulty===undefined?0:p.difficulty, 0, 15);
          h += field_num(idx, 'starts_count', '开始探索次数', p.starts_count===undefined?999:p.starts_count, 0, 99999);
          h += field_text(idx, 'squad', '开局分队', p.squad||'指挥分队', '如 指挥分队');
          h += field_text(idx, 'roles', '开局职业组', p.roles||'取长补短', '如 取长补短');
          h += field_text(idx, 'core_char', '开局干员', p.core_char||'', '中文名字，如 羽毛笔');
        } else {
          h += field_check(idx, 'use_support', '开局使用助战干员', p.use_support===true);
          h += field_check(idx, 'use_nonfriend_support', '允许非好友助战干员', p.use_nonfriend_support===true);
          h += field_check(idx, 'stop_at_final_boss', '第5层关卡前停止探索', p.stop_at_final_boss===true);
          h += field_check(idx, 'stop_at_max_level', '肉鸽等级刷满后停止', p.stop_at_max_level===true);
          h += field_check(idx, 'investment_enabled', '自动投资源石锭', p.investment_enabled!==false);
          h += field_check(idx, 'stop_when_investment_full', '投资满后自动停止', p.stop_when_investment_full===true);
          h += field_num(idx, 'investments_count', '最大投资次数', p.investments_count===undefined?999:p.investments_count, 0, 9999);
          h += field_check(idx, 'refresh_trader_with_dice', '使用骰子刷新商店商品', p.refresh_trader_with_dice===true);
        }
      }
      break;
    case 'Reclamation':
      h += field_select(idx, 'theme', '生息演算主题', [
        ['Fire','沙中之火 (已关闭)'],
        ['Tales','沙洲遗闻'],
        ['RelaunchAnchor','重启锚点']
      ], p.theme||'Tales');
      h += field_select_num(idx, 'mode', '演算模式', [
        [0,'无存档刷生息点'],
        [1,'有存档制造道具刷分'],
        [16,'RA-1 自动精耕细作交付'],
        [32,'RA-15 初雪60杀'],
        [48,'RA-4 赤金解锁维什戴尔杀Boss']
      ], p.mode===undefined?0:p.mode);
      h += field_text(idx, 'tools_to_craft', '自动制造物品', JSON.stringify(p.tools_to_craft||["荧光棒"]), '如 ["荧光棒"]');
      h += field_num(idx, 'num_craft_batches', '单次最大制造轮数', p.num_craft_batches===undefined?16:p.num_craft_batches, 1, 99);
      break;
    case 'Copilot':
      h += field_text(idx, 'filename', '作业 JSON 路径', p.filename||'', '相对或绝对路径，如 copilot/1-7.json');
      h += field_num(idx, 'loop_times', '循环执行次数', p.loop_times===undefined?1:p.loop_times, 1, 999);
      h += field_check(idx, 'use_sanity_potion', '理智不足时允许自动吃药', p.use_sanity_potion===true);
      h += field_check(idx, 'formation', '自动编队', p.formation===true);
      h += field_check(idx, 'add_trust', '自动编队以信赖升序填充空位', p.add_trust===true);
      break;
    case 'SSSCopilot':
      h += field_text(idx, 'filename', '保全作业 JSON 路径', p.filename||'', '如 sss/plan.json');
      h += field_num(idx, 'loop_times', '循环执行次数', p.loop_times===undefined?1:p.loop_times, 1, 99);
      break;
    case 'ParadoxCopilot':
      h += field_text(idx, 'filename', '悖论作业 JSON 路径', p.filename||'', '如 paradox/exusiai.json');
      break;
    default:
      h += `<div class="help">此任务类型暂无可视化设置，请在下方编辑原始参数：</div>`;
      h += `<div class="fg"><label>参数JSON</label><textarea style="min-height:100px" onchange="try{S.tasks[${idx}].params=JSON.parse(this.value)}catch(e){}">${esc(JSON.stringify(p,null,2))}</textarea></div>`;
  }

  const vars = task.variants || [];
  if (vars.length) {
    h += `<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--bd)">`;
    h += `<div style="font-size:12px;color:var(--t2);margin-bottom:6px">📐 条件变体 (${vars.length}个)</div>`;
    vars.forEach((v, vi) => {
      const cond = v.condition ? JSON.stringify(v.condition) : '默认';
      const vp = JSON.stringify(v.params || {});
      h += `<div style="background:var(--s2);border-radius:6px;padding:8px;margin-bottom:6px;font-size:11px">
        <div style="color:var(--yl)">条件: ${esc(cond)}</div>
        <div>参数: ${esc(vp)}</div>
      </div>`;
    });
    h += '</div>';
  }

  h += '</div>';
  return h;
}

function isOnSideStoryCondition(cond) {
  return cond && cond.type === 'OnSideStory';
}
function isActivityDateTimeCondition(cond) {
  return cond && cond.type === 'DateTime' && typeof cond.start === 'string' && typeof cond.end === 'string';
}
function isWeekdayCondition(cond) {
  return cond && cond.type === 'Weekday' && Array.isArray(cond.weekdays);
}
function isTimeCondition(cond) {
  return cond && cond.type === 'Time';
}
function hasOnlyParams(params, keys) {
  const actual = Object.keys(params || {});
  return actual.length === keys.length && actual.every(k => keys.includes(k));
}
function cleanWeekdays(days) {
  const valid = WEEKDAYS.map(d => d.v);
  const out = [];
  (days || []).forEach(d => {
    const v = typeof d === 'string' ? d.slice(0,3) : d;
    const found = valid.find(x => x === v);
    if (found && !out.includes(found)) out.push(found);
  });
  return out;
}
function formatWeekdays(days) {
  const set = new Set(cleanWeekdays(days));
  return WEEKDAYS.filter(d => set.has(d.v)).map(d => d.n).join('、') || '未选择开放日';
}
function normalizeCliTime(value) {
  if (!value) return '';
  const m = String(value).match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return '';
  const h = Math.max(0, Math.min(23, parseInt(m[1], 10) || 0));
  const min = Math.max(0, Math.min(59, parseInt(m[2], 10) || 0));
  const sec = Math.max(0, Math.min(59, parseInt(m[3] || '0', 10) || 0));
  return `${String(h).padStart(2,'0')}:${String(min).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
}
function timeForInput(value) {
  const t = normalizeCliTime(value);
  return t ? t.slice(0,5) : '';
}
function normalizeCliDateTime(value) {
  if (!value) return '';
  const m = String(value).match(/^(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})/);
  if (!m) return '';
  return `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`;
}
function findActivityWindowForStage(stage) {
  if (!stage || !S.stageDiag) return null;
  const groups = [...(S.stageDiag.active || []), ...(S.stageDiag.expired || [])];
  for (const group of groups) {
    if (group.source !== 'stage-api') continue;
    const stages = group.stages || [];
    if (!stages.some(s => s.Value === stage)) continue;
    const start = normalizeCliDateTime(group.start_cli || group.start);
    const end = normalizeCliDateTime(group.end_cli || group.end);
    if (!start || !end) continue;
    const timezone = Number.isFinite(Number(group.timezone)) ? Number(group.timezone) : 8;
    return {start, end, timezone};
  }
  return null;
}
function activityStageStatus(stage) {
  if (!stage || !S.stageDiag) return null;
  const find = arr => (arr || []).find(g => g.source === 'stage-api' && (g.stages || []).some(s => s.Value === stage));
  const act = find(S.stageDiag.active);
  if (act) return {state: 'active', navigable: !!act.runtime_navigable, days_left: act.days_left, name: act.name};
  const exp = find(S.stageDiag.expired);
  if (exp) return {state: 'expired', end: exp.end_cli || exp.end, name: exp.name};
  return null;
}
function buildActivityDateTimeCondition(stage, previousCondition) {
  const window = findActivityWindowForStage(stage);
  if (window) {
    return {type: 'DateTime', start: window.start, end: window.end, timezone: window.timezone};
  }
  if (isActivityDateTimeCondition(previousCondition)) {
    return previousCondition;
  }
  return null;
}
function getFightActivityWindow(stage, model) {
  // A corrupt old DateTime variant does not turn a permanent stage into an activity.
  if (commonStageEntry(stage) || STAGE_OPEN_WEEKDAYS[stage]) return null;
  const official = findActivityWindowForStage(stage);
  if (official) return official;
  if (model.activityStage !== stage || !isActivityDateTimeCondition(model.activityCondition)) return null;
  const condition = model.activityCondition;
  return {start:condition.start, end:condition.end, timezone:condition.timezone ?? 8};
}
function getFightModel(task, p) {
  const vars = task.variants || [];
  const params = p || task.params || {};
  const oldExpireDays = params.medicine_expire_days !== undefined
    ? (parseInt(params.medicine_expire_days, 10) || 0)
    : (params.expiring_medicine ? 2 : 0);
  const model = {
    baseStage: params.stage || '',
    activityEnabled: false,
    activityStage: '',
    activityCondition: null,
    backups: [],
    expireDays: oldExpireDays,
    expireTimed: false,
    expireStart: '',
    other: []
  };
  vars.forEach(v => {
    const vp = v.params || {};
    if (!v.condition && typeof vp.stage === 'string' && hasOnlyParams(vp, ['stage'])) {
      model.baseStage = vp.stage;
      return;
    }
    if (isWeekdayCondition(v.condition) && typeof vp.stage === 'string' && hasOnlyParams(vp, ['stage'])) {
      model.backups.push({stage: vp.stage, weekdays: cleanWeekdays(v.condition.weekdays)});
      return;
    }
    if ((isOnSideStoryCondition(v.condition) || isActivityDateTimeCondition(v.condition)) && typeof vp.stage === 'string' && hasOnlyParams(vp, ['stage'])) {
      model.activityEnabled = true;
      model.activityStage = vp.stage;
      model.activityCondition = v.condition;
      return;
    }
    if (isTimeCondition(v.condition) && vp.medicine_expire_days !== undefined && hasOnlyParams(vp, ['medicine_expire_days'])) {
      model.expireTimed = true;
      model.expireDays = parseInt(vp.medicine_expire_days, 10) || 0;
      model.expireStart = normalizeCliTime(v.condition.start || '');
      return;
    }
    model.other.push(v);
  });
  model.backups.reverse();
  model.hasComplexVariants = model.other.length > 0;
  return model;
}
function applyFightModel(idx, model) {
  markTaskDirty();
  const task = S.tasks[idx];
  if (!task.params) task.params = {};
  // 不变量：正在开放的限时活动关卡不能写成无条件主关卡，否则活动结束后仍会一直打、
  // 导航失败导致 Fight 报错（历史 CF-8 类故障）。自动转为带活动 DateTime 窗口的覆盖，
  // 并回落到非活动兜底关卡 1-7，让活动结束后自动落回。
  if (!model.activityEnabled) {
    const st = activityStageStatus(model.baseStage);
    if (st && st.state === 'active') {
      model.activityEnabled = true;
      model.activityStage = model.baseStage;
      model.baseStage = '1-7';
    }
  }
  const p = task.params;
  const managedStage = model.activityEnabled || model.backups.length > 0;
  delete p.expiring_medicine;
  if (managedStage) {
    delete p.stage;
  } else {
    p.stage = model.baseStage || '';
  }
  if (model.expireTimed && model.expireDays > 0 && model.expireStart) {
    p.medicine_expire_days = 0;
  } else {
    p.medicine_expire_days = Math.max(0, parseInt(model.expireDays, 10) || 0);
  }

  const nextVars = [...(model.other || [])];
  if (managedStage) {
    if (model.baseStage) nextVars.push({params: {stage: model.baseStage}});
    [...model.backups].reverse().forEach(b => {
      const weekdays = cleanWeekdays(b.weekdays);
      if (b.stage && weekdays.length) {
        nextVars.push({condition: {type: 'Weekday', weekdays}, params: {stage: b.stage}});
      }
    });
    if (model.activityEnabled && model.activityStage) {
      const activityCondition = buildActivityDateTimeCondition(model.activityStage, model.activityCondition);
      if (activityCondition) {
        nextVars.push({condition: activityCondition, params: {stage: model.activityStage}});
      }
    }
  }
  if (model.expireTimed && model.expireDays > 0 && model.expireStart) {
    // maa-cli skips the whole task if no variant matches, not just the medicine override.
    if (!nextVars.some(v => !v.condition || v.condition.type === 'Always')) {
      nextVars.unshift({params: {stage: model.baseStage || ''}});
    }
    nextVars.push({condition: {type: 'Time', start: model.expireStart}, params: {medicine_expire_days: Math.max(0, parseInt(model.expireDays, 10) || 0)}});
  }

  task.variants = nextVars;
  if (nextVars.length) task.strategy = 'merge';
  else delete task.strategy;
}
function getFightPrimaryStage(task, p) {
  const m = getFightModel(task, p);
  return m.activityEnabled ? (m.activityStage || '') : (m.baseStage || '');
}
function enableFightFallback(idx, on) {
  const task = S.tasks[idx];
  if (!task.params) task.params = {};
  const m = getFightModel(task, task.params);
  if (on) {
    if (m.hasComplexVariants && !confirm('此任务已有自定义 variants。WebUI 会保留它们，并叠加活动条件覆盖；如果自定义 variants 也改 stage，最终以后面的匹配项为准。继续？')) {
      render();
      return;
    }
    m.activityEnabled = true;
    m.activityStage = m.activityStage || m.baseStage || task.params.stage || '';
    m.baseStage = m.baseStage || '1-7';
  } else {
    m.baseStage = m.baseStage || '1-7';
    m.activityEnabled = false;
    m.activityStage = '';
    m.activityCondition = null;
  }
  applyFightModel(idx, m);
  render();
}
function setFightPrimaryStage(idx, value) {
  const task = S.tasks[idx];
  if (!task.params) task.params = {};
  const m = getFightModel(task, task.params);
  const window = getFightActivityWindow(value, m);
  if (window) {
    m.activityEnabled = true;
    m.activityStage = value;
    m.activityCondition = {type:'DateTime', ...window};
    m.baseStage = m.baseStage || '1-7';
  } else {
    m.baseStage = value;
    m.activityEnabled = false;
    m.activityStage = '';
    m.activityCondition = null;
  }
  applyFightModel(idx, m);
  render();
}
function setFightFallbackStage(idx, value) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  m.baseStage = value;
  applyFightModel(idx, m);
}
function addFightBackupStage(idx) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  if (m.hasComplexVariants && !confirm('此任务已有自定义 variants。WebUI 会保留它们，并叠加备选关卡规则；如果自定义 variants 也改 stage，最终以后面的匹配项为准。继续？')) {
    render();
    return;
  }
  m.baseStage = m.baseStage || '1-7';
  m.backups.push({stage: 'CE-6', weekdays: ['Tue','Thu','Sat']});
  applyFightModel(idx, m);
  render();
}
function setFightBackupStage(idx, bi, value) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  if (m.backups[bi]) m.backups[bi].stage = value;
  applyFightModel(idx, m);
}
function toggleFightBackupWeekday(idx, bi, day, on) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  if (!m.backups[bi]) return;
  let days = cleanWeekdays(m.backups[bi].weekdays);
  if (on && !days.includes(day)) days.push(day);
  if (!on) days = days.filter(d => d !== day);
  m.backups[bi].weekdays = days;
  applyFightModel(idx, m);
}
function moveFightBackupStage(idx, bi, dir) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  const ni = bi + dir;
  if (ni < 0 || ni >= m.backups.length) return;
  [m.backups[bi], m.backups[ni]] = [m.backups[ni], m.backups[bi]];
  applyFightModel(idx, m);
  render();
}
function removeFightBackupStage(idx, bi) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  m.backups.splice(bi, 1);
  applyFightModel(idx, m);
  render();
}
function setFightExpireDays(idx, value) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  m.expireDays = Math.max(0, parseInt(value, 10) || 0);
  if (m.expireDays === 0) {
    m.expireTimed = false;
    m.expireStart = '';
  }
  applyFightModel(idx, m);
  render();
}
function setFightExpireStart(idx, value) {
  const task = S.tasks[idx];
  const m = getFightModel(task, task.params || {});
  m.expireStart = normalizeCliTime(value);
  m.expireTimed = !!m.expireStart;
  if (m.expireTimed && m.expireDays === 0) m.expireDays = 2;
  applyFightModel(idx, m);
  render();
}

function renderFightPanel(idx, p, task) {
  let h = '';
  let stageOpts = '<option value="">当前/上次关卡</option>';
  const currentStage = getFightPrimaryStage(task, p);
  const stageGroups = visibleStageGroups();
  if (stageGroups.length) {
    stageGroups.forEach(a => {
      const label = a.source === 'resource-index'
        ? `🧭 ${a.name} (${a.runtime_navigable ? '资源索引/可导航' : '资源索引/仅地图索引'})`
        : `🎪 ${a.name} (剩${a.days_left}天)`;
      stageOpts += `<optgroup label="${esc(label)}">`;
      a.stages.forEach(s => {
        const drop = s.Drop ? ` — ${s.Drop}` : '';
        const selected = s.Value === currentStage ? ' selected' : '';
        stageOpts += `<option value="${esc(s.Value)}"${selected}>${esc(s.Display)}${esc(drop)}</option>`;
      });
      stageOpts += '</optgroup>';
    });
  }
  stageOpts += renderCommonStageOptions(currentStage, currentPlanWeekday());
  const model = getFightModel(task, p);
  const stageLabel = model.activityEnabled ? '活动主关卡' : (model.backups.length ? '默认/兜底关卡' : '关卡指定');
  h += `<div class="fg"><label>关卡列表</label><label style="min-width:auto"><input type="checkbox" ${S.showResourceStages?'checked':''} onchange="setShowResourceStages(this.checked)"> 显示资源 JSON 中的其他关卡</label></div>`;
  h += `<div class="fg"><label>${stageLabel}</label>
    <select onchange="setFightPrimaryStage(${idx},this.value)" style="width:220px">${stageOpts}</select>
  </div>`;
  h += `<div class="fg"><label></label><input type="text" placeholder="手动输入关卡 (如 TD-8)" value="${esc(currentStage)}" onchange="setFightPrimaryStage(${idx},this.value)" style="width:220px"></div>`;
  const stStatus = activityStageStatus(currentStage);
  if (stStatus && stStatus.state === 'expired') {
    h += `<div class="help" style="color:var(--rd)">⚠ 当前主关卡 <b>${esc(currentStage)}</b> 属于已结束的活动「${esc(stStatus.name||'')}」（${esc(stStatus.end||'')} 结束），MAA 已无法导航，会导致作战失败。请改为兜底关卡，或勾选下方“活动开放覆盖”让其按活动时间窗自动回落。</div>`;
  } else if (stStatus && stStatus.state === 'active' && !stStatus.navigable) {
    h += `<div class="help" style="color:var(--or)">⚠ 当前主关卡 <b>${esc(currentStage)}</b> 在官方活动开放期内，但没有独立的活动导航配置；已允许选择，MAA 可能通过通用导航进入，请保留兜底关卡。</div>`;
  }

  h += `<div class="fg"><label>活动开放覆盖</label>
    <input type="checkbox" ${model.activityEnabled?'checked':''} onchange="enableFightFallback(${idx},this.checked)">
    <button class="btn btn-s" onclick="enableFightFallback(${idx},true)" type="button">活动开放刷主关，否则刷兜底</button>
  </div>`;
  if (model.activityEnabled) {
    h += `<div class="fg"><label>未开放兜底关卡</label><input type="text" placeholder="如 1-7" value="${esc(model.baseStage || '1-7')}" onchange="setFightFallbackStage(${idx},this.value)" style="width:220px"></div>`;
    h += `<div class="help">判断条件来自官方 <code>StageActivityV2</code> 中该关卡自己的起止时间，保存为 maa-cli <code>DateTime</code> variant；活动结束后会自动落回“未开放兜底关卡”和下面的备选关卡规则。这是启动前选关策略，不是截图/导航失败后的重试。</div>`;
  } else if (model.hasComplexVariants) {
    h += `<div class="help">此任务已有自定义 variants。WebUI 会保留它们，但无法完整解释；如果自定义 variants 也修改 stage，最终以后面匹配到的 stage 为准。</div>`;
  }

  h += `<div class="help"><b>备选关卡（按开放日）</b><br>用于“第一顺位关卡不在今天开放时，自动刷下一顺位”。优先级：活动开放覆盖 &gt; 备选关卡（上面的优先） &gt; 默认/兜底关卡。保存为 maa-cli <code>Weekday</code> variants，不是运行失败后重跑。</div>`;
  model.backups.forEach((b, bi) => {
    const days = new Set(cleanWeekdays(b.weekdays));
    const checks = WEEKDAYS.map(d => `<label style="min-width:auto;margin-right:8px"><input type="checkbox" ${days.has(d.v)?'checked':''} onchange="toggleFightBackupWeekday(${idx},${bi},'${d.v}',this.checked)"> ${d.n}</label>`).join('');
    h += `<div class="fg"><label>备选 ${bi + 1}</label>
      <input type="text" value="${esc(b.stage)}" placeholder="如 CE-6" onchange="setFightBackupStage(${idx},${bi},this.value)" style="width:120px">
      <span style="font-size:11px;color:var(--t2);margin-left:6px">${esc(formatWeekdays(b.weekdays))}</span>
      <button class="btn btn-s" onclick="moveFightBackupStage(${idx},${bi},-1)" type="button">上移</button>
      <button class="btn btn-s" onclick="moveFightBackupStage(${idx},${bi},1)" type="button">下移</button>
      <button class="btn btn-s" onclick="removeFightBackupStage(${idx},${bi})" type="button">删除</button>
    </div>`;
    h += `<div class="fg"><label></label><div>${checks}</div></div>`;
  });
  h += `<div class="fg"><label></label><button class="btn btn-s" onclick="addFightBackupStage(${idx})" type="button">添加备选关卡</button></div>`;

  const expireOpts = [[0,'不自动使用'],[1,'1天内过期'],[2,'2天内过期'],[3,'3天内过期'],[7,'7天内过期'],[14,'14天内过期']];
  h += `<div class="fg"><label>即将过期药</label><select onchange="setFightExpireDays(${idx},this.value)">${expireOpts.map(([v,n]) => `<option value="${v}" ${v==model.expireDays?'selected':''}>${esc(n)}</option>`).join('')}</select></div>`;
  h += `<div class="fg"><label>过期药生效时间</label><input type="time" value="${esc(timeForInput(model.expireStart))}" onchange="setFightExpireStart(${idx},this.value)"> <span style="font-size:11px;color:var(--t2)">留空表示本任务运行时全天生效</span></div>`;
  if (model.expireDays > 0) {
    const when = model.expireTimed ? `每天 ${timeForInput(model.expireStart)} 之后` : '本任务运行时';
  h += `<div class="help">会使用距离到期不超过 ${model.expireDays} 天的理智药；当前设置为：${esc(when)} 生效。</div>`;
  }

  h += field_num(idx, 'medicine', '使用药剂', p.medicine===undefined?0:p.medicine, 0, 999);
  h += field_num(idx, 'stone', '使用源石', p.stone===undefined?0:p.stone, 0, 999);
  h += field_num(idx, 'times', '指定次数', p.times===undefined?999:p.times, 0, 999);
  h += field_select_num(idx, 'series', '连战次数', [[0,'自动 (Auto)'],[-1,'禁用连战'],[1,'1次'],[2,'2连战'],[3,'3连战'],[4,'4连战'],[5,'5连战'],[6,'6连战']], p.series===undefined?0:p.series);
  h += field_check(idx, 'DrGrandet', '省理智碎石模式 (DrGrandet)', p.DrGrandet===true);
  h += field_select(idx, 'server', '服务器', [['CN','国服/官服'],['US','美服'],['JP','日服'],['KR','韩服']], p.server||'CN');
  h += field_select(idx, 'client_type', '崩溃重启客户端', [['','不启用'],['Official','官服'],['Bilibili','B服']], p.client_type||'');
  h += field_check(idx, 'report_to_penguin', '企鹅物流数据汇报', p.report_to_penguin===true);
  h += field_check(idx, 'report_to_yituliu', '一图流数据汇报', p.report_to_yituliu===true);
  if (p.report_to_yituliu === true) {
    h += field_text(idx, 'yituliu_id', '一图流汇报 ID', p.yituliu_id||'', '留空使用默认ID');
  }
  return h;
}

// ─── Field helpers ───
function field_check(idx, key, label, val) {
  return `<div class="fg"><label>${esc(label)}</label><input type="checkbox" ${val?'checked':''} onchange="setParam(${idx},'${key}',this.checked)"></div>`;
}
function field_num(idx, key, label, val, min, max) {
  return `<div class="fg"><label>${esc(label)}</label><input type="number" value="${val}" min="${min}" max="${max}" onchange="setParamNum(${idx},'${key}',this.value)"></div>`;
}
function field_text(idx, key, label, val, ph) {
  return `<div class="fg"><label>${esc(label)}</label><input type="text" value="${esc(val)}" placeholder="${esc(ph||'')}" onchange="setParamStr(${idx},'${key}',this.value)"></div>`;
}
function field_select(idx, key, label, opts, val) {
  let o = opts.map(([v,n]) => `<option value="${v}" ${v==val?'selected':''}>${esc(n)}</option>`).join('');
  return `<div class="fg"><label>${esc(label)}</label><select onchange="setParam(${idx},'${key}',this.value)">${o}</select></div>`;
}
function field_select_num(idx, key, label, opts, val) {
  let o = opts.map(([v,n]) => `<option value="${v}" ${v==val?'selected':''}>${esc(n)}</option>`).join('');
  return `<div class="fg"><label>${esc(label)}</label><select onchange="setParamNum(${idx},'${key}',this.value)">${o}</select></div>`;
}

// ─── Task actions ───
function ev(e) { e.stopPropagation(); }
function selTask(i) { S.sel = (S.sel === i ? -1 : i); render(); }
function toggleTask(i, on) {
  S.tasks[i]._enabled = on;
  markTaskDirty();
  render();
}
function moveTask(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= S.tasks.length) return;
  [S.tasks[i], S.tasks[j]] = [S.tasks[j], S.tasks[i]];
  if (S.sel === i) S.sel = j;
  else if (S.sel === j) S.sel = i;
  markTaskDirty();
  render();
}
function rmTask(i) {
  if (!confirm('删除任务 ' + (S.tasks[i].name || taskName(S.tasks[i].type)) + ' ?')) return;
  S.tasks.splice(i, 1);
  if (S.sel >= S.tasks.length) S.sel = S.tasks.length - 1;
  markTaskDirty();
  render();
}
function addTask() {
  const type = document.getElementById('addType').value;
  const defaults = getDefaultParams(type);
  S.tasks.push({type, name: '', params: defaults, _enabled: true, variants: []});
  S.sel = S.tasks.length - 1;
  markTaskDirty();
  render();
}
function allTasks(on) {
  S.tasks.forEach(t => t._enabled = on);
  markTaskDirty();
  render();
}

function setParam(idx, key, val) {
  if (!S.tasks[idx].params) S.tasks[idx].params = {};
  if (typeof val === 'string' && (val === 'true' || val === 'false')) val = val === 'true';
  S.tasks[idx].params[key] = val;
  markTaskDirty();
}
function setParamNum(idx, key, val) { setParam(idx, key, parseInt(val) || 0); }
function setParamStr(idx, key, val) {
  if (!S.tasks[idx].params) S.tasks[idx].params = {};
  try { S.tasks[idx].params[key] = JSON.parse(val); }
  catch { S.tasks[idx].params[key] = val; }
  markTaskDirty();
}
function toggleFac(idx, fac, on) {
  if (!S.tasks[idx].params) S.tasks[idx].params = {};
  let f = S.tasks[idx].params.facility || [...FACILITIES];
  if (on && !f.includes(fac)) f.push(fac);
  if (!on) f = f.filter(x => x !== fac);
  S.tasks[idx].params.facility = f;
  markTaskDirty();
}

function getDefaultParams(type) {
  switch(type) {
    case 'StartUp': return {client_type:'Official', start_game_enabled:true};
    case 'Fight': return {stage:'', medicine:0, medicine_expire_days:0, stone:0, times:999, series:0, report_to_penguin:false, DrGrandet:false, server:'CN', client_type:''};
    case 'Recruit': return {refresh:true, select:[4,5], confirm:[3,4,5], times:4, set_time:true, expedite:false, extra_tags_mode:0, skip_robot:true};
    case 'Infrast': return {
      mode: 0,
      facility: [...FACILITIES],
      drones: '_NotUse',
      threshold: 0.3,
      dorm_trust_enabled: false,
      dorm_notstationed_enabled: true,
      replenish: true,
      reception_message_board: true,
      reception_clue_exchange: true,
      reception_send_clue: true,
      continue_training: true
    };
    case 'Mall': return {
      shopping:true,
      buy_first:["招聘许可","龙门币"],
      blacklist:["家具零件","加急许可"],
      force_shopping_if_credit_full:true,
      visit_friends:true,
      only_buy_discount:false,
      reserve_max_credit:false,
      credit_fight:false,
      formation_index:0
    };
    case 'Award': return {
      award: true,
      mail: true,
      recruit: true,
      orundum: true,
      mining: true,
      specialaccess: true
    };
    case 'CloseDown': return {client_type:'Official'};
    case 'Roguelike': return {
      theme:'Sami',
      mode:0,
      starts_count:999,
      difficulty:0,
      squad:'指挥分队',
      roles:'取长补短',
      core_char:'',
      use_support:false,
      use_nonfriend_support:false,
      stop_at_final_boss:false,
      stop_at_max_level:false,
      investment_enabled:true,
      stop_when_investment_full:false,
      investments_count:999
    };
    case 'Reclamation': return {
      theme:'Tales',
      mode:0,
      tools_to_craft:['荧光棒'],
      num_craft_batches:16
    };
    case 'Copilot': return {
      filename:'',
      loop_times:1,
      use_sanity_potion:false,
      formation:false,
      add_trust:false
    };
    case 'SSSCopilot': return {
      filename:'',
      loop_times:1
    };
    case 'ParadoxCopilot': return {
      filename:''
    };
    default: return {};
  }
}

function bindTaskEvents() {}

// ─── Logs ───
function renderLogs() {
  return `<div class="card"><h2>📄 运行日志</h2><div class="logbox" id="logbox" style="max-height:350px">加载中...</div>
    <div class="btn-row"><button class="btn btn-s" onclick="loadLogs()">🔄 刷新</button></div></div>
    <div class="card"><h2>🔍 MaaCore 日志</h2><div class="logbox" id="corelog" style="max-height:350px">加载中...</div></div>`;
}
async function loadLogs() {
  const d = await api('logs');
  const el = document.getElementById('logbox');
  if (el) { el.textContent = d.log || '暂无'; el.scrollTop = el.scrollHeight; }
  const el2 = document.getElementById('corelog');
  if (el2) el2.textContent = d.core_log || '暂无';
}

// ─── Cron ───
function renderCron() {
  return `<div class="card"><h2>⏰ 定时任务</h2>
    <textarea id="crontext" spellcheck="false">加载中...</textarea>
    <div class="btn-row"><button class="btn btn-p" onclick="saveCron()">💾 保存</button></div>
    <div class="help"><b>格式:</b> 分 时 日 月 周 命令<br>
    <b>示例:</b> <code>0 10 * * * /root/maa-scripts/maa-run.sh daily >> /var/log/maa/cron.log 2>&1</code></div>
  </div>`;
}
async function loadCron() {
  const d = await api('crontab');
  const el = document.getElementById('crontext');
  if (el) el.value = d.content || '';
}
async function saveCron() {
  const v = document.getElementById('crontext').value;
  const r = await api('crontab', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({content:v})});
  showMsg(r.ok ? 'ok' : 'err', r.ok ? '定时任务已保存 ✓' : '保存失败');
}

// ─── Connection ───
function renderConn() {
  return `<div class="card"><h2>🔗 连接配置</h2>
    <textarea id="proftext" spellcheck="false" style="min-height:200px">加载中...</textarea>
    <div class="btn-row"><button class="btn btn-p" onclick="saveConn('profile')">💾 保存连接</button></div></div>
    <div class="card"><h2>⚙ CLI 配置</h2>
    <textarea id="clitext" spellcheck="false" style="min-height:180px">加载中...</textarea>
    <div class="btn-row"><button class="btn btn-p" onclick="saveConn('cli')">💾 保存CLI</button></div></div>`;
}
async function loadConn() {
  const d = await api('files');
  document.getElementById('proftext').value = d.profile || '';
  document.getElementById('clitext').value = d.cli || '';
}
async function saveConn(which) {
  const val = document.getElementById(which === 'profile' ? 'proftext' : 'clitext').value;
  const r = await api('files', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({file:which, content:val})});
  showMsg(r.ok ? 'ok' : 'err', r.ok ? '已保存 ✓' : '保存失败');
}

function esc(s) { if (s === null || s === undefined) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// ═══ Init ═══
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && S.infrastSchedule.selectedCell) {
    closeInfrastScheduleDrawer();
  }
});
(async () => { await loadAll(); go('home'); })();
</script>
</body>
</html>'''.replace(
        '/* __WEEKLY_EDITOR_SCRIPT__ */',
        (Path(__file__).parent / 'maa-weekly-editor.js').read_text(encoding='utf-8'),
    ).replace(
        '/* __CULTIVATION_UI_SCRIPT__ */',
        (Path(__file__).parent / 'maa-cultivation-ui.js').read_text(encoding='utf-8'),
    )


def web_html():
    """Return the exact generated WebUI artifact used by tests and HTTP."""
    return get_html()


# ═══════════════════════════════════════
# HTTP Server
# ═══════════════════════════════════════
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _html(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            import sys
            print(f"JSON parse error: {e}", file=sys.stderr)
            print(f"Raw body (first 500): {raw[:500]}", file=sys.stderr)
            return {}

    def _import_body(self):
        content_length = self.headers.get("Content-Length")
        if not isinstance(content_length, str) or not re.fullmatch(
            r"[0-9]+", content_length
        ):
            raise PlanImportError("invalid_request", "Content-Length 无效")
        length = int(content_length)
        if length < 1:
            raise PlanImportError("invalid_request", "请求体不能为空")
        if length > MAX_IMPORT_BODY_BYTES:
            raise PlanImportError("request_too_large", "请求体超过大小限制")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise PlanImportError("invalid_request", "请求体长度不完整")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PlanImportError("invalid_request", "请求体不是有效的 UTF-8 JSON")
        if not isinstance(body, dict):
            raise PlanImportError("invalid_request", "请求体必须是 JSON 对象")
        return body

    def _schedule_body(self):
        content_length = self.headers.get("Content-Length")
        if not isinstance(content_length, str) or not re.fullmatch(
            r"[0-9]+", content_length
        ):
            raise ScheduleError("invalid_request", "$", "Content-Length 无效")
        length = int(content_length)
        if length < 1:
            raise ScheduleError("invalid_request", "$", "请求体不能为空")
        if length > MAX_SCHEDULE_BODY_BYTES:
            raise ScheduleError("request_too_large", "$", "请求体超过大小限制")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ScheduleError("invalid_request", "$", "请求体长度不完整")

        def reject_duplicates(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value

        try:
            body = json.loads(
                raw.decode("utf-8"), object_pairs_hook=reject_duplicates
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ScheduleError(
                "invalid_request", "$", "请求体不是有效的 UTF-8 JSON 对象"
            )
        if not isinstance(body, dict):
            raise ScheduleError("invalid_request", "$", "请求体必须是 JSON 对象")
        return body

    def _cultivation_body(self):
        """Strict, size-limited JSON body for the cultivation API only."""
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return {}
        if not isinstance(content_length, str) or not re.fullmatch(
            r"[0-9]+", content_length
        ):
            raise CultivationRequestError("Content-Length 无效")
        length = int(content_length)
        if length > MAX_CULTIVATION_BODY_BYTES:
            raise CultivationRequestError(
                "请求体超过大小限制", status=413, code="request_too_large"
            )
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise CultivationRequestError("请求体长度不完整")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CultivationRequestError("请求体不是有效的 UTF-8 JSON")
        if not isinstance(body, dict):
            raise CultivationRequestError("请求体必须是 JSON 对象")
        return body

    def _cultivation_post(self, action=None, mode=None):
        try:
            body = self._cultivation_body()
        except CultivationRequestError as error:
            self._json(error.status, cultivation_error_payload(error, error.status))
            return
        if mode is not None:
            # Drain-worthy body is already read; now gate and start.
            if mode == "run":
                blocked = cultivation_run_gate()
                if blocked is not None:
                    status, payload = blocked
                    self._json(status, payload)
                    return
            elif mode == "mastery":
                blocked = cultivation_mastery_run_gate()
                if blocked is not None:
                    status, payload = blocked
                    self._json(status, payload)
                    return
            ok = run_cultivation_bg(mode)
            if ok:
                self._json(200, {"ok": True, "mode": mode})
                return
            self._json(
                409,
                {
                    "ok": False,
                    "mode": mode,
                    "error": {
                        "code": "busy",
                        "message": "已有任务正在运行，请稍后再试",
                    },
                },
            )
            return
        status, payload = cultivation_dispatch(action, body)
        self._json(status, payload)

    def _schedule_error(self, error):
        if isinstance(error, (ScheduleError, PlanValidationError, OperatorBoxError)):
            code = error.code
            path = error.path if isinstance(error.path, str) and error.path.startswith("$") else "$"
            message = error.message
            if (
                re.search(
                    r"(?<![A-Za-z0-9])/(?!/)[^\s\[\]{}()<>,;\"']+",
                    message,
                )
                or re.search(r"[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])*[^\\/\s]*", message)
                or re.search(r"\\\\[^\\/\s]+[\\/][^\\/\s]+", message)
            ):
                message = "排班请求无法完成"
            if code in {"invalid_request", "invalid_filename", "file_not_regular"}:
                status = 400
            elif code in {"file_not_found", "snapshot_missing"}:
                status = 404
            elif code in {
                "stale_preview",
                "transaction_id_reused",
                "pending_transaction",
                "recovery_conflict",
                "ambiguous_pending_transaction",
                "unresolved_daily_entries",
                "conflict_unconfirmed",
                "unmanaged_duplicate",
            }:
                status = 409
            elif code in {"request_too_large", "file_too_large"}:
                status = 413
            else:
                status = 422
        else:
            code = "server_error"
            path = "$"
            message = "排班服务暂时不可用"
            status = 500
        self._json(
            status,
            {
                "ok": False,
                "error": {"code": code, "path": path, "message": message},
            },
        )

    def _import_error(self, error):
        if isinstance(error, (PlanImportError, PlanValidationError)):
            code = error.code
            message = error.message
            status = infrast_import_error_status(error)
        else:
            code = "server_error"
            message = "导入基建排班失败"
            status = 500
        self._json(status, {
            "ok": False,
            "error": {"code": code, "message": message},
        })

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path

        if p == "/" or p == "":
            self._html(200, web_html())
            return

        if p == "/api/config":
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    tasks = load_tasks(TASK_FILE)
                # mark all enabled by default
                for t in tasks:
                    if "_enabled" not in t: t["_enabled"] = True
                self._json(200, {
                    "tasks": tasks,
                    "version": get_maa_version(),
                    "adb": get_adb_status(),
                    "common_stages": _load_common_stage_catalog(RESOURCE_DIR),
                })
            except Exception as error:
                self._schedule_error(error)
        elif p == "/api/weekly-plan":
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    payload = get_weekly_plan_payload()
                self._json(200, payload)
            except Exception as error:
                self._schedule_error(error)
        elif p == "/api/infrast/plans":
            self._json(200, get_infrast_plan_list_payload())
        elif p == "/api/infrast/plan":
            filename_values = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True
            ).get("filename", [])
            if len(filename_values) != 1 or not filename_values[0]:
                self._json(400, {"error": {
                    "code": "invalid_filename",
                    "path": "$",
                    "message": "filename 必须恰好出现一次且非空",
                }})
                return
            filename = filename_values[0]
            try:
                self._json(200, get_infrast_plan_payload(CONFIG_DIR, filename))
            except PlanValidationError as error:
                self._json(infrast_error_status(error), {"error": {
                    "code": error.code,
                    "path": error.path,
                    "message": error.message,
                }})
        elif p == "/api/infrast/schedule":
            pairs = urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=False
            )
            if (
                len(pairs) != 1
                or pairs[0][0] != "filename"
                or not pairs[0][1]
            ):
                self._schedule_error(
                    ScheduleError(
                        "invalid_request",
                        "$.filename",
                        "filename 必须恰好出现一次且非空",
                    )
                )
                return
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    crontab_raw = read_crontab_bytes()
                    payload = get_infrast_schedule_payload(
                        CONFIG_DIR, pairs[0][1], crontab_raw
                    )
                self._json(200, payload)
            except Exception as error:
                self._schedule_error(error)
        elif p == "/api/infrast/operators":
            pairs = urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=False
            )
            if len(pairs) > 1 or (
                pairs and (pairs[0][0] != "filename" or not pairs[0][1])
            ):
                self._schedule_error(
                    OperatorBoxError(
                        "invalid_request",
                        "$.filename",
                        "filename 只能出现一次且不能为空",
                    )
                )
                return
            try:
                filename = pairs[0][1] if pairs else None
                self._json(
                    200,
                    get_operator_box_payload(
                        Path(CONFIG_DIR), filename, read_crontab_bytes()
                    ),
                )
            except Exception as error:
                self._schedule_error(error)
        elif p == "/api/cultivation":
            status, payload = cultivation_dispatch("payload")
            self._json(status, payload)
        elif p.startswith("/api/cultivation/avatar/"):
            op_id = p[len("/api/cultivation/avatar/"):].strip()
            if not CULTIVATION_AVATAR_ID_RE.fullmatch(op_id):
                self._json(400, {"error": "invalid operator id"})
                return
            webp_path = os.path.join(CULTIVATION_AVATAR_DIR, f"{op_id}.webp")
            if os.path.isfile(webp_path):
                try:
                    with open(webp_path, "rb") as fh:
                        body = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/webp")
                    self.send_header("Cache-Control", "public, max-age=604800")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                except OSError:
                    pass
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72">'
                '<rect width="72" height="72" rx="12" fill="#2a2d36"/>'
                '<text x="36" y="43" text-anchor="middle" fill="#a0a8b8" font-size="20" font-family="sans-serif">OP</text>'
                '</svg>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
        elif p == "/api/medicine-plan":
            try:
                with schedule_write_lock:
                    status, payload = medicine_dispatch("payload")
                self._json(status, payload)
            except Exception as error:
                self._json(500, medicine_error_payload(error, 500))
        elif p == "/api/stages":
            self._json(200, get_stage_catalog())
        elif p == "/api/status":
            with run_lock:
                d = {
                    "active": running_task["active"],
                    "task": running_task["task"],
                    "start_time": running_task["start_time"],
                    "output": running_task["output"][-8000:],
                    "startup_health": read_startup_health_state(),
                    "skip_next_daily": load_skip_next_daily(CONFIG_DIR),
                }
            self._json(200, d)
        elif p == "/api/startup-health":
            self._json(200, read_startup_health_state())
        elif p == "/api/logs":
            name, log = get_logs(150)
            core = get_maacore_log(80)
            self._json(200, {"name": name, "log": log, "core_log": core})
        elif p == "/api/crontab":
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    content = read_crontab_bytes().decode("utf-8", "replace")
                self._json(200, {"content": content})
            except Exception as error:
                self._schedule_error(error)
        elif p == "/api/files":
            self._json(200, {
                "profile": read_file(PROFILE_FILE),
                "cli": read_file(CLI_FILE)
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path

        if p == "/api/cultivation/settings":
            self._cultivation_post("settings")
            return

        if p == "/api/cultivation/goal":
            self._cultivation_post("goal")
            return

        if p == "/api/cultivation/goal-state":
            self._cultivation_post("goal-state")
            return

        if p == "/api/cultivation/balance":
            self._cultivation_post("balance")
            return

        if p == "/api/cultivation/mastery-settings":
            self._cultivation_post("mastery-settings")
            return

        if p == "/api/cultivation/mastery-plan":
            self._cultivation_post("mastery-plan")
            return

        if p == "/api/cultivation/mastery-plan-state":
            self._cultivation_post("mastery-plan-state")
            return

        if p == "/api/cultivation/mastery-run":
            self._cultivation_post(mode="mastery")
            return

        if p in ("/api/cultivation/scan", "/api/cultivation/run"):
            self._cultivation_post(mode="scan" if p.endswith("/scan") else "run")
            return

        if self.path == "/api/infrast/schedule/preview":
            try:
                body = self._schedule_body()
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    crontab_raw = read_crontab_bytes()
                    result = preview_schedule_change(
                        Path(CONFIG_DIR), body, crontab_raw
                    )
                self._json(200, result)
            except Exception as error:
                self._schedule_error(error)
            return

        if self.path == "/api/infrast/schedule/commit":
            try:
                body = self._schedule_body()
                fields = {
                    "request",
                    "expected_hashes",
                    "expected_target_hashes",
                    "transaction_id",
                    "generated_at",
                }
                if set(body) != fields:
                    raise ScheduleError(
                        "invalid_request", "$", "确认排班请求字段无效"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = commit_schedule_change(
                        Path(CONFIG_DIR),
                        body["request"],
                        body["expected_hashes"],
                        body["expected_target_hashes"],
                        body["transaction_id"],
                        body["generated_at"],
                        read_crontab_bytes,
                        install_crontab_bytes,
                    )
                self._json(200, {"ok": True, "result": result})
            except Exception as error:
                self._schedule_error(error)
            return

        if self.path == "/api/infrast/schedule/restore/preview":
            try:
                body = self._schedule_body()
                if set(body) != {"snapshot_id"}:
                    raise ScheduleError(
                        "invalid_request", "$", "恢复预览请求字段无效"
                    )
                if body["snapshot_id"] is not None and not isinstance(
                    body["snapshot_id"], str
                ):
                    raise ScheduleError(
                        "invalid_request", "$.snapshot_id", "snapshot_id 必须是字符串或 null"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = preview_schedule_restore(
                        Path(CONFIG_DIR),
                        body["snapshot_id"],
                        read_crontab_bytes(),
                    )
                self._json(200, result)
            except Exception as error:
                self._schedule_error(error)
            return

        if self.path == "/api/infrast/schedule/restore":
            try:
                body = self._schedule_body()
                fields = {
                    "snapshot_id",
                    "expected_hashes",
                    "expected_snapshot_sha",
                }
                if set(body) != fields:
                    raise ScheduleError(
                        "invalid_request", "$", "确认恢复请求字段无效"
                    )
                if not isinstance(body["snapshot_id"], str):
                    raise ScheduleError(
                        "invalid_request", "$.snapshot_id", "snapshot_id 必须是字符串"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = commit_schedule_restore(
                        Path(CONFIG_DIR),
                        body["snapshot_id"],
                        body["expected_hashes"],
                        body["expected_snapshot_sha"],
                        read_crontab_bytes,
                        install_crontab_bytes,
                    )
                self._json(200, {"ok": True, "result": result})
            except Exception as error:
                self._schedule_error(error)
            return

        if self.path == "/api/infrast/import/preview":
            try:
                body = self._import_body()
                if set(body) != {"filename", "content"}:
                    raise PlanImportError(
                        "invalid_request", "请求字段必须是 filename 和 content"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = preview_plan_import(
                        get_infrast_dir(CONFIG_DIR), body["filename"], body["content"]
                    )
                self._json(200, {"ok": True, "preview": result})
            except ScheduleError as error:
                self._schedule_error(error)
            except Exception as error:
                self._import_error(error)
            return

        if self.path == "/api/infrast/import":
            try:
                body = self._import_body()
                fields = {
                    "filename",
                    "content",
                    "preview_sha256",
                    "expected_current_sha256",
                    "replace",
                }
                if set(body) != fields:
                    raise PlanImportError(
                        "invalid_request", "确认导入请求字段无效"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = commit_plan_import(
                        get_infrast_dir(CONFIG_DIR),
                        body["filename"],
                        body["content"],
                        body["preview_sha256"],
                        body["expected_current_sha256"],
                        body["replace"],
                    )
                self._json(200, {"ok": True, "result": result})
            except ScheduleError as error:
                self._schedule_error(error)
            except Exception as error:
                self._import_error(error)
            return

        if self.path == "/api/infrast/operators/scan":
            try:
                body = self._schedule_body()
                if body:
                    raise OperatorBoxError(
                        "invalid_request", "$", "扫描请求不接受参数"
                    )
                ok = run_operator_scan_bg(CONFIG_DIR)
                self._json(
                    202 if ok else 409,
                    {
                        "ok": ok,
                        "error": "" if ok else "已有任务正在运行",
                    },
                )
            except Exception as error:
                self._schedule_error(error)
            return

        if self.path == "/api/infrast/operators/weekly/preview":
            try:
                body = self._schedule_body()
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = preview_weekly_schedule(
                        Path(CONFIG_DIR), body, read_crontab_bytes()
                    )
                result.pop("candidate_crontab", None)
                self._json(200, result)
            except Exception as error:
                self._schedule_error(error)
            return

        if self.path == "/api/infrast/operators/weekly":
            try:
                body = self._schedule_body()
                if set(body) != {"request", "expected_crontab_sha256"}:
                    raise OperatorBoxError(
                        "invalid_request", "$", "确认每周扫描请求字段无效"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    result = commit_weekly_schedule(
                        Path(CONFIG_DIR),
                        body["request"],
                        body["expected_crontab_sha256"],
                        read_crontab_bytes,
                        install_crontab_bytes,
                    )
                self._json(200, {"ok": True, "result": result})
            except Exception as error:
                self._schedule_error(error)
            return

        if p == "/api/medicine-plan/settings":
            try:
                body = self._schedule_body()
                with schedule_write_lock:
                    status, payload = medicine_dispatch("settings", body)
                self._json(status, payload)
            except ScheduleError as error:
                self._schedule_error(error)
            except Exception as error:
                self._json(500, medicine_error_payload(error, 500))
            return

        body = self._body()

        if p == "/api/config":
            tasks = body.get("tasks", [])
            if not tasks:
                self._json(400, {"ok": False, "error": "No tasks received"})
                return
            # Save all tasks, save_tasks handles disabled task separation using _enabled
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    ok = save_tasks(tasks, TASK_FILE)
            except Exception as error:
                self._schedule_error(error)
                return
            self._json(200, {"ok": ok})

        elif p == "/api/weekly-plan/settings":
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._json(400, {"ok": False, "error": "enabled must be a boolean"})
                return
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    settings = set_enabled(Path(CONFIG_DIR), enabled)
                self._json(200, {"ok": True, **settings})
            except ScheduleError as error:
                self._schedule_error(error)
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})

        elif p == "/api/weekly-plan/day":
            weekday = body.get("weekday", "")
            tasks = body.get("tasks")
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    save_weekday_tasks(Path(CONFIG_DIR), weekday, tasks)
                self._json(200, {"ok": True, "weekday": weekday})
            except ScheduleError as error:
                self._schedule_error(error)
            except (ValueError, OSError) as e:
                self._json(400, {"ok": False, "error": str(e)})

        elif p == "/api/weekly-plan/copy":
            source = body.get("source", "")
            targets = body.get("targets")
            try:
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    copied = copy_weekday_tasks(Path(CONFIG_DIR), source, targets)
                self._json(200, {"ok": True, "copied": copied})
            except ScheduleError as error:
                self._schedule_error(error)
            except (ValueError, OSError) as e:
                self._json(400, {"ok": False, "error": str(e)})

        elif p == "/api/run":
            action = body.get("action")
            if action == "skip-next-daily":
                try:
                    state = arm_skip_next_daily(CONFIG_DIR, read_crontab_bytes())
                    self._json(200, {"ok": True, "skip_next_daily": state})
                except Exception as error:
                    self._json(400, {"ok": False, "error": str(error)})
                return
            if action == "cancel-skip-next-daily":
                cancel_skip_next_daily(CONFIG_DIR)
                self._json(200, {
                    "ok": True,
                    "skip_next_daily": load_skip_next_daily(CONFIG_DIR),
                })
                return
            if action is not None:
                self._json(400, {"ok": False, "error": "Invalid action"})
                return
            task = body.get("task", "daily")
            weekday = body.get("weekday")
            infrast_now = body.get("infrast_now", False)
            if weekday is not None and weekday not in WEEKDAYS:
                self._json(400, {"ok": False, "error": "Invalid weekday"})
                return
            if not isinstance(infrast_now, bool) or (
                infrast_now and (task != "daily" or weekday is not None)
            ):
                self._json(400, {"ok": False, "error": "Invalid infrast_now"})
                return
            ok = run_task_bg(task, weekday, infrast_now=infrast_now)
            self._json(
                200,
                {
                    "ok": ok,
                    "weekday": weekday or "",
                    "infrast_now": infrast_now,
                },
            )

        elif p == "/api/update":
            ok = update_maa_bg()
            self._json(200, {"ok": ok})

        elif p == "/api/startup-health":
            ok = startup_health_bg()
            self._json(200, {"ok": ok})

        elif p == "/api/kill":
            global running_task
            with run_lock:
                pid = running_task["pid"]
                if pid:
                    try:
                        subprocess.run(["pkill", "-P", str(pid)], env=get_env())
                        os.kill(pid, 9)
                    except Exception:
                        pass
                subprocess.run(["pkill", "-f", "maa run"], env=get_env())
                subprocess.run(["pkill", "-f", "maa-run.sh"], env=get_env())
                
                # 显式恢复分辨率并强退游戏，防止 SIGKILL 导致 bash trap 无法触发而遗留横屏/游戏
                try:
                    subprocess.run(["nsenter", "-t", "1", "-m", "--", "/system/bin/sh", "-c", "wm size reset"], env=get_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["adb", "-s", "127.0.0.1:5555", "shell", "am", "force-stop", "com.hypergryph.arknights"], env=get_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

                running_task["active"] = False
                running_task["output"] += "\n\n--- 挂机任务被用户手动停止 ---"
            self._json(200, {"ok": True})

        elif p == "/api/game":
            action = body.get("action", "")
            cmd = []
            if action == "start":
                cmd = ["adb", "-s", "127.0.0.1:5555", "shell", "monkey", "-p", "com.hypergryph.arknights", "-c", "android.intent.category.LAUNCHER", "1"]
            elif action == "stop":
                cmd = ["adb", "-s", "127.0.0.1:5555", "shell", "am", "force-stop", "com.hypergryph.arknights"]
            
            if cmd:
                try:
                    subprocess.run(cmd, env=get_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(200, {"ok": False, "error": str(e)})
            else:
                self._json(400, {"ok": False, "error": "invalid action"})

        elif p == "/api/crontab":
            content = body.get("content", "")
            try:
                if not isinstance(content, str):
                    raise ScheduleError(
                        "invalid_request", "$.content", "content 必须是字符串"
                    )
                with schedule_write_lock:
                    recover_schedule_or_raise(CONFIG_DIR)
                    install_crontab_bytes(content.encode("utf-8"))
                self._json(200, {"ok": True})
            except Exception as error:
                self._schedule_error(error)

        elif p == "/api/files":
            which = body.get("file", "profile")
            content = body.get("content", "")
            target = PROFILE_FILE if which == "profile" else CLI_FILE
            ok = write_file(target, content)
            self._json(200, {"ok": ok})

        else:
            self._json(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    server = WEB_SERVER_CLASS(("0.0.0.0", PORT), Handler)
    print(f"MAA WebUI v2 启动: http://0.0.0.0:{PORT}")
    print(f"配置目录: {CONFIG_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
        server.shutdown()

if __name__ == "__main__":
    main()
