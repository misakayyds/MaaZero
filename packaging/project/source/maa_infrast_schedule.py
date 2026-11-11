#!/usr/bin/env python3
"""Analyze validated MAA custom-infrastructure plan schedules locally."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import tomllib
import uuid
from pathlib import Path

from maa_infrast_plan import parse_plan_document, read_plan_bytes
from maa_task_config import load_tasks, render_tasks, validate_task_bytes
from maa_weekly_plan import load_settings


DURATION_RE = re.compile(
    r"(?<![\d+-])(\d{1,2})\s*(?:H|小时)(?![A-Za-z0-9])", re.IGNORECASE
)
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_CRON = {day: (index + 1) % 7 for index, day in enumerate(WEEKDAYS)}
MANAGED_BEGIN = b"# BEGIN MAA_WEBUI_SCHEDULE"
MANAGED_END = b"# END MAA_WEBUI_SCHEDULE"
_CRON_FIELD = rb"[^ \t;&|`$()<>]+"
_DAILY_ARGUMENT = (
    rb"(?:--weekday[ \t]+(?:mon|tue|wed|thu|fri|sat|sun)"
    rb"|--scheduled-time[ \t]+\d{2}:\d{2}"
    rb"|--lock-wait-seconds[ \t]+\d{1,4})"
)
DAILY_COMMAND_RE = re.compile(
    rb"^[ \t]*(?:" + _CRON_FIELD + rb"[ \t]+){5}"
    rb"/root/maa-scripts/maa-run\.sh[ \t]+daily"
    rb"(?:[ \t]+" + _DAILY_ARGUMENT + rb")*"
    rb"(?:[ \t]+(?:>|>>)[ \t]*/var/log/maa/cron\.log(?:[ \t]+2>&1)?)?"
    rb"[ \t]*$"
)
ANY_DAILY_COMMAND_RE = re.compile(
    rb"/root/maa-scripts/maa-run\.sh[ \t]+daily(?=$|[^A-Za-z0-9_]|[0-9]+[<>])"
)
DEFAULT_INFRAST_PARAMS = {
    "mode": 0,
    "facility": ["Trade", "Reception", "Mfg", "Control", "Power", "Office", "Dorm"],
    "drones": "_NotUse",
    "threshold": 0.3,
    "dorm_trust_enabled": False,
    "dorm_notstationed_enabled": True,
    "replenish": True,
    "reception_message_board": True,
    "reception_clue_exchange": True,
    "reception_send_clue": True,
    "continue_training": True,
}


@dataclass(frozen=True)
class ScheduleError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return self.message


def _schedule_error(code: str, path: str, message: str) -> None:
    raise ScheduleError(code, path, message)


def _time_to_minute(value: object, path: str) -> int:
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        _schedule_error("invalid_time", path, "时间必须是 HH:MM")
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError:
        _schedule_error("invalid_time", path, "时间必须是有效的 HH:MM")
    return parsed.hour * 60 + parsed.minute


def _period_time_to_minute(value: object, path: str) -> int:
    try:
        return _time_to_minute(value, path)
    except ScheduleError as exc:
        raise ScheduleError("invalid_period", exc.path, "period 时间必须是 HH:MM") from exc


def _duration_from_plan(plan: dict[str, object], path: str) -> int:
    values = {
        int(match)
        for field in ("name", "description")
        for match in DURATION_RE.findall(str(plan.get(field, "")))
    }
    if len(values) != 1:
        _schedule_error("ambiguous_duration", path, "无法唯一推测班次时长")
    value = values.pop()
    if value <= 0:
        _schedule_error("invalid_duration", path, "班次时长必须大于 0")
    return value


def _slot_name(plan: dict[str, object], index: int) -> str:
    return plan["name"] if "name" in plan else f"计划 {index + 1}"


def _format_time(minute: int) -> str:
    hour, minute = divmod(minute % MINUTES_PER_DAY, 60)
    return f"{hour:02d}:{minute:02d}"


def _date_value(value: object, path: str) -> date:
    if not isinstance(value, str):
        _schedule_error("invalid_date", path, "日期必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _schedule_error("invalid_date", path, "日期必须是有效的 YYYY-MM-DD")
    if parsed.isoformat() != value:
        _schedule_error("invalid_date", path, "日期必须是 YYYY-MM-DD")
    return parsed


def build_cycle_definition(
    durations_hours: object,
    names: object,
    anchor_date: str,
    anchor_time: str,
) -> dict[str, object]:
    """Build the immutable wall-clock definition for an inferred plan cycle."""
    if (
        not isinstance(durations_hours, list)
        or not durations_hours
        or any(
            type(value) is not int or value <= 0
            for value in durations_hours
        )
    ):
        _schedule_error("invalid_cycle", "$.durations_hours", "班次时长必须是正整数小时")
    if (
        not isinstance(names, list)
        or len(names) != len(durations_hours)
        or any(not isinstance(value, str) or not value for value in names)
    ):
        _schedule_error("invalid_cycle", "$.names", "班次名称与时长必须一一对应")
    anchor_day = _date_value(anchor_date, "$.anchor_date")
    anchor_minute = _time_to_minute(anchor_time, "$.anchor_time")
    cycle_minutes = sum(durations_hours) * 60
    repeat_minutes = math.lcm(cycle_minutes, MINUTES_PER_DAY)
    trigger_times = sorted(
        {
            _format_time(anchor_minute + cycle_start + boundary)
            for cycle_start in range(0, repeat_minutes, cycle_minutes)
            for boundary in (
                sum(durations_hours[:index]) * 60
                for index in range(len(durations_hours))
            )
        }
    )
    return {
        "anchor_date": anchor_day.isoformat(),
        "anchor_time": anchor_time,
        "durations_hours": list(durations_hours),
        "names": list(names),
        "cycle_hours": cycle_minutes // 60,
        "day_mod_divisor": repeat_minutes // MINUTES_PER_DAY,
        "trigger_times": trigger_times,
    }


def resolve_cycle_slot(
    cycle: object,
    logical_date: str,
    current_time: str,
    *,
    boundary_only: bool,
) -> dict[str, object] | None:
    """Resolve the active plan, or the exact boundary plan, from wall-clock time."""
    if not isinstance(cycle, dict):
        _schedule_error("invalid_cycle", "$.cycle", "跨日循环定义无效")
    expected = {
        "anchor_date",
        "anchor_time",
        "durations_hours",
        "names",
        "cycle_hours",
        "day_mod_divisor",
        "trigger_times",
    }
    if set(cycle) != expected:
        _schedule_error("invalid_cycle", "$.cycle", "跨日循环字段无效")
    rebuilt = build_cycle_definition(
        cycle["durations_hours"],
        cycle["names"],
        cycle["anchor_date"],
        cycle["anchor_time"],
    )
    if rebuilt != cycle:
        _schedule_error("invalid_cycle", "$.cycle", "跨日循环派生字段不一致")
    current_day = _date_value(logical_date, "$.logical_date")
    current_minute = _time_to_minute(current_time, "$.current_time")
    anchor_minute = _time_to_minute(cycle["anchor_time"], "$.anchor_time")
    anchor = datetime.combine(
        _date_value(cycle["anchor_date"], "$.anchor_date"),
        datetime.min.time(),
    ) + timedelta(minutes=anchor_minute)
    current = datetime.combine(current_day, datetime.min.time()) + timedelta(
        minutes=current_minute
    )
    cycle_minutes = cycle["cycle_hours"] * 60
    cycle_number, remainder = divmod(
        int((current - anchor).total_seconds() // 60),
        cycle_minutes,
    )
    elapsed = 0
    plan_index = 0
    for index, duration in enumerate(cycle["durations_hours"]):
        next_elapsed = elapsed + duration * 60
        if elapsed <= remainder < next_elapsed:
            plan_index = index
            break
        elapsed = next_elapsed
    shift_start = anchor + timedelta(
        minutes=cycle_number * cycle_minutes + elapsed
    )
    if boundary_only and current != shift_start:
        return None
    return {
        "time": shift_start.strftime("%H:%M"),
        "logical_date": shift_start.date().isoformat(),
        "plan_index": plan_index,
        "name": cycle["names"][plan_index],
        "source_time": cycle["anchor_time"],
        "source": "cycle",
    }


def _slots_from_period(document: dict[str, object]) -> dict[str, object]:
    plans = document["plans"]
    coverage = [-1] * MINUTES_PER_DAY

    for plan_index, plan in enumerate(plans):
        plan_path = f"$.plans[{plan_index}]"
        period_path = f"{plan_path}.period"
        if "period" not in plan or not isinstance(plan["period"], list) or not plan["period"]:
            _schedule_error("invalid_period", period_path, "period 必须是非空时间区间数组")

        for range_index, value in enumerate(plan["period"]):
            range_path = f"{period_path}[{range_index}]"
            if not isinstance(value, list) or len(value) != 2:
                _schedule_error("invalid_period", range_path, "period 区间必须包含起止两个时间")
            start = _period_time_to_minute(value[0], f"{range_path}[0]")
            end = _period_time_to_minute(value[1], f"{range_path}[1]")
            if start > end:
                _schedule_error("invalid_period", range_path, "period 跨午夜必须拆分为两个区间")
            for minute in range(start, end + 1):
                if coverage[minute] != -1:
                    _schedule_error("period_overlap", range_path, "period 区间存在重叠或重复转换")
                coverage[minute] = plan_index

    if -1 in coverage:
        _schedule_error("period_gap", "$.plans", "period 未覆盖完整 1440 分钟循环")

    transitions = [
        minute
        for minute in range(MINUTES_PER_DAY)
        if coverage[minute] != coverage[(minute - 1) % MINUTES_PER_DAY]
    ]
    if not transitions:
        transitions = [0]

    slots = []
    durations_hours = []
    for index, start in enumerate(transitions):
        end = transitions[(index + 1) % len(transitions)]
        duration_minutes = (
            MINUTES_PER_DAY
            if len(transitions) == 1
            else (end - start) % MINUTES_PER_DAY
        )
        plan_index = coverage[start]
        slots.append(
            {
                "time": _format_time(start),
                "plan_index": plan_index,
                "name": _slot_name(plans[plan_index], plan_index),
                "source": "period",
            }
        )
        duration = duration_minutes / 60
        durations_hours.append(int(duration) if duration.is_integer() else duration)

    return {
        "source": "period",
        "requires_confirmation": False,
        "durations_hours": durations_hours,
        "cycle_hours": 24,
        "cross_day_cycle": False,
        "day_mod_divisor": 1,
        "trigger_times": [slot["time"] for slot in slots],
        "slots": slots,
        "warnings": [],
    }


def _slots_from_durations(
    document: dict[str, object], anchor_time: str
) -> dict[str, object]:
    anchor = _time_to_minute(anchor_time, "$.anchor_time")
    plans = document["plans"]
    durations = [
        _duration_from_plan(plan, f"$.plans[{index}]")
        for index, plan in enumerate(plans)
    ]
    cycle_hours = sum(durations)
    if cycle_hours < 24:
        _schedule_error("invalid_cycle", "$.plans", "班次循环时长总和不能小于 24 小时")

    slots = []
    minute = anchor
    for plan_index, (plan, duration) in enumerate(zip(plans, durations)):
        slots.append(
            {
                "time": _format_time(minute),
                "day_offset": minute // MINUTES_PER_DAY,
                "plan_index": plan_index,
                "name": _slot_name(plan, plan_index),
                "source": "inferred",
            }
        )
        minute += duration * 60

    cycle_minutes = cycle_hours * 60
    repeat_minutes = math.lcm(cycle_minutes, MINUTES_PER_DAY)
    trigger_times = sorted(
        {
            _format_time(anchor + cycle_start + boundary)
            for cycle_start in range(0, repeat_minutes, cycle_minutes)
            for boundary in (
                sum(durations[:index]) * 60 for index in range(len(durations))
            )
        }
    )

    return {
        "source": "inferred",
        "requires_confirmation": True,
        "durations_hours": durations,
        "cycle_hours": cycle_hours,
        "cross_day_cycle": cycle_hours > 24,
        "day_mod_divisor": repeat_minutes // MINUTES_PER_DAY,
        "trigger_times": trigger_times,
        "slots": slots,
        "warnings": [
            "班次时间由名称或描述推测，保存前需要确认",
            *(
                ["班次循环超过 24 小时，将按锚点日期时间跨日循环"]
                if cycle_hours > 24
                else []
            ),
        ],
    }


def analyze_plan_document(
    document: dict[str, object], anchor_time: str = "02:00"
) -> dict[str, object]:
    """Return automatic slots from one raw, already validated plan document."""
    plans = document.get("plans") if isinstance(document, dict) else None
    if not isinstance(plans, list) or not plans:
        _schedule_error("invalid_document", "$.plans", "必须提供已验证的计划数组")
    if any(not isinstance(plan, dict) for plan in plans):
        _schedule_error("invalid_document", "$.plans", "计划必须是对象")

    if any("period" in plan for plan in plans):
        return _slots_from_period(document)
    return _slots_from_durations(document, anchor_time)


def analyze_plan_file(
    infrast_dir: Path, filename: str, anchor_time: str = "02:00"
) -> dict[str, object]:
    """Read a plan once, validate it, and attach its immutable byte identity."""
    raw = read_plan_bytes(Path(infrast_dir), filename)
    document = parse_plan_document(raw, filename)
    result = analyze_plan_document(document, anchor_time)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "filename": filename,
        **result,
    }


def _normalized_slots(slots: object, path: str = "$.slots") -> list[dict[str, object]]:
    if not isinstance(slots, list) or not slots:
        _schedule_error("invalid_slots", path, "至少需要一个班次")
    normalized = []
    seen_times = set()
    for index, slot in enumerate(slots):
        slot_path = f"{path}[{index}]"
        if not isinstance(slot, dict):
            _schedule_error("invalid_slots", slot_path, "班次必须是对象")
        time = slot.get("time")
        minute = _time_to_minute(time, f"{slot_path}.time")
        if minute in seen_times:
            _schedule_error("duplicate_slot", f"{slot_path}.time", "同一天不能有重复班次时间")
        plan_index = slot.get("plan_index")
        if not isinstance(plan_index, int) or isinstance(plan_index, bool) or plan_index < 0:
            _schedule_error("invalid_slots", f"{slot_path}.plan_index", "plan_index 必须是非负整数")
        seen_times.add(minute)
        normalized.append({**slot, "time": time, "plan_index": plan_index, "_minute": minute})
    return sorted(normalized, key=lambda slot: slot["_minute"])


def _managed_variants(slots: object) -> list[dict[str, object]]:
    normalized = _normalized_slots(slots)
    if len(normalized) == 1:
        return [{"condition": {"type": "Time"}, "params": {"plan_index": normalized[0]["plan_index"]}}]
    variants = []
    for index, slot in enumerate(normalized):
        next_slot = normalized[(index + 1) % len(normalized)]
        variants.append(
            {
                "condition": {
                    "type": "Time",
                    "start": f"{slot['time']}:00",
                    "end": f"{next_slot['time']}:00",
                },
                "params": {"plan_index": slot["plan_index"]},
            }
        )
    return variants


def build_cycle_variants(cycle: object) -> list[dict[str, object]]:
    rebuilt = build_cycle_definition(
        cycle.get("durations_hours") if isinstance(cycle, dict) else None,
        cycle.get("names") if isinstance(cycle, dict) else None,
        cycle.get("anchor_date") if isinstance(cycle, dict) else None,
        cycle.get("anchor_time") if isinstance(cycle, dict) else None,
    )
    if rebuilt != cycle:
        _schedule_error("invalid_cycle", "$.cycle", "跨日循环派生字段不一致")

    anchor = datetime.combine(
        _date_value(cycle["anchor_date"], "$.cycle.anchor_date"),
        datetime.min.time(),
    ) + timedelta(
        minutes=_time_to_minute(cycle["anchor_time"], "$.cycle.anchor_time")
    )
    cycle_minutes = cycle["cycle_hours"] * 60
    repeat_minutes = cycle["day_mod_divisor"] * MINUTES_PER_DAY
    segments: list[list[tuple[datetime, dict[str, object]]]] = [
        [] for _ in cycle["durations_hours"]
    ]
    boundaries = [
        sum(cycle["durations_hours"][:index]) * 60
        for index in range(len(cycle["durations_hours"]))
    ]
    for cycle_start in range(0, repeat_minutes, cycle_minutes):
        for plan_index, (boundary, duration) in enumerate(
            zip(boundaries, cycle["durations_hours"])
        ):
            start = anchor + timedelta(minutes=cycle_start + boundary)
            end = start + timedelta(hours=duration)
            cursor = start
            while cursor < end:
                midnight = datetime.combine(
                    cursor.date() + timedelta(days=1), datetime.min.time()
                )
                segment_end = min(end, midnight)
                day_mod = {
                    "type": "DayMod",
                    "divisor": cycle["day_mod_divisor"],
                    "remainder": cursor.date().toordinal()
                    % cycle["day_mod_divisor"],
                }
                time_condition: dict[str, object] = {"type": "Time"}
                if cursor.time() != datetime.min.time():
                    time_condition["start"] = cursor.strftime("%H:%M:00")
                if segment_end != midnight:
                    time_condition["end"] = segment_end.strftime("%H:%M:00")
                condition = (
                    day_mod
                    if set(time_condition) == {"type"}
                    else {
                        "type": "And",
                        "conditions": [day_mod, time_condition],
                    }
                )
                segments[plan_index].append((cursor, condition))
                cursor = segment_end

    variants = []
    for plan_index, plan_segments in enumerate(segments):
        conditions = [
            condition
            for _start, condition in sorted(plan_segments, key=lambda item: item[0])
        ]
        condition = (
            conditions[0]
            if len(conditions) == 1
            else {"type": "Or", "conditions": conditions}
        )
        variants.append(
            {
                "condition": condition,
                "params": {"plan_index": plan_index},
            }
        )
    return variants


def compile_weekday_tasks(
    tasks: list[dict[str, object]],
    filename: str,
    slots: list[dict[str, object]],
    previous_slots: list[dict[str, object]] | None = None,
    *,
    cycle: dict[str, object] | None = None,
    previous_cycle: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Return one weekday task list with exactly one managed active Infrast."""
    if not isinstance(tasks, list):
        _schedule_error("invalid_tasks", "$.tasks", "任务必须是数组")
    if not isinstance(filename, str) or not filename:
        _schedule_error("invalid_filename", "$.filename", "文件名不能为空")

    compiled = copy.deepcopy(tasks)
    variants = (
        build_cycle_variants(cycle)
        if cycle is not None
        else _managed_variants(slots)
    )
    active_indexes = [
        index
        for index, task in enumerate(compiled)
        if isinstance(task, dict)
        and task.get("type") == "Infrast"
        and task.get("_enabled") is not False
    ]
    if len(active_indexes) > 1:
        _schedule_error("ambiguous_infrast", "$.tasks", "只能有一个启用的 Infrast 任务")

    if active_indexes:
        infrast = compiled.pop(active_indexes[0])
        strategy = infrast.get("strategy")
        if strategy not in (None, "first"):
            _schedule_error("unsupported_infrast_strategy", "$.tasks", "现有 Infrast strategy 不受支持")
        current_variants = infrast.get("variants")
        if previous_cycle is not None:
            if current_variants != build_cycle_variants(previous_cycle):
                _schedule_error("unsupported_infrast_variants", "$.tasks", "现有 Infrast variants 不是受管排班")
        elif previous_slots is not None:
            if current_variants != _managed_variants(previous_slots):
                _schedule_error("unsupported_infrast_variants", "$.tasks", "现有 Infrast variants 不是受管排班")
        else:
            if current_variants is not None and not isinstance(current_variants, list):
                _schedule_error("unsupported_infrast_variants", "$.tasks", "现有 Infrast variants 不受支持")
            if current_variants:
                _schedule_error("unsupported_infrast_variants", "$.tasks", "现有 Infrast variants 不是受管排班")
        raw_params = infrast.get("params")
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
    else:
        infrast = {"name": "基建换班", "type": "Infrast"}
        params = copy.deepcopy(DEFAULT_INFRAST_PARAMS)

    params["mode"] = 10000
    params["filename"] = filename
    params.pop("plan_index", None)
    infrast["params"] = params
    infrast["strategy"] = "first"
    infrast["variants"] = variants
    infrast["_enabled"] = True

    startup_indexes = [
        index
        for index, task in enumerate(compiled)
        if isinstance(task, dict)
        and task.get("type") == "StartUp"
        and task.get("_enabled") is not False
    ]
    if startup_indexes:
        insert_at = startup_indexes[-1] + 1
    else:
        insert_at = next(
            (
                index
                for index, task in enumerate(compiled)
                if isinstance(task, dict)
                and task.get("_enabled") is not False
                and task.get("type") != "StartUp"
            ),
            len(compiled),
        )
    compiled.insert(insert_at, infrast)
    return compiled


