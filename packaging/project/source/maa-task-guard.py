#!/usr/bin/env python3
import argparse
import copy
import json
import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)


def write_task_toml(data, output_path):
    lines = [
        "# MAA 日常任务配置 (由 maa-task-guard 生成)",
        f"# 更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for array_name in ("tasks", "disabled_tasks"):
        for task in data.get(array_name, []) or []:
            lines.extend(format_task(task, array_name))
    Path(output_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def format_task(task, array_name):
    lines = [f"[[{array_name}]]"]
    if "_original_index" in task:
        lines.append(f"_original_index = {toml_value(task['_original_index'])}")
    if task.get("name"):
        lines.append(f"name = {toml_value(task['name'])}")
    lines.append(f"type = {toml_value(task.get('type', ''))}")
    if task.get("strategy"):
        lines.append(f"strategy = {toml_value(task['strategy'])}")
    for key, value in task.items():
        if key.startswith("_") or key in {"name", "type", "strategy", "params", "variants"}:
            continue
        lines.append(f"{key} = {toml_value(value)}")

    params = task.get("params") or {}
    variants = task.get("variants") or []
    if params and not variants and is_small_inline_params(params):
        lines.append(f"params = {toml_value(params)}")
    elif params:
        lines.append("")
        lines.append(f"[{array_name}.params]")
        for key, value in params.items():
            lines.append(f"{key} = {toml_value(value)}")

    for variant in variants:
        lines.append("")
        lines.append(f"[[{array_name}.variants]]")
        condition = variant.get("condition")
        if condition:
            lines.append(f"condition = {toml_value(condition)}")
        params = variant.get("params") or {}
        if params:
            lines.append(f"params = {toml_value(params)}")
    lines.append("")
    return lines


def is_small_inline_params(params):
    if len(params) > 3:
        return False
    for value in params.values():
        if isinstance(value, dict):
            return False
        if isinstance(value, list) and len(value) > 5:
            return False
    return True


def load_toml(path):
    return tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_activity(path):
    if not path or not Path(path).is_file():
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def cli_datetime(value):
    return str(value).replace("/", "-").replace(" ", "T")


def stage_value(stage):
    return (
        stage.get("Value")
        or stage.get("value")
        or stage.get("Display")
        or stage.get("display")
        or ""
    )


def stage_windows(activity_data, client="Official"):
    if not isinstance(activity_data, dict):
        return {}
    official = activity_data.get(client) or {}
    side_story = official.get("sideStoryStage") or official.get("SideStoryStage") or {}
    windows = {}
    for info in side_story.values():
        activity = info.get("Activity") or info.get("activity") or {}
        start = activity.get("UtcStartTime") or activity.get("utcStartTime")
        end = activity.get("UtcExpireTime") or activity.get("utcExpireTime")
        if not start or not end:
            continue
        timezone_value = activity.get("TimeZone", activity.get("timeZone", 8))
        try:
            timezone_value = int(timezone_value)
        except Exception:
            timezone_value = 8
        condition = {
            "type": "DateTime",
            "start": cli_datetime(start),
            "end": cli_datetime(end),
            "timezone": timezone_value,
        }
        for stage in info.get("Stages") or info.get("stages") or []:
            value = stage_value(stage)
            if value:
                windows[value] = copy.deepcopy(condition)
    return windows


def is_on_side_story(condition):
    return isinstance(condition, dict) and condition.get("type") == "OnSideStory"


def normalize_activity_stage_variants(data, activity_data):
    windows = stage_windows(activity_data)
    if not windows:
        return copy.deepcopy(data), ["activity_guard=no_activity_windows"]

    normalized = copy.deepcopy(data)
    notes = []
    for array_name in ("tasks", "disabled_tasks"):
        for task in normalized.get(array_name, []) or []:
            if task.get("type") != "Fight":
                continue
            variants = task.get("variants") or []
            if not variants:
                continue

            next_variants = []
            for variant in variants:
                params = variant.get("params") or {}
                stage = params.get("stage")
                condition = variant.get("condition")
                if is_on_side_story(condition) and stage:
                    specific_condition = windows.get(stage)
                    if specific_condition:
                        updated = copy.deepcopy(variant)
                        updated["condition"] = specific_condition
                        next_variants.append(updated)
                        notes.append(f"narrowed_stage={stage}")
                    else:
                        notes.append(f"removed_unmatched_on_side_story_stage={stage}")
                    continue
                next_variants.append(variant)

            if next_variants:
                task["variants"] = next_variants
                if task.get("strategy") is None:
                    task["strategy"] = "merge"
            else:
                task.pop("variants", None)
                task.pop("strategy", None)
    return normalized, notes


def parse_cli_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def now_for_condition(now, timezone_value):
    try:
        offset = int(timezone_value)
    except Exception:
        offset = 8
    tz = timezone(timedelta(hours=offset))
    if now.tzinfo is None:
        return now
    return now.astimezone(tz).replace(tzinfo=None)


def condition_active(condition, now, activity_data):
    if not condition:
        return True
    ctype = condition.get("type") if isinstance(condition, dict) else None
    if ctype == "DateTime":
        current = now_for_condition(now, condition.get("timezone", 8))
        start = parse_cli_datetime(condition.get("start"))
        end = parse_cli_datetime(condition.get("end"))
        if start and start.tzinfo is not None:
            start = start.astimezone(timezone.utc).replace(tzinfo=None)
        if end and end.tzinfo is not None:
            end = end.astimezone(timezone.utc).replace(tzinfo=None)
        if start and current < start:
            return False
        if end and current >= end:
            return False
        return True
    if ctype == "Weekday":
        weekdays = condition.get("weekdays") or []
        current = now_for_condition(now, condition.get("timezone", 8))
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return names[current.weekday()] in weekdays
    if ctype == "OnSideStory":
        current = now_for_condition(now, 8)
        for cond in stage_windows(activity_data).values():
            if condition_active(cond, current.replace(tzinfo=timezone(timedelta(hours=8))), activity_data):
                return True
        return False
    if ctype == "And":
        return all(condition_active(c, now, activity_data) for c in condition.get("conditions") or [])
    if ctype == "Or":
        return any(condition_active(c, now, activity_data) for c in condition.get("conditions") or [])
    if ctype == "Not":
        return not condition_active(condition.get("condition") or {}, now, activity_data)
    return False


def effective_stage(data, activity_data, now):
    for task in data.get("tasks", []) or []:
        if task.get("type") != "Fight":
            continue
        params = copy.deepcopy(task.get("params") or {})
        variants = task.get("variants") or []
        if variants:
            matched = False
            for variant in variants:
                if condition_active(variant.get("condition"), now, activity_data):
                    params.update(copy.deepcopy(variant.get("params") or {}))
                    matched = True
                    if task.get("strategy", "first") != "merge":
                        break
            if not matched:
                params = {}
        return params.get("stage", "")
    return ""


def main():
    default_cache_dir = os.environ.get("MAA_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "maa"
    )
    default_activity_file = os.path.join(default_cache_dir, "StageActivityV2.json")

    parser = argparse.ArgumentParser()
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--activity-file", default=default_activity_file)
    parser.add_argument("--output")
    parser.add_argument("--now")
    parser.add_argument("--dry-run-stage", action="store_true")
    args = parser.parse_args()

    data = load_toml(args.task_file)
    activity_data = load_activity(args.activity_file)
    normalized, notes = normalize_activity_stage_variants(data, activity_data)

    if args.dry_run_stage:
        now = parse_cli_datetime(args.now) if args.now else datetime.now(timezone(timedelta(hours=8)))
        print(f"effective_stage={effective_stage(normalized, activity_data, now)}")
        return 0

    output = args.output or args.task_file
    write_task_toml(normalized, output)
    for note in notes:
        print(f"activity_guard: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