def _split_raw_lines(raw: bytes) -> tuple[list[bytes], bytes]:
    lines = raw.splitlines(keepends=True)
    for line in lines:
        if line.endswith(b"\r\n"):
            return lines, b"\r\n"
        if line.endswith(b"\n"):
            return lines, b"\n"
        if line.endswith(b"\r"):
            return lines, b"\r"
    return lines, b"\n"


def _logical_line(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith((b"\n", b"\r")):
        return line[:-1]
    return line


def parse_managed_crontab(raw: bytes) -> dict[str, object]:
    """Parse marker ownership without normalizing any crontab bytes."""
    if not isinstance(raw, bytes):
        _schedule_error("invalid_crontab", "$.crontab", "crontab 必须是字节串")
    lines, eol = _split_raw_lines(raw)
    logical = [_logical_line(line) for line in lines]
    begins = [index for index, line in enumerate(logical) if line == MANAGED_BEGIN]
    ends = [index for index, line in enumerate(logical) if line == MANAGED_END]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
            _schedule_error("invalid_cron_markers", "$.crontab", "受管 cron 标记必须唯一、完整且顺序正确")
        begin_index, end_index = begins[0], ends[0]
    else:
        begin_index = end_index = None

    outside = range(len(lines)) if begin_index is None else list(range(begin_index)) + list(range(end_index + 1, len(lines)))
    candidates = []
    unsafe_daily_lines = []
    for index in outside:
        line = logical[index]
        if line.lstrip().startswith(b"#"):
            continue
        if ANY_DAILY_COMMAND_RE.search(line) and not DAILY_COMMAND_RE.search(line):
            unsafe_daily_lines.append({"index": index, "line": line})
            continue
        if not DAILY_COMMAND_RE.search(line):
            continue
        digest = hashlib.sha256(line).hexdigest()
        candidates.append({"index": index, "line": line, "sha256": digest, "line_hash": digest})
    return {
        "has_managed_block": begin_index is not None,
        "managed_begin_index": begin_index,
        "managed_end_index": end_index,
        "managed_block": b"" if begin_index is None else b"".join(lines[begin_index + 1 : end_index]),
        "unmanaged_daily_lines": candidates,
        "unsafe_daily_lines": unsafe_daily_lines,
        "adoption_candidates": candidates,
        "eol": eol,
    }


def _canonical_cron_line(entry: dict[str, object]) -> bytes:
    weekday = entry.get("weekday", entry.get("day"))
    time = entry.get("time")
    if weekday not in WEEKDAY_CRON:
        _schedule_error("invalid_weekday", "$.entries", "weekday 必须是 mon 至 sun")
    minute_of_day = _time_to_minute(time, "$.entries.time")
    lock_wait = entry.get("lock_wait_seconds")
    if not isinstance(lock_wait, int) or isinstance(lock_wait, bool) or not 0 <= lock_wait <= 1800:
        _schedule_error("invalid_lock_wait", "$.entries.lock_wait_seconds", "锁等待时间必须在 0 到 1800 秒")
    hour, minute = divmod(minute_of_day, 60)
    return (
        f"{minute} {hour} * * {WEEKDAY_CRON[weekday]} /root/maa-scripts/maa-run.sh daily "
        f"--weekday {weekday} --scheduled-time {time} --lock-wait-seconds {lock_wait} "
        ">> /var/log/maa/cron.log 2>&1"
    ).encode("ascii")


def build_cron_entries(days: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    """Expand weekday slots into canonical cron entries using one seven-day ring."""
    if not isinstance(days, dict) or any(day not in WEEKDAY_CRON for day in days):
        _schedule_error("invalid_weekday", "$.days", "只能配置 mon 至 sun")
    expanded = []
    for day_index, day in enumerate(WEEKDAYS):
        slots = days.get(day, [])
        if not isinstance(slots, list):
            _schedule_error("invalid_slots", f"$.days.{day}", "班次必须是数组")
        try:
            normalized = _normalized_slots(slots, f"$.days.{day}") if slots else []
        except ScheduleError as exc:
            if exc.code == "duplicate_slot":
                raise ScheduleError("duplicate_cron_slot", exc.path, "同一星期不能有重复 cron 时间") from exc
            raise
        for slot in normalized:
            expanded.append({**{key: value for key, value in slot.items() if key != "_minute"}, "weekday": day, "_absolute_minute": day_index * MINUTES_PER_DAY + slot["_minute"]})
    if not expanded:
        _schedule_error("invalid_slots", "$.days", "至少需要一个 cron 班次")
    expanded.sort(key=lambda entry: entry["_absolute_minute"])
    for index, entry in enumerate(expanded):
        next_entry = expanded[(index + 1) % len(expanded)]
        interval = (next_entry["_absolute_minute"] - entry["_absolute_minute"]) % MINUTES_PER_WEEK
        if interval == 0:
            interval = MINUTES_PER_WEEK
        entry["lock_wait_seconds"] = min(1800, max(0, interval * 60 - 1))
    for entry in expanded:
        entry["day"] = entry["weekday"]
        entry["cron_line"] = _canonical_cron_line(entry).decode("ascii")
        entry["line"] = entry["cron_line"]
        entry.pop("_absolute_minute")
    return expanded


def build_cycle_cron_entries(cycle: dict[str, object]) -> list[dict[str, object]]:
    """Trigger every candidate clock time; runtime DayMod validation skips non-boundaries."""
    return build_cron_entries(
        {
            day: [
                {
                    "time": time,
                    "plan_index": index,
                    "name": "跨日循环候选触发",
                    "source_time": time,
                    "source": "cycle",
                }
                for index, time in enumerate(cycle["trigger_times"])
            ]
            for day in WEEKDAYS
        }
    )


def render_managed_crontab(
    raw: bytes, entries: list[dict[str, object]], adopted_line_hashes: set[str]
) -> bytes:
    """Replace or append the owned block while preserving all other raw bytes."""
    parsed = parse_managed_crontab(raw)
    if not isinstance(adopted_line_hashes, set) or not all(isinstance(value, str) for value in adopted_line_hashes):
        _schedule_error("invalid_adoption", "$.adopted_line_hashes", "adoption 必须是 SHA-256 集合")
    candidates = parsed["unmanaged_daily_lines"]
    candidate_hashes = {candidate["sha256"] for candidate in candidates}
    if not adopted_line_hashes.issubset(candidate_hashes):
        _schedule_error("invalid_adoption", "$.adopted_line_hashes", "adoption 包含不存在的 daily 行")
    if candidate_hashes - adopted_line_hashes:
        _schedule_error("unresolved_daily_entries", "$.crontab", "存在未确认纳管的 maa-run.sh daily 行")
    if parsed["unsafe_daily_lines"]:
        _schedule_error("unresolved_daily_entries", "$.crontab", "存在无法安全纳管的 maa-run.sh daily 行")
    if not isinstance(entries, list):
        _schedule_error("invalid_entries", "$.entries", "cron entries 必须是数组")

    lines, eol = _split_raw_lines(raw)
    adopted_indexes = {candidate["index"] for candidate in candidates if candidate["sha256"] in adopted_line_hashes}
    generated = [MANAGED_BEGIN, *[_canonical_cron_line(entry) for entry in entries], MANAGED_END]
    block = b"".join(line + eol for line in generated)
    begin_index = parsed["managed_begin_index"]
    end_index = parsed["managed_end_index"]
    if begin_index is not None:
        prefix = b"".join(line for index, line in enumerate(lines[:begin_index]) if index not in adopted_indexes)
        suffix = b"".join(line for index, line in enumerate(lines[end_index + 1 :], start=end_index + 1) if index not in adopted_indexes)
        return prefix + block + suffix

    prefix = b"".join(line for index, line in enumerate(lines) if index not in adopted_indexes)
    separator = b"" if not prefix or prefix.endswith((b"\n", b"\r")) else eol
    return prefix + separator + block


SCHEDULE_MANIFEST = "infrast-schedule.json"
BACKUP_DIR_PARTS = ("backups", "infrast-schedule")
LATEST_ACTIVATION = "latest-activation.json"
SOURCE_HASH_KEYS = (
    "json",
    "schedule_manifest",
    "weekly_settings",
    "daily_task",
    *tuple(f"task.{day}" for day in WEEKDAYS),
    "latest_activation",
    "crontab",
)
FILE_TARGET_KEYS = (
    *tuple(f"task.{day}" for day in WEEKDAYS),
    "schedule_manifest",
    "weekly_settings",
    "latest_activation",
)
ALL_TARGET_KEYS = (*FILE_TARGET_KEYS, "crontab")
REQUEST_KEYS = {
    "filename",
    "sha256",
    "mode",
    "anchor_date",
    "anchor_time",
    "confirmed_inference",
    "enable_weekly_plan",
    "days",
    "cycle",
    "adopted_line_hashes",
}
REQUIRED_REQUEST_KEYS = REQUEST_KEYS - {"anchor_date", "cycle"}
MISSING_HASH = "missing"


class SimulatedProcessDeath(BaseException):
    """Test seam for a process ending without exception compensation."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source_hash(path: Path) -> str:
    try:
        return _sha(Path(path).read_bytes())
    except FileNotFoundError:
        return MISSING_HASH


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _backup_root(config_dir: Path) -> Path:
    return Path(config_dir) / BACKUP_DIR_PARTS[0] / BACKUP_DIR_PARTS[1]


def _target_path(config_dir: Path, key: str) -> Path:
    config_dir = Path(config_dir)
    if key.startswith("task.") and key[5:] in WEEKDAYS:
        return config_dir / "tasks" / f"daily-{key[5:]}.toml"
    mapping = {
        "schedule_manifest": config_dir / SCHEDULE_MANIFEST,
        "weekly_settings": config_dir / "weekly-plan.json",
        "latest_activation": _backup_root(config_dir) / LATEST_ACTIVATION,
    }
    try:
        return mapping[key]
    except KeyError as exc:
        _schedule_error("snapshot_invalid", "$.targets", "事务包含未知逻辑目标")
        raise AssertionError from exc


def _assert_safe_fixed_path(config_dir: Path, path: Path, key: str) -> None:
    config_dir = Path(config_dir).absolute()
    path = Path(path).absolute()
    try:
        path.relative_to(config_dir)
    except ValueError:
        _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务目标逃逸配置目录")
    current = path.parent
    while current != config_dir:
        if current.is_symlink():
            _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务目标父目录不能是符号链接")
        if current == current.parent:
            _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务目标路径无效")
        current = current.parent


def _read_target(config_dir: Path, key: str, crontab_raw: bytes) -> bytes | None:
    if key == "crontab":
        return crontab_raw
    path = _target_path(config_dir, key)
    _assert_safe_fixed_path(config_dir, path, key)
    try:
        if path.is_symlink() or not path.is_file():
            if path.exists():
                _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务目标必须是普通文件")
            return None
        return path.read_bytes()
    except OSError as exc:
        _schedule_error("snapshot_invalid", f"$.targets.{key}", f"无法读取事务目标: {exc}")


def _hash_optional(raw: bytes | None) -> str:
    return MISSING_HASH if raw is None else _sha(raw)


def _source_hashes(config_dir: Path, filename: str, crontab_raw: bytes) -> dict[str, str]:
    config_dir = Path(config_dir)
    result = {
        "json": (
            _source_hash(config_dir / "infrast" / filename)
            if filename
            else MISSING_HASH
        ),
        "schedule_manifest": _source_hash(config_dir / SCHEDULE_MANIFEST),
        "weekly_settings": _source_hash(config_dir / "weekly-plan.json"),
        "daily_task": _source_hash(config_dir / "tasks" / "daily.toml"),
        "latest_activation": _source_hash(_backup_root(config_dir) / LATEST_ACTIVATION),
        "crontab": _sha(crontab_raw),
    }
    result.update(
        {
            f"task.{day}": _source_hash(
                config_dir / "tasks" / f"daily-{day}.toml"
            )
            for day in WEEKDAYS
        }
    )
    return result


def _validate_hash_map(
    value: object, keys: tuple[str, ...] | set[str], path: str
) -> dict[str, str]:
    expected = set(keys)
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or any(not isinstance(item, str) for item in value.values())
    ):
        _schedule_error("stale_preview", path, "预览哈希字段缺失、额外或无效")
    return value


def _load_manifest(config_dir: Path) -> dict[str, object] | None:
    path = Path(config_dir) / SCHEDULE_MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _schedule_error("invalid_schedule_manifest", "$.schedule_manifest", "Stage C 清单损坏")
    if not isinstance(value, dict):
        _schedule_error("invalid_schedule_manifest", "$.schedule_manifest", "Stage C 清单必须是对象")
    return value


def _normalize_request(
    config_dir: Path, request: dict, crontab_raw: bytes
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(request, dict) or set(request) - REQUEST_KEYS:
        _schedule_error("invalid_request", "$", "排班请求包含未知字段")
    missing = REQUIRED_REQUEST_KEYS - set(request)
    if missing:
        _schedule_error("invalid_request", "$", "排班请求缺少必填字段")
    filename = request["filename"]
    if not isinstance(filename, str) or not filename:
        _schedule_error("invalid_filename", "$.filename", "文件名不能为空")
    raw = read_plan_bytes(Path(config_dir) / "infrast", filename)
    actual_sha = _sha(raw)
    if (
        not isinstance(request["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", request["sha256"])
        or request["sha256"] != actual_sha
    ):
        _schedule_error("stale_preview", "$.sha256", "基建 JSON 已变化")
    mode = request["mode"]
    if mode not in ("preserve", "replace"):
        _schedule_error("invalid_mode", "$.mode", "mode 必须是 preserve 或 replace")
    anchor_time = request["anchor_time"]
    _time_to_minute(anchor_time, "$.anchor_time")
    document = parse_plan_document(raw, filename)
    analysis = analyze_plan_document(document, anchor_time)
    if analysis["requires_confirmation"] and request["confirmed_inference"] is not True:
        _schedule_error(
            "inference_confirmation_required",
            "$.confirmed_inference",
            "推测班次必须确认后才能保存",
        )
    if not isinstance(request["enable_weekly_plan"], bool):
        _schedule_error("invalid_request", "$.enable_weekly_plan", "周计划确认必须是布尔值")
    if not isinstance(request["adopted_line_hashes"], list) or not all(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        for value in request["adopted_line_hashes"]
    ):
        _schedule_error("invalid_adoption", "$.adopted_line_hashes", "纳管哈希无效")

    plan_count = len(document["plans"])
    analysis = dict(analysis)
    raw_days = request["days"]
    cycle = None
    anchor_date = request.get("anchor_date")
    if analysis["cross_day_cycle"]:
        if mode != "replace":
            _schedule_error(
                "cycle_requires_replace",
                "$.mode",
                "跨日循环必须由独立基建计划接管执行时间",
            )
        if raw_days != {}:
            _schedule_error(
                "invalid_cycle_days",
                "$.days",
                "跨日循环的七日视图由锚点自动生成",
            )
        cycle = build_cycle_definition(
            analysis["durations_hours"],
            [_slot_name(plan, index) for index, plan in enumerate(document["plans"])],
            anchor_date,
            anchor_time,
        )
        normalized_days: dict[str, list[dict[str, object]]] = {}
        analysis["selected_intervals_hours"] = {}
        analysis["timing_mismatch"] = False
    else:
        if not isinstance(raw_days, dict) or set(raw_days) != set(WEEKDAYS):
            _schedule_error("invalid_weekday", "$.days", "必须明确配置完整七天")
        normalized_days = {}
        for day in WEEKDAYS:
            slots = raw_days[day]
            normalized_slots = _normalized_slots(slots, f"$.days.{day}")
            indexes = [slot["plan_index"] for slot in normalized_slots]
            if sorted(indexes) != list(range(plan_count)):
                _schedule_error(
                    "invalid_plan_mapping",
                    f"$.days.{day}",
                    "每个启用日必须且只能包含一次全部 plan_index",
                )
            day_slots = []
            for index, slot in enumerate(normalized_slots):
                slot_path = f"$.days.{day}[{index}]"
                source_time = slot.get("source_time")
                _time_to_minute(source_time, f"{slot_path}.source_time")
                name = slot.get("name")
                source = slot.get("source")
                if not isinstance(name, str) or not name:
                    _schedule_error("invalid_slots", f"{slot_path}.name", "班次名称不能为空")
                if source not in ("period", "inferred", "manual"):
                    _schedule_error("invalid_slots", f"{slot_path}.source", "班次来源无效")
                day_slots.append(
                    {
                        "time": slot["time"],
                        "plan_index": slot["plan_index"],
                        "name": name,
                        "source_time": source_time,
                        "source": source,
                    }
                )
            normalized_days[day] = day_slots
        selected_intervals = {}
        for day, slots in normalized_days.items():
            minutes = [_time_to_minute(slot["time"], f"$.days.{day}") for slot in slots]
            selected_intervals[day] = [
                ((minutes[(index + 1) % len(minutes)] - minute) % MINUTES_PER_DAY)
                / 60
                for index, minute in enumerate(minutes)
            ]
        analysis["selected_intervals_hours"] = selected_intervals
        analysis["timing_mismatch"] = any(
            intervals != analysis["durations_hours"]
            for intervals in selected_intervals.values()
        )
        if mode == "preserve" and analysis["timing_mismatch"]:
            analysis["warnings"] = [
                *analysis["warnings"],
                "保留的实际日常时间间隔与 JSON 建议班次时长不一致",
            ]
    if "cycle" in request and request["cycle"] != cycle:
        _schedule_error(
            "stale_preview",
            "$.cycle",
            "服务端归一化的跨日循环已变化",
        )
    return (
        {
            "filename": filename,
            "sha256": actual_sha,
            "mode": mode,
            "anchor_date": anchor_date,
            "anchor_time": anchor_time,
            "confirmed_inference": request["confirmed_inference"],
            "enable_weekly_plan": request["enable_weekly_plan"],
            "days": normalized_days,
            "cycle": cycle,
            "adopted_line_hashes": list(request["adopted_line_hashes"]),
        },
        analysis,
    )


def _stable_manifest_semantics(
    manifest: dict[str, object] | None, normalized: dict[str, object]
) -> bool:
    if not manifest:
        return False
    if normalized.get("cycle") is not None:
        return bool(
            manifest.get("schema_version") == 2
            and manifest.get("enabled") is True
            and manifest.get("filename") == normalized["filename"]
            and manifest.get("json_sha256") == normalized["sha256"]
            and manifest.get("mode") == normalized["mode"]
            and manifest.get("days") == {}
            and manifest.get("cycle") == normalized["cycle"]
            and isinstance(manifest.get("transaction_id"), str)
            and isinstance(manifest.get("updated_at"), str)
        )
    return bool(
        manifest.get("schema_version") == 1
        and manifest.get("enabled") is True
        and manifest.get("filename") == normalized["filename"]
        and manifest.get("json_sha256") == normalized["sha256"]
        and manifest.get("mode") == normalized["mode"]
        and manifest.get("days") == normalized["days"]
        and isinstance(manifest.get("transaction_id"), str)
        and isinstance(manifest.get("updated_at"), str)
    )


def _consumed_adoptions_match_activation(
    config_dir: Path, adopted_line_hashes: list[str]
) -> bool:
    if not adopted_line_hashes:
        return False
    try:
        _snapshot_id, _snapshot_sha, snapshot, tx_dir = _select_snapshot(
            config_dir, None
        )
        original_crontab = _original_bytes(
            tx_dir, "crontab", snapshot["targets"]["crontab"]["sha256"]
        )
    except ScheduleError:
        return False
    if original_crontab is None:
        return False
    candidates = {
        item["sha256"]
        for item in parse_managed_crontab(original_crontab)["unmanaged_daily_lines"]
    }
    return set(adopted_line_hashes).issubset(candidates)


def _safe_consumed_adoption_noop(
    config_dir: Path,
    request: dict,
    crontab_raw: bytes,
    manifest: dict[str, object] | None,
    normalized: dict[str, object],
) -> tuple[dict[str, object], dict[str, bytes | None], bytes | None] | None:
    adopted = normalized["adopted_line_hashes"]
    if (
        not _stable_manifest_semantics(manifest, normalized)
        or not adopted
        or not _consumed_adoptions_match_activation(config_dir, adopted)
    ):
        return None
    cleared_request = copy.deepcopy(request)
    cleared_request["adopted_line_hashes"] = []
    meta, candidates, snapshot_raw = _preview_change_with_metadata(
        config_dir,
        cleared_request,
        crontab_raw,
        manifest["transaction_id"],
        manifest["updated_at"],
    )
    if not _all_candidates_current(config_dir, candidates, crontab_raw):
        return None
    return meta, candidates, snapshot_raw


def _load_tasks_strict(path: Path, fallback: Path | None = None) -> list[dict[str, object]]:
    selected = path
    try:
        raw = selected.read_bytes()
    except OSError:
        raw = b""
    valid, _ = validate_task_bytes(raw)
    if not valid and fallback is not None:
        selected = fallback
        try:
            raw = selected.read_bytes()
        except OSError:
            raw = b""
        valid, error = validate_task_bytes(raw)
        if not valid:
            _schedule_error("invalid_daily_task", "$.daily_task", f"daily.toml 无法使用: {error}")
    elif not valid:
        _schedule_error("invalid_daily_task", "$.daily_task", "任务 TOML 无法使用")
    tasks = load_tasks(selected)
    if not tasks:
        _schedule_error("invalid_daily_task", "$.daily_task", "任务 TOML 没有可用任务")
    return tasks


def _snapshot_document(
    snapshot_id: str, originals: dict[str, bytes | None]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "targets": {
            key: {"sha256": _hash_optional(originals[key])}
            for key in ALL_TARGET_KEYS
        },
    }


def _pointer_bytes(snapshot_id: str, snapshot_sha256: str) -> bytes:
    return _json_bytes(
        {"snapshot_id": snapshot_id, "snapshot_sha256": snapshot_sha256}
    )


def _candidate_change(
    config_dir: Path,
    normalized: dict[str, object],
    crontab_raw: bytes,
    transaction_id: str,
    generated_at: str,
    initial_activation: bool,
) -> tuple[dict[str, bytes | None], bytes | None]:
    config_dir = Path(config_dir)
    daily = config_dir / "tasks" / "daily.toml"
    previous = _load_manifest(config_dir)
    cycle = normalized.get("cycle")
    candidates: dict[str, bytes | None] = {}
    for day in WEEKDAYS:
        weekday_path = config_dir / "tasks" / f"daily-{day}.toml"
        tasks = _load_tasks_strict(weekday_path, daily)
        previous_slots = None
        previous_cycle = None
        if previous and previous.get("enabled") is True:
            if previous.get("schema_version") == 2 and isinstance(
                previous.get("cycle"), dict
            ):
                previous_cycle = previous["cycle"]
            previous_days = previous.get("days")
            if previous_cycle is None and isinstance(previous_days, dict):
                previous_slots = previous_days.get(day)
        compiled = compile_weekday_tasks(
            tasks,
            normalized["filename"],
            normalized["days"].get(day, []),
            previous_slots=previous_slots,
            cycle=cycle,
            previous_cycle=previous_cycle,
        )
        raw = render_tasks(compiled, generated_at=generated_at).encode("utf-8")
        valid, error = validate_task_bytes(raw)
        if not valid:
            _schedule_error("invalid_candidate_toml", f"$.task.{day}", error)
        candidates[f"task.{day}"] = raw
    manifest = {
        "schema_version": 2 if cycle is not None else 1,
        "enabled": True,
        "filename": normalized["filename"],
        "json_sha256": normalized["sha256"],
        "mode": normalized["mode"],
        "days": normalized["days"],
        "transaction_id": transaction_id,
        "updated_at": generated_at,
    }
    if cycle is not None:
        manifest["cycle"] = cycle
    candidates["schedule_manifest"] = _json_bytes(manifest)
    candidates["weekly_settings"] = _json_bytes(
        {"enabled": True, "initialized": True}
    )
    entries = (
        build_cycle_cron_entries(cycle)
        if cycle is not None
        else build_cron_entries(normalized["days"])
    )
    candidates["crontab"] = render_managed_crontab(
        crontab_raw, entries, set(normalized["adopted_line_hashes"])
    )
    snapshot_raw = None
    if initial_activation:
        originals = {
            key: _read_target(config_dir, key, crontab_raw)
            for key in ALL_TARGET_KEYS
        }
        snapshot_raw = _json_bytes(_snapshot_document(transaction_id, originals))
        candidates["latest_activation"] = _pointer_bytes(
            transaction_id, _sha(snapshot_raw)
        )
    return candidates, snapshot_raw


def _validate_candidate_consistency(
    candidates: dict[str, bytes | None], normalized: dict[str, object]
) -> None:
    for day in WEEKDAYS:
        raw = candidates.get(f"task.{day}")
        if not isinstance(raw, bytes):
            _schedule_error("invalid_candidate_toml", f"$.task.{day}", "候选 TOML 缺失")
        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            _schedule_error("invalid_candidate_toml", f"$.task.{day}", "候选 TOML 无法解析")
        active = document.get("tasks")
        if not isinstance(active, list):
            _schedule_error("invalid_candidate_toml", f"$.task.{day}", "候选任务数组缺失")
        infrast = [
            task
            for task in active
            if isinstance(task, dict) and task.get("type") == "Infrast"
        ]
        cycle = normalized.get("cycle")
        expected_variants = (
            build_cycle_variants(cycle)
            if cycle is not None
            else _managed_variants(normalized["days"][day])
        )
        if (
            len(infrast) != 1
            or infrast[0].get("strategy") != "first"
            or not isinstance(infrast[0].get("params"), dict)
            or infrast[0]["params"].get("mode") != 10000
            or infrast[0]["params"].get("filename") != normalized["filename"]
            or infrast[0].get("variants") != expected_variants
        ):
            _schedule_error(
                "invalid_candidate_toml",
                f"$.task.{day}",
                "候选 TOML 与规范化排班不一致",
            )
    manifest_raw = candidates.get("schedule_manifest")
    if not isinstance(manifest_raw, bytes):
        _schedule_error("invalid_schedule_manifest", "$.schedule_manifest", "候选清单缺失")
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _schedule_error("invalid_schedule_manifest", "$.schedule_manifest", "候选清单无效")
    if (
        manifest.get("filename") != normalized["filename"]
        or manifest.get("json_sha256") != normalized["sha256"]
        or manifest.get("days") != normalized["days"]
        or manifest.get("cycle") != normalized.get("cycle")
    ):
        _schedule_error("invalid_schedule_manifest", "$.schedule_manifest", "候选清单与排班不一致")
    expected_lines = [
        _canonical_cron_line(entry)
        for entry in (
            build_cycle_cron_entries(normalized["cycle"])
            if normalized.get("cycle") is not None
            else build_cron_entries(normalized["days"])
        )
    ]
    crontab = candidates.get("crontab")
    if not isinstance(crontab, bytes):
        _schedule_error("invalid_crontab", "$.crontab", "候选 crontab 缺失")
    parsed = parse_managed_crontab(crontab)
    managed_lines = [
        line
        for line in parsed["managed_block"].splitlines()
        if line.strip()
    ]
    if managed_lines != expected_lines:
        _schedule_error("invalid_crontab", "$.crontab", "候选 cron 与规范化排班不一致")


def _iso_timestamp(value: str) -> None:
    if not isinstance(value, str):
        _schedule_error("invalid_transaction", "$.generated_at", "事务时间无效")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _schedule_error("invalid_transaction", "$.generated_at", "事务时间无效")


def _preview_change_with_metadata(
    config_dir: Path,
    request: dict,
    crontab_raw: bytes,
    transaction_id: str,
    generated_at: str,
) -> tuple[dict[str, object], dict[str, bytes | None], bytes | None]:
    normalized, analysis = _normalize_request(config_dir, request, crontab_raw)
    settings = load_settings(config_dir)
    if not settings["enabled"] and normalized["enable_weekly_plan"] is not True:
        _schedule_error(
            "weekly_enable_required",
            "$.enable_weekly_plan",
            "启用基建排班前必须明确启用周计划",
        )
    manifest = _load_manifest(config_dir)
    initial = not manifest or manifest.get("enabled") is not True
    candidates, snapshot_raw = _candidate_change(
        config_dir,
        normalized,
        crontab_raw,
        transaction_id,
        generated_at,
        initial,
    )
    _validate_candidate_consistency(candidates, normalized)
    return (
        {
            "normalized_request": normalized,
            "analysis": analysis,
            "initial_activation": initial,
        },
        candidates,
        snapshot_raw,
    )


def _candidate_hashes(candidates: dict[str, bytes | None]) -> dict[str, str]:
    return {key: _hash_optional(raw) for key, raw in candidates.items()}


def _all_candidates_current(
    config_dir: Path, candidates: dict[str, bytes | None], crontab_raw: bytes
) -> bool:
    return all(
        _read_target(config_dir, key, crontab_raw) == raw
        for key, raw in candidates.items()
    )


def preview_schedule_change(
    config_dir: Path, request: dict, crontab_raw: bytes
) -> dict[str, object]:
    """Build an immutable, write-free Stage C schedule preview."""
    if not isinstance(crontab_raw, bytes):
        _schedule_error("invalid_crontab", "$.crontab", "crontab 必须是字节串")
    normalized, _ = _normalize_request(config_dir, request, crontab_raw)
    manifest = _load_manifest(config_dir)
    if _stable_manifest_semantics(manifest, normalized):
        transaction_id = manifest["transaction_id"]
        generated_at = manifest["updated_at"]
        consumed_noop = _safe_consumed_adoption_noop(
            config_dir, request, crontab_raw, manifest, normalized
        )
        if consumed_noop is not None:
            meta, candidates, _ = consumed_noop
        else:
            meta, candidates, _ = _preview_change_with_metadata(
                config_dir, request, crontab_raw, transaction_id, generated_at
            )
        if _all_candidates_current(config_dir, candidates, crontab_raw):
            return {
                "status": "unchanged",
                "transaction_id": transaction_id,
                "generated_at": generated_at,
                "source_hashes": _source_hashes(
                    config_dir, normalized["filename"], crontab_raw
                ),
                "target_hashes": _candidate_hashes(candidates),
                "request": meta["normalized_request"],
                "analysis": meta["analysis"],
                "task_diff": [],
                "cron_diff": [],
            }
    transaction_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    meta, candidates, _ = _preview_change_with_metadata(
        config_dir, request, crontab_raw, transaction_id, generated_at
    )
    current_hashes = {
        key: _hash_optional(_read_target(config_dir, key, crontab_raw))
        for key in candidates
    }
    return {
        "status": "changed",
        "transaction_id": transaction_id,
        "generated_at": generated_at,
        "source_hashes": _source_hashes(
            config_dir, meta["normalized_request"]["filename"], crontab_raw
        ),
        "target_hashes": _candidate_hashes(candidates),
        "request": meta["normalized_request"],
        "analysis": meta["analysis"],
        "task_diff": [
            {
                "target": key,
                "before_sha256": current_hashes[key],
                "after_sha256": _hash_optional(candidates[key]),
            }
            for key in candidates
            if key.startswith("task.") and current_hashes[key] != _hash_optional(candidates[key])
        ],
        "cron_diff": {
            "before_sha256": current_hashes["crontab"],
            "after_sha256": _hash_optional(candidates["crontab"]),
        },
    }


class _TransactionIO:
    """Injectable durable file/crontab I/O used by commit and recovery."""

    def __init__(
        self,
        config_dir: Path,
        read_crontab=None,
        install_crontab=None,
        fail_at: int | None = None,
    ):
        self.config_dir = Path(config_dir)
        self._read_crontab = read_crontab
        self._install_crontab = install_crontab
        self.fail_at = fail_at
        self.operation_count = 0
        self.events: list[str] = []

    def _checkpoint(self, event: str) -> None:
        self.operation_count += 1
        self.events.append(event)
        if self.fail_at == self.operation_count:
            raise OSError(f"injected transaction I/O failure #{self.operation_count}: {event}")

    def fsync_dir(self, path: Path) -> None:
        self.events.append(f"fsync_dir:{Path(path).name}")
        if os.name == "nt":
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def replace_bytes(self, path: Path, raw: bytes) -> None:
        path = Path(path)
        _assert_safe_fixed_path(self.config_dir, path, path.name)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            _schedule_error("snapshot_invalid", "$.targets", "事务目标必须是普通文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            os.replace(temp_name, path)
            temp_name = ""
            self.fsync_dir(path.parent)
            self._checkpoint(f"replace:{path.name}")
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

    def remove(self, path: Path) -> None:
        path = Path(path)
        _assert_safe_fixed_path(self.config_dir, path, path.name)
        try:
            if path.is_symlink() or not path.is_file():
                _schedule_error("snapshot_invalid", "$.targets", "事务目标必须是普通文件")
            path.unlink()
            self.fsync_dir(path.parent)
            self._checkpoint(f"remove:{path.name}")
        except FileNotFoundError:
            pass

    def read_crontab(self) -> bytes:
        if self._read_crontab is None:
            raise RuntimeError("read_crontab callback is required")
        raw = self._read_crontab()
        if not isinstance(raw, bytes):
            _schedule_error("invalid_crontab", "$.crontab", "crontab 回读必须是字节串")
        return raw

    def install_crontab(self, raw: bytes) -> None:
        if self._install_crontab is None:
            raise RuntimeError("install_crontab callback is required")
        self._install_crontab(raw)
        self._checkpoint("install_crontab")


_transaction_io_factory = _TransactionIO


def _backup_filename(key: str) -> str:
    return "original-" + key.replace(".", "-") + ".bin"


def _prepared_journal_paths(config_dir: Path) -> list[Path]:
    root = _backup_root(config_dir)
    if root.is_symlink():
        _schedule_error("snapshot_invalid", "$.transaction", "事务根目录不能是符号链接")
    if not root.exists():
        return []
    prepared = []
    for path in root.glob("*/transaction.json"):
        if path.parent.is_symlink():
            _schedule_error("snapshot_invalid", "$.transaction", "事务目录不能是符号链接")
        value, _ = _load_json_strict(path, "snapshot_invalid", "snapshot_invalid")
        if value.get("state") not in ("prepared", "committed", "rolled_back"):
            _schedule_error("snapshot_invalid", "$.transaction", "事务状态无效")
        if value["state"] == "prepared":
            prepared.append(path)
    return prepared


def _require_recovered_state(config_dir: Path) -> None:
    if _prepared_journal_paths(config_dir):
        _schedule_error(
            "pending_transaction",
            "$.transaction",
            "存在待恢复事务，必须先完成恢复",
        )


def _write_journal(io: _TransactionIO, path: Path, journal: dict[str, object]) -> None:
    io.replace_bytes(path, _json_bytes(journal))


def _validate_transaction_id(value: str, path: str = "$.transaction_id") -> str:
    if not isinstance(value, str):
        _schedule_error("invalid_transaction", path, "事务 ID 无效")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        _schedule_error("invalid_transaction", path, "事务 ID 无效")
    if str(parsed) != value:
        _schedule_error("invalid_transaction", path, "事务 ID 必须是规范 UUID")
    return value


def _prepare_transaction(
    config_dir: Path,
    transaction_id: str,
    kind: str,
    candidates: dict[str, bytes | None],
    current_crontab: bytes,
    io: _TransactionIO,
    snapshot_raw: bytes | None = None,
    activation: bool = False,
) -> tuple[Path, dict[str, object]]:
    _validate_transaction_id(transaction_id)
    if set(candidates) - set(ALL_TARGET_KEYS):
        _schedule_error("snapshot_invalid", "$.targets", "事务包含未知逻辑目标")
    tx_dir = _backup_root(config_dir) / transaction_id
    journal_path = tx_dir / "transaction.json"
    _assert_safe_fixed_path(config_dir, journal_path, "transaction")
    if tx_dir.exists():
        if not tx_dir.is_dir() or tx_dir.is_symlink():
            _schedule_error("transaction_id_reused", "$.transaction_id", "事务目录无效")
        try:
            prior, _ = _load_json_strict(
                journal_path, "transaction_id_reused", "transaction_id_reused"
            )
        except ScheduleError:
            _schedule_error("transaction_id_reused", "$.transaction_id", "事务 ID 已存在")
        if prior.get("target_hashes") != _candidate_hashes(candidates):
            _schedule_error("transaction_id_reused", "$.transaction_id", "事务 ID 已用于不同内容")
        return journal_path, prior
    tx_dir.mkdir(parents=True, exist_ok=False)
    io.fsync_dir(tx_dir.parent)
    originals = {
        key: _read_target(config_dir, key, current_crontab)
        for key in candidates
    }
    for key, raw in originals.items():
        if raw is not None:
            io.replace_bytes(tx_dir / _backup_filename(key), raw)
    if snapshot_raw is not None:
        io.replace_bytes(tx_dir / "snapshot.json", snapshot_raw)
    journal = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "kind": kind,
        "activation": activation,
        "state": "prepared",
        "target_hashes": _candidate_hashes(candidates),
        "targets": [
            {
                "key": key,
                "original_sha256": _hash_optional(originals[key]),
                "candidate_sha256": _hash_optional(candidates[key]),
                "applied": False,
            }
            for key in candidates
        ],
    }
    _write_journal(io, journal_path, journal)
    return journal_path, journal


def _journal_entry_map(journal: dict[str, object]) -> dict[str, dict[str, object]]:
    targets = journal.get("targets")
    if not isinstance(targets, list):
        _schedule_error("snapshot_invalid", "$.targets", "事务目标清单无效")
    result = {}
    for item in targets:
        if (
            not isinstance(item, dict)
            or set(item) != {"key", "original_sha256", "candidate_sha256", "applied"}
            or item.get("key") not in ALL_TARGET_KEYS
            or item["key"] in result
            or not isinstance(item.get("original_sha256"), str)
            or not isinstance(item.get("candidate_sha256"), str)
            or not isinstance(item.get("applied"), bool)
        ):
            _schedule_error("snapshot_invalid", "$.targets", "事务目标清单无效")
        result[item["key"]] = item
    return result


def _original_bytes(tx_dir: Path, key: str, expected_sha: str) -> bytes | None:
    if expected_sha == MISSING_HASH:
        return None
    path = tx_dir / _backup_filename(key)
    try:
        if path.is_symlink() or not path.is_file():
            _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务备份缺失")
        raw = path.read_bytes()
    except OSError:
        _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务备份缺失")
    if _sha(raw) != expected_sha:
        _schedule_error("snapshot_invalid", f"$.targets.{key}", "事务备份哈希不匹配")
    return raw


def _set_target(
    config_dir: Path, key: str, raw: bytes | None, io: _TransactionIO
) -> None:
    if key == "crontab":
        if raw is None:
            _schedule_error("snapshot_invalid", "$.targets.crontab", "crontab 不能缺失")
        io.install_crontab(raw)
    elif raw is None:
        io.remove(_target_path(config_dir, key))
    else:
        io.replace_bytes(_target_path(config_dir, key), raw)


def _rollback_journal(
    config_dir: Path,
    tx_dir: Path,
    journal: dict[str, object],
    io: _TransactionIO,
    current_crontab: bytes | None = None,
) -> dict[str, object]:
    _journal_entry_map(journal)

    def recovery_crontab():
        if current_crontab is not None:
            return current_crontab
        return io.read_crontab()

    conflicts = []
    for item in reversed(journal["targets"]):
        key = item["key"]
        if key == "crontab":
            current = recovery_crontab()
        else:
            current = _read_target(config_dir, key, b"")
        current_sha = None if current is None and key == "crontab" else _hash_optional(current)
        if current_sha is None:
            # The journal flag can lag the external cron install if the process
            # dies between those two operations. Never infer "not installed"
            # from applied=False when current cron bytes cannot be verified.
            conflicts.append("crontab")
            continue
        if current_sha == item["original_sha256"]:
            continue
        if current_sha != item["candidate_sha256"]:
            conflicts.append(key)
    if conflicts:
        return {
            "status": "blocked",
            "code": "recovery_conflict",
            "transaction_id": journal.get("transaction_id"),
            "targets": conflicts,
        }
    for item in reversed(journal["targets"]):
        key = item["key"]
        if key == "crontab":
            current = recovery_crontab()
        else:
            current = _read_target(config_dir, key, b"")
        current_sha = None if current is None and key == "crontab" else _hash_optional(current)
        if current_sha == item["candidate_sha256"]:
            original = _original_bytes(tx_dir, key, item["original_sha256"])
            _set_target(config_dir, key, original, io)
    journal["state"] = "rolled_back"
    _write_journal(io, tx_dir / "transaction.json", journal)
    return {
        "status": "rolled_back",
        "transaction_id": journal.get("transaction_id"),
    }


def _apply_transaction(
    config_dir: Path,
    transaction_id: str,
    kind: str,
    candidates: dict[str, bytes | None],
    expected_crontab_sha: str,
    read_crontab,
    install_crontab,
    snapshot_raw: bytes | None = None,
    activation: bool = False,
) -> dict[str, object]:
    io = _transaction_io_factory(config_dir, read_crontab, install_crontab)
    current_crontab = io.read_crontab()
    if _sha(current_crontab) != expected_crontab_sha:
        _schedule_error("stale_preview", "$.source_hashes.crontab", "crontab 已变化")
    journal_path, journal = _prepare_transaction(
        config_dir,
        transaction_id,
        kind,
        candidates,
        current_crontab,
        io,
        snapshot_raw=snapshot_raw,
        activation=activation,
    )
    if journal.get("state") == "committed":
        return {"status": "unchanged", "transaction_id": transaction_id}
    entries = _journal_entry_map(journal)
    try:
        order = [
            *[f"task.{day}" for day in WEEKDAYS if f"task.{day}" in candidates],
            *[
                key
                for key in ("schedule_manifest", "weekly_settings", "latest_activation")
                if key in candidates
            ],
        ]
        for key in order:
            _set_target(config_dir, key, candidates[key], io)
            entries[key]["applied"] = True
            _write_journal(io, journal_path, journal)
        if "crontab" in candidates:
            if _sha(io.read_crontab()) != expected_crontab_sha:
                _schedule_error("stale_preview", "$.source_hashes.crontab", "crontab 已变化")
            _set_target(config_dir, "crontab", candidates["crontab"], io)
            entries["crontab"]["applied"] = True
            _write_journal(io, journal_path, journal)
            if io.read_crontab() != candidates["crontab"]:
                raise OSError("crontab read-back did not match installed bytes")
        journal["state"] = "committed"
        _write_journal(io, journal_path, journal)
        return {"status": "committed", "transaction_id": transaction_id}
    except Exception:
        try:
            current = io.read_crontab()
        except Exception:
            current = None
        rollback = _rollback_journal(
            config_dir, journal_path.parent, journal, io, current
        )
        if rollback.get("code") == "recovery_conflict":
            raise ScheduleError(
                "recovery_conflict",
                "$.transaction",
                "事务失败且检测到外部修改，已保留 journal",
            )
        raise


def commit_schedule_change(
    config_dir: Path,
    request: dict,
    expected_hashes: dict,
    expected_target_hashes: dict,
    transaction_id: str,
    generated_at: str,
    read_crontab,
    install_crontab,
) -> dict[str, object]:
    """Re-render a reviewed preview and apply it as one recoverable transaction."""
    _require_recovered_state(config_dir)
    _validate_transaction_id(transaction_id)
    _iso_timestamp(generated_at)
    crontab_raw = read_crontab()
    normalized, _ = _normalize_request(config_dir, request, crontab_raw)
    expected = _validate_hash_map(expected_hashes, SOURCE_HASH_KEYS, "$.expected_hashes")
    current = _source_hashes(config_dir, normalized["filename"], crontab_raw)
    if current != expected:
        _schedule_error("stale_preview", "$.expected_hashes", "预览来源已变化")
    manifest = _load_manifest(config_dir)
    consumed_noop = _safe_consumed_adoption_noop(
        config_dir, request, crontab_raw, manifest, normalized
    )
    if consumed_noop is not None:
        _meta, candidates, _snapshot_raw = consumed_noop
        targets = _validate_hash_map(
            expected_target_hashes, set(candidates), "$.expected_target_hashes"
        )
        if _candidate_hashes(candidates) != targets:
            _schedule_error(
                "stale_preview",
                "$.expected_target_hashes",
                "预览目标与已提交排班不一致",
            )
        return {
            "status": "unchanged",
            "transaction_id": manifest["transaction_id"],
        }
    meta, candidates, snapshot_raw = _preview_change_with_metadata(
        config_dir, request, crontab_raw, transaction_id, generated_at
    )
    targets = _validate_hash_map(
        expected_target_hashes, set(candidates), "$.expected_target_hashes"
    )
    if _candidate_hashes(candidates) != targets:
        _schedule_error("stale_preview", "$.expected_target_hashes", "预览目标与重新渲染结果不一致")
    if _all_candidates_current(config_dir, candidates, crontab_raw):
        return {"status": "unchanged", "transaction_id": transaction_id}
    return _apply_transaction(
        config_dir,
        transaction_id,
        "schedule_change",
        candidates,
        expected["crontab"],
        read_crontab,
        install_crontab,
        snapshot_raw=snapshot_raw,
        activation=meta["initial_activation"],
    )


def _load_json_strict(path: Path, missing_code: str, invalid_code: str) -> tuple[dict, bytes]:
    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        if path.is_symlink() or not path.is_file():
            if path.exists():
                _schedule_error(invalid_code, "$.snapshot", "快照路径必须是普通文件")
            _schedule_error(missing_code, "$.snapshot", "快照不存在")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except ScheduleError:
        raise
    except FileNotFoundError:
        _schedule_error(missing_code, "$.snapshot", "快照不存在")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _schedule_error(invalid_code, "$.snapshot", "快照损坏")
    if not isinstance(value, dict):
        _schedule_error(invalid_code, "$.snapshot", "快照必须是对象")
    return value, raw


def _select_snapshot(
    config_dir: Path, snapshot_id: str | None
) -> tuple[str, str, dict[str, object], Path]:
    root = _backup_root(config_dir)
    if snapshot_id is None:
        pointer, _ = _load_json_strict(
            root / LATEST_ACTIVATION, "snapshot_missing", "snapshot_invalid"
        )
        if set(pointer) != {"snapshot_id", "snapshot_sha256"}:
            _schedule_error("snapshot_invalid", "$.snapshot", "恢复指针字段无效")
        snapshot_id = pointer["snapshot_id"]
        expected_sha = pointer["snapshot_sha256"]
        if not isinstance(expected_sha, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha
        ):
            _schedule_error("snapshot_invalid", "$.snapshot", "恢复指针哈希无效")
    else:
        expected_sha = None
    try:
        _validate_transaction_id(snapshot_id, "$.snapshot_id")
    except ScheduleError:
        _schedule_error("snapshot_invalid", "$.snapshot_id", "快照 ID 无效")
    tx_dir = root / snapshot_id
    if root.is_symlink() or tx_dir.is_symlink() or not tx_dir.is_dir():
        if not tx_dir.exists():
            _schedule_error("snapshot_missing", "$.snapshot", "快照不存在")
        _schedule_error("snapshot_invalid", "$.snapshot", "快照目录无效")
    transaction, _ = _load_json_strict(
        tx_dir / "transaction.json", "snapshot_missing", "snapshot_invalid"
    )
    if (
        transaction.get("state") != "committed"
        or transaction.get("activation") is not True
        or transaction.get("transaction_id") != snapshot_id
    ):
        _schedule_error("snapshot_invalid", "$.snapshot", "快照不是已提交的激活基线")
    snapshot, snapshot_raw = _load_json_strict(
        tx_dir / "snapshot.json", "snapshot_missing", "snapshot_invalid"
    )
    snapshot_sha = _sha(snapshot_raw)
    if expected_sha is not None and expected_sha != snapshot_sha:
        _schedule_error("snapshot_invalid", "$.snapshot", "恢复指针哈希不匹配")
    if (
        set(snapshot) != {"schema_version", "snapshot_id", "targets"}
        or snapshot.get("schema_version") != 1
        or snapshot.get("snapshot_id") != snapshot_id
        or not isinstance(snapshot.get("targets"), dict)
        or set(snapshot["targets"]) != set(ALL_TARGET_KEYS)
    ):
        _schedule_error("snapshot_invalid", "$.snapshot", "快照字段无效")
    for key, item in snapshot["targets"].items():
        if (
            not isinstance(item, dict)
            or set(item) != {"sha256"}
            or not isinstance(item["sha256"], str)
            or (
                item["sha256"] != MISSING_HASH
                and not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            )
        ):
            _schedule_error("snapshot_invalid", f"$.snapshot.targets.{key}", "快照目标无效")
    return snapshot_id, snapshot_sha, snapshot, tx_dir


def _restore_candidates(
    snapshot: dict[str, object], tx_dir: Path
) -> dict[str, bytes | None]:
    candidates = {}
    for key in ALL_TARGET_KEYS:
        expected = snapshot["targets"][key]["sha256"]
        candidates[key] = _original_bytes(tx_dir, key, expected)
    # Consuming the baseline is part of restore, even if an old pointer predated activation.
    candidates["latest_activation"] = None
    return candidates


def preview_schedule_restore(
    config_dir: Path, snapshot_id: str | None, crontab_raw: bytes
) -> dict[str, object]:
    selected, snapshot_sha, snapshot, tx_dir = _select_snapshot(
        config_dir, snapshot_id
    )
    candidates = _restore_candidates(snapshot, tx_dir)
    filename = ""
    manifest = _load_manifest(config_dir)
    if manifest and isinstance(manifest.get("filename"), str):
        filename = manifest["filename"]
    hashes = _source_hashes(config_dir, filename, crontab_raw)
    hashes["snapshot_manifest"] = snapshot_sha
    current_hashes = {
        key: _hash_optional(_read_target(config_dir, key, crontab_raw))
        for key in candidates
    }
    target_hashes = _candidate_hashes(candidates)
    target_diff = [
        {
            "target": key,
            "before_sha256": current_hashes[key],
            "after_sha256": target_hashes[key],
        }
        for key in candidates
        if current_hashes[key] != target_hashes[key]
    ]
    return {
        "status": "changed" if target_diff else "unchanged",
        "snapshot_id": selected,
        "snapshot_sha256": snapshot_sha,
        "source_hashes": hashes,
        "target_hashes": target_hashes,
        "target_diff": target_diff,
        "task_diff": [
            item for item in target_diff if item["target"].startswith("task.")
        ],
        "weekly_diff": [
            item
            for item in target_diff
            if item["target"]
            in {"schedule_manifest", "weekly_settings", "latest_activation"}
        ],
        "cron_diff": next(
            (
                {
                    "before_sha256": item["before_sha256"],
                    "after_sha256": item["after_sha256"],
                }
                for item in target_diff
                if item["target"] == "crontab"
            ),
            {},
        ),
    }


def commit_schedule_restore(
    config_dir: Path,
    snapshot_id: str,
    expected_hashes: dict,
    expected_snapshot_sha: str,
    read_crontab,
    install_crontab,
) -> dict[str, object]:
    _require_recovered_state(config_dir)
    crontab_raw = read_crontab()
    selected, snapshot_sha, snapshot, tx_dir = _select_snapshot(
        config_dir, snapshot_id
    )
    if snapshot_sha != expected_snapshot_sha:
        _schedule_error("stale_preview", "$.snapshot_sha256", "恢复快照已变化")
    manifest = _load_manifest(config_dir)
    filename = manifest.get("filename", "") if manifest else ""
    current = _source_hashes(config_dir, filename, crontab_raw)
    current["snapshot_manifest"] = snapshot_sha
    _validate_hash_map(
        expected_hashes, (*SOURCE_HASH_KEYS, "snapshot_manifest"), "$.expected_hashes"
    )
    if current != expected_hashes:
        _schedule_error("stale_preview", "$.expected_hashes", "恢复预览来源已变化")
    candidates = _restore_candidates(snapshot, tx_dir)
    if _all_candidates_current(config_dir, candidates, crontab_raw):
        return {"status": "unchanged", "snapshot_id": selected}
    transaction_id = str(uuid.uuid4())
    result = _apply_transaction(
        config_dir,
        transaction_id,
        "schedule_restore",
        candidates,
        current["crontab"],
        read_crontab,
        install_crontab,
    )
    result["snapshot_id"] = selected
    return result


def _load_pending_journal(path: Path) -> tuple[dict[str, object], Path]:
    value, _ = _load_json_strict(path, "snapshot_invalid", "snapshot_invalid")
    if (
        value.get("schema_version") != 1
        or value.get("state") != "prepared"
        or not isinstance(value.get("transaction_id"), str)
        or path.parent.name != value["transaction_id"]
    ):
        _schedule_error("snapshot_invalid", "$.transaction", "待恢复事务 journal 无效")
    _validate_transaction_id(value["transaction_id"])
    _journal_entry_map(value)
    return value, path.parent


def recover_pending_transaction(
    config_dir: Path, read_crontab, install_crontab
) -> dict[str, object]:
    """Roll back exactly one prepared transaction; never guess an order."""
    root = _backup_root(config_dir)
    if not root.exists():
        return {"status": "clean"}
    prepared = _prepared_journal_paths(config_dir)
    if len(prepared) > 1:
        return {
            "status": "blocked",
            "code": "ambiguous_pending_transaction",
            "transactions": sorted(path.parent.name for path in prepared),
        }
    if not prepared:
        return {"status": "clean"}
    journal, tx_dir = _load_pending_journal(prepared[0])
    io = _transaction_io_factory(config_dir, read_crontab, install_crontab)
    return _rollback_journal(config_dir, tx_dir, journal, io, None)
