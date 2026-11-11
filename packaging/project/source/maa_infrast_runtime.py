#!/usr/bin/env python3
"""Fail-closed runtime guard for Stage C scheduled infrastructure tasks."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NoReturn

from maa_infrast_plan import PlanValidationError, parse_plan_document, read_plan_bytes
from maa_infrast_schedule import (
    ScheduleError as CycleScheduleError,
    build_cycle_definition,
    build_cycle_variants,
    resolve_cycle_slot,
)
from maa_task_config import (
    StrictTaskConfigError,
    load_tasks_strict,
    render_tasks,
)


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SHA_RE = re.compile(r"[0-9a-f]{64}")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
ATTEMPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
STATES = {
    "run_started",
    "infrast_started",
    "infrast_completed",
    "infrast_failed",
    "daily_completed",
    "partial_success",
    "unknown",
}
TERMINAL_STATES = {"daily_completed", "partial_success", "unknown"}
TRANSITIONS = {
    "run_started": {
        "infrast_started",
        "daily_completed",
        "unknown",
    },
    "infrast_started": {
        "infrast_completed",
        "infrast_failed",
        "unknown",
    },
    "infrast_completed": {"daily_completed", "unknown"},
    "infrast_failed": {"partial_success", "unknown"},
    "daily_completed": set(),
    "partial_success": set(),
    "unknown": set(),
}

PROTOCOL_KEYS = {
    "RESULT",
    "EXECUTION_KEY",
    "ATTEMPT_ID",
    "EFFECTIVE_TASK",
    "INFRAST_OMITTED",
    "OMIT_REASON",
    "STATE",
    "DOWNSTREAM_COUNTS",
}
RESULT_FIELDS = {
    "not_managed": {"RESULT"},
    "not_due": {"RESULT", "INFRAST_OMITTED", "OMIT_REASON"},
    "stale_schedule": {
        "RESULT",
        "INFRAST_OMITTED",
        "OMIT_REASON",
    },
    "claimed": {
        "RESULT",
        "EXECUTION_KEY",
        "ATTEMPT_ID",
        "INFRAST_OMITTED",
        "OMIT_REASON",
    },
    "duplicate": {"RESULT", "EXECUTION_KEY", "ATTEMPT_ID"},
    "prepared": {
        "RESULT",
        "EFFECTIVE_TASK",
        "INFRAST_OMITTED",
        "OMIT_REASON",
        "DOWNSTREAM_COUNTS",
    },
    "marked": {
        "RESULT",
        "EXECUTION_KEY",
        "ATTEMPT_ID",
        "STATE",
    },
    "status": {
        "RESULT",
        "EXECUTION_KEY",
        "ATTEMPT_ID",
        "STATE",
    },
    "not_found": {"RESULT", "EXECUTION_KEY"},
}
METADATA_KEYS = {
    "manifest_transaction_id",
    "manifest_sha256",
    "filename",
    "json_sha256",
    "observed_json_sha256",
    "plan_index",
    "logical_date",
    "logical_weekday",
    "scheduled_time",
    "task_sha256",
    "slot",
    "preflight_omit_reason",
}


@dataclass
class RuntimeScheduleError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class RuntimePreflightError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class LedgerError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class ProtocolError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class CliValidationError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


class ClaimResult(str):
    """String-compatible claim result carrying the attempt chosen under lock."""

    result: str
    attempt_id: str
    omit_reason: str

    def __new__(cls, result: str, attempt_id: str, omit_reason: str = ""):
        value = super().__new__(cls, result)
        value.result = result
        value.attempt_id = attempt_id
        value.omit_reason = omit_reason
        return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_object(raw: bytes, error_cls, code: str, message: str) -> dict:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise error_cls(code, message) from exc
    if not isinstance(value, dict):
        raise error_cls(code, message)
    return value


def _read_manifest_slot(
    schedule_file: Path,
    logical_weekday: str,
    scheduled_time: str,
    logical_date: date | None = None,
    *,
    boundary_only: bool = True,
) -> dict[str, object]:
    if logical_weekday not in WEEKDAYS:
        raise RuntimeScheduleError("invalid_weekday", "逻辑星期无效")
    if not TIME_RE.fullmatch(scheduled_time):
        raise RuntimeScheduleError("invalid_time", "计划时间无效")
    try:
        raw = Path(schedule_file).read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeScheduleError("schedule_missing", "Stage C 排班清单不存在") from exc
    except OSError as exc:
        raise RuntimeScheduleError("schedule_corrupt", "Stage C 排班清单无法读取") from exc
    manifest = _json_object(
        raw,
        RuntimeScheduleError,
        "schedule_corrupt",
        "Stage C 排班清单损坏",
    )
    common = {
        "schema_version",
        "enabled",
        "filename",
        "json_sha256",
        "mode",
        "days",
        "transaction_id",
        "updated_at",
    }
    schema_version = manifest.get("schema_version")
    required = common if schema_version == 1 else common | {"cycle"}
    if (
        set(manifest) != required
        or schema_version not in {1, 2}
        or type(manifest.get("enabled")) is not bool
        or not isinstance(manifest.get("filename"), str)
        or not manifest["filename"]
        or not isinstance(manifest.get("json_sha256"), str)
        or not SHA_RE.fullmatch(manifest["json_sha256"])
        or manifest.get("mode") not in {"preserve", "replace"}
        or not isinstance(manifest.get("days"), dict)
        or not isinstance(manifest.get("transaction_id"), str)
        or not isinstance(manifest.get("updated_at"), str)
    ):
        raise RuntimeScheduleError("schedule_corrupt", "Stage C 排班清单字段无效")
    try:
        if str(uuid.UUID(manifest["transaction_id"])) != manifest["transaction_id"]:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise RuntimeScheduleError("schedule_corrupt", "Stage C 事务 ID 无效") from exc
    if not manifest["enabled"]:
        raise RuntimeScheduleError("schedule_disabled", "Stage C 排班已停用")
    if schema_version == 2:
        if manifest["days"] != {} or not isinstance(logical_date, date):
            raise RuntimeScheduleError("schedule_corrupt", "跨日排班逻辑日期无效")
        if logical_weekday != WEEKDAYS[logical_date.weekday()]:
            raise RuntimeScheduleError("invalid_weekday", "逻辑日期与星期不一致")
        cycle = manifest.get("cycle")
        try:
            rebuilt = build_cycle_definition(
                cycle.get("durations_hours") if isinstance(cycle, dict) else None,
                cycle.get("names") if isinstance(cycle, dict) else None,
                cycle.get("anchor_date") if isinstance(cycle, dict) else None,
                cycle.get("anchor_time") if isinstance(cycle, dict) else None,
            )
            if rebuilt != cycle:
                raise ValueError
            resolved = resolve_cycle_slot(
                cycle,
                logical_date.isoformat(),
                scheduled_time,
                boundary_only=boundary_only,
            )
        except (CycleScheduleError, ValueError) as exc:
            raise RuntimeScheduleError("schedule_corrupt", "跨日循环定义无效") from exc
        if resolved is None:
            raise RuntimeScheduleError(
                "cycle_not_due", "当前时间不是跨日循环换班边界"
            )
        slot = {
            key: resolved[key]
            for key in ("time", "plan_index", "name", "source_time", "source")
        }
        slot_logical_date = date.fromisoformat(resolved["logical_date"])
    else:
        if set(manifest["days"]) != set(WEEKDAYS):
            raise RuntimeScheduleError("schedule_corrupt", "Stage C 日期班次无效")
        slots = manifest["days"][logical_weekday]
        if not isinstance(slots, list):
            raise RuntimeScheduleError("schedule_corrupt", "Stage C 日期班次无效")
        valid_slots = [
            slot
            for slot in slots
            if isinstance(slot, dict)
            and TIME_RE.fullmatch(str(slot.get("time", "")))
        ]
        if boundary_only:
            matches = [
                slot for slot in valid_slots if slot.get("time") == scheduled_time
            ]
            if len(matches) != 1:
                raise RuntimeScheduleError("slot_missing", "计划时间没有唯一班次")
            slot = matches[0]
            slot_logical_date = logical_date
        else:
            if not isinstance(logical_date, date) or not valid_slots:
                raise RuntimeScheduleError("slot_missing", "无法确定当前班次")
            current_minute = _minute(scheduled_time)
            ordered = sorted(valid_slots, key=lambda item: _minute(item["time"]))
            eligible = [
                item for item in ordered if _minute(item["time"]) <= current_minute
            ]
            slot = eligible[-1] if eligible else ordered[-1]
            slot_logical_date = (
                logical_date
                if _minute(slot["time"]) <= current_minute
                else logical_date - timedelta(days=1)
            )
    required_slot = {"time", "plan_index", "name", "source_time", "source"}
    if (
        set(slot) != required_slot
        or not TIME_RE.fullmatch(str(slot.get("time", "")))
        or not TIME_RE.fullmatch(str(slot.get("source_time", "")))
        or type(slot.get("plan_index")) is not int
        or slot["plan_index"] < 0
        or not isinstance(slot.get("name"), str)
        or not slot["name"]
        or slot.get("source") not in {"period", "inferred", "manual", "cycle"}
    ):
        raise RuntimeScheduleError("schedule_corrupt", "Stage C 班次字段无效")
    return {
        "manifest": manifest,
        "manifest_raw": raw,
        "manifest_sha256": _sha(raw),
        "logical_weekday": logical_weekday,
        "scheduled_time": scheduled_time,
        "slot_logical_date": slot_logical_date,
        "slot": copy.deepcopy(slot),
    }


def _minute(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2})(?::(\d{2}))?", value)
    if not match:
        raise ValueError
    hour, minute, second = (int(item or 0) for item in match.groups())
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError
    return hour * 60 + minute


def _time_condition_matches(condition: object, scheduled_time: str) -> bool:
    if not isinstance(condition, dict) or condition.get("type") != "Time":
        return False
    if set(condition) == {"type"}:
        return True
    if set(condition) != {"type", "start", "end"}:
        return False
    try:
        value = _minute(scheduled_time)
        start = _minute(condition["start"])
        end = _minute(condition["end"])
    except (KeyError, TypeError, ValueError):
        return False
    if start == end:
        return False
    if start < end:
        return start <= value < end
    return value >= start or value < end


def _validate_task_binding(
    tasks: list[dict[str, object]],
    filename: str,
    slot: dict[str, object],
    scheduled_time: str,
    cycle: dict[str, object] | None = None,
) -> None:
    active = [
        task
        for task in tasks
        if task.get("type") == "Infrast" and task.get("_enabled") is not False
    ]
    if len(active) != 1:
        raise RuntimePreflightError(
            "ambiguous_infrast",
            "任务 TOML 必须恰好包含一个启用的 Infrast",
        )
    task = active[0]
    params = task.get("params")
    if (
        not isinstance(params, dict)
        or params.get("mode") != 10000
        or params.get("filename") != filename
        or task.get("strategy", "first") != "first"
    ):
        raise RuntimePreflightError("infrast_binding_mismatch", "Infrast 参数与清单不一致")
    variants = task.get("variants")
    if not isinstance(variants, list):
        raise RuntimePreflightError("variant_mismatch", "Infrast Time variant 缺失")
    if cycle is not None:
        try:
            expected = build_cycle_variants(cycle)
        except CycleScheduleError as exc:
            raise RuntimePreflightError(
                "variant_mismatch", "Infrast DayMod variant 无效"
            ) from exc
        if variants != expected:
            raise RuntimePreflightError(
                "variant_mismatch", "Infrast DayMod variant 与清单不一致"
            )
        return
    matches = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_params = variant.get("params")
        if (
            _time_condition_matches(variant.get("condition"), scheduled_time)
            and isinstance(variant_params, dict)
        ):
            matches.append(variant_params.get("plan_index"))
    if matches != [slot["plan_index"]]:
        raise RuntimePreflightError("variant_mismatch", "Infrast Time variant 与清单不一致")


def _read_and_validate_plan(
    infrast_dir: Path,
    filename: str,
    expected_sha: str,
    plan_index: int,
) -> str:
    try:
        raw = read_plan_bytes(Path(infrast_dir), filename)
    except PlanValidationError as exc:
        raise RuntimePreflightError(exc.code, str(exc)) from exc
    actual_sha = _sha(raw)
    if actual_sha != expected_sha:
        raise RuntimePreflightError("json_sha_mismatch", "基建 JSON SHA 已变化")
    try:
        document = parse_plan_document(raw, filename)
    except PlanValidationError as exc:
        raise RuntimePreflightError(exc.code, str(exc)) from exc
    plans = document.get("plans")
    if not isinstance(plans, list) or not 0 <= plan_index < len(plans):
        raise RuntimePreflightError("plan_index_out_of_range", "plan_index 超出 JSON 范围")
    return actual_sha


def _require_nonempty_tasks(path: Path) -> tuple[list[dict[str, object]], bytes]:
    tasks = load_tasks_strict(path)
    if not tasks:
        raise StrictTaskConfigError("unexpected_empty", "任务 TOML 没有任务")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise StrictTaskConfigError("unreadable", "任务 TOML 无法重新读取") from exc
    return tasks, raw


def resolve_slot(
    task_file: Path,
    schedule_file: Path,
    infrast_dir: Path,
    logical_weekday: str,
    scheduled_time: str,
    *,
    logical_date: date | None = None,
) -> dict:
    """Resolve and fully validate one explicit scheduled slot."""
    selected = _read_manifest_slot(
        Path(schedule_file),
        logical_weekday,
        scheduled_time,
        logical_date,
    )
    manifest = selected["manifest"]
    tasks, task_raw = _require_nonempty_tasks(Path(task_file))
    _validate_task_binding(
        tasks,
        manifest["filename"],
        selected["slot"],
        scheduled_time,
        manifest.get("cycle"),
    )
    observed_sha = _read_and_validate_plan(
        Path(infrast_dir),
        manifest["filename"],
        manifest["json_sha256"],
        selected["slot"]["plan_index"],
    )
    return {
        "manifest_transaction_id": manifest["transaction_id"],
        "manifest_sha256": selected["manifest_sha256"],
        "filename": manifest["filename"],
        "json_sha256": manifest["json_sha256"],
        "observed_json_sha256": observed_sha,
        "plan_index": selected["slot"]["plan_index"],
        "logical_weekday": logical_weekday,
        "scheduled_time": scheduled_time,
        "task_sha256": _sha(task_raw),
        "slot": selected["slot"],
        "preflight_omit_reason": "",
    }


def execution_key(
    logical_date: date,
    scheduled_time: str,
    filename: str,
    sha256: str,
    plan_index: int,
) -> str:
    if not isinstance(logical_date, date):
        raise ValueError("logical_date must be a date")
    if not TIME_RE.fullmatch(scheduled_time):
        raise ValueError("invalid scheduled_time")
    if not isinstance(filename, str) or not filename or "|" in filename:
        raise ValueError("invalid filename")
    if not isinstance(sha256, str) or not SHA_RE.fullmatch(sha256):
        raise ValueError("invalid sha256")
    if type(plan_index) is not int or plan_index < 0:
        raise ValueError("invalid plan_index")
    return (
        f"{logical_date.isoformat()}T{scheduled_time}|{filename}|"
        f"{sha256}|{plan_index}"
    )


def _history_started(attempt: dict) -> bool:
    history = attempt.get("history", [])
    states = {
        item.get("state")
        for item in history
        if isinstance(item, dict)
    }
    return bool(
        states
        & {
            "infrast_started",
            "infrast_completed",
            "infrast_failed",
        }
        or (
            attempt.get("state") == "unknown"
            and "infrast_started" in states
        )
    )


def _ledger_started(ledger_state: dict | None) -> bool:
    if not ledger_state:
        return False
    attempts = ledger_state.get("attempts")
    if isinstance(attempts, list):
        return any(isinstance(item, dict) and _history_started(item) for item in attempts)
    if ledger_state.get("state") in {
        "infrast_started",
        "infrast_completed",
        "infrast_failed",
    }:
        return True
    return _history_started(ledger_state)


def _downstream_counts(
    tasks: list[dict[str, object]], slot: dict | None
) -> dict[str, int]:
    del slot
    start = 0
    for index, task in enumerate(tasks):
        if task.get("type") == "Infrast" and task.get("_enabled") is not False:
            start = index + 1
            break
    counts: dict[str, int] = {}
    for task in tasks[start:]:
        task_type = task.get("type")
        if (
            task.get("_enabled") is not False
            and isinstance(task_type, str)
            and task_type not in {"StartUp", "Infrast"}
        ):
            counts[task_type] = counts.get(task_type, 0) + 1
    return dict(sorted(counts.items()))


def prepare_effective_tasks(
    tasks: list[dict],
    slot: dict | None,
    ledger_state: dict | None,
) -> tuple[list[dict], dict]:
    """Build an in-memory task list, omitting only active Infrast when unsafe."""
    effective = copy.deepcopy(tasks)
    reason = ""
    if slot is None:
        reason = "slot_unavailable"
    elif isinstance(ledger_state, dict) and ledger_state.get("preflight_omit_reason"):
        reason = str(ledger_state["preflight_omit_reason"])
    elif _ledger_started(ledger_state):
        reason = "infrast_already_started"
    omitted = bool(reason)
    if omitted:
        effective = [
            task
            for task in effective
            if not (
                task.get("type") == "Infrast"
                and task.get("_enabled") is not False
            )
        ]
    return effective, {
        "infrast_omitted": omitted,
        "omit_reason": reason,
        "downstream_counts": _downstream_counts(tasks, slot),
    }


def canonical_downstream_counts(value: object) -> str:
    if (
        not isinstance(value, dict)
        or any(
            not isinstance(key, str)
            or not key
            or type(count) is not int
            or count < 0
            for key, count in value.items()
        )
    ):
        raise ProtocolError("invalid_downstream_counts", "下游计数无效")
    return json.dumps(dict(sorted(value.items())), ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _default_lock(path: Path):
    """Cross-process sibling advisory lock (flock on POSIX, msvcrt on Windows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp.write(raw)
            temp.flush()
            os.fsync(temp.fileno())
            temp_name = temp.name
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _corrupt(message: str) -> NoReturn:
    raise LedgerError("corrupt_ledger", message)


def _ledger_timestamp(value: object, message: str) -> datetime:
    if not isinstance(value, str):
        _corrupt(message)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _corrupt(message)
    if parsed.tzinfo is None:
        _corrupt(message)
    return parsed.astimezone(timezone.utc)


def _safe_ledger_text(value: object, message: str) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        _corrupt(message)
    return value


def _validate_metadata(key: str, metadata: object, *, corrupt: bool) -> None:
    def invalid(message: str) -> NoReturn:
        if corrupt:
            _corrupt(message)
        raise LedgerError("invalid_payload", message)

    if not isinstance(metadata, dict) or set(metadata) != METADATA_KEYS:
        invalid("冻结元数据字段无效")
    try:
        transaction_id = metadata["manifest_transaction_id"]
        if (
            not isinstance(transaction_id, str)
            or str(uuid.UUID(transaction_id)) != transaction_id
        ):
            invalid("冻结事务 ID 无效")
    except (ValueError, AttributeError):
        invalid("冻结事务 ID 无效")
    for field in ("manifest_sha256", "json_sha256", "task_sha256"):
        value = metadata[field]
        if not isinstance(value, str) or not SHA_RE.fullmatch(value):
            invalid(f"冻结字段 {field} 无效")
    observed = metadata["observed_json_sha256"]
    if observed != "" and (
        not isinstance(observed, str) or not SHA_RE.fullmatch(observed)
    ):
        invalid("冻结 observed_json_sha256 无效")
    filename = metadata["filename"]
    if (
        not isinstance(filename, str)
        or not filename
        or "|" in filename
        or "\x00" in filename
        or "\r" in filename
        or "\n" in filename
    ):
        invalid("冻结 filename 无效")
    if metadata["logical_weekday"] not in WEEKDAYS:
        invalid("冻结 logical_weekday 无效")
    if (
        not isinstance(metadata["scheduled_time"], str)
        or not TIME_RE.fullmatch(metadata["scheduled_time"])
    ):
        invalid("冻结 scheduled_time 无效")
    try:
        logical_date = date.fromisoformat(metadata["logical_date"])
    except (TypeError, ValueError):
        invalid("冻结 logical_date 无效")
    plan_index = metadata["plan_index"]
    if type(plan_index) is not int or plan_index < 0:
        invalid("冻结 plan_index 无效")
    slot = metadata["slot"]
    if (
        not isinstance(slot, dict)
        or set(slot) != {"time", "plan_index", "name", "source_time", "source"}
        or slot.get("time") != metadata["scheduled_time"]
        or slot.get("plan_index") != plan_index
        or not isinstance(slot.get("name"), str)
        or not slot["name"]
        or not isinstance(slot.get("source_time"), str)
        or not TIME_RE.fullmatch(slot["source_time"])
        or slot.get("source") not in {"period", "inferred", "manual", "cycle"}
    ):
        invalid("冻结 slot 无效")
    reason = metadata["preflight_omit_reason"]
    if not isinstance(reason, str) or any(char in reason for char in "\x00\r\n"):
        invalid("冻结 preflight_omit_reason 无效")
    try:
        expected_key = execution_key(
            logical_date,
            metadata["scheduled_time"],
            filename,
            metadata["json_sha256"],
            plan_index,
        )
    except ValueError:
        invalid("冻结 execution key 字段无效")
    if expected_key != key:
        invalid("冻结元数据与 execution key 不一致")


def _validate_attempt(attempt: object) -> tuple[str, datetime, datetime]:
    if (
        not isinstance(attempt, dict)
        or set(attempt)
        != {
            "id",
            "state",
            "created_at",
            "updated_at",
            "detail",
            "history",
        }
    ):
        _corrupt("运行尝试字段无效")
    attempt_id = attempt["id"]
    if not isinstance(attempt_id, str) or not ATTEMPT_RE.fullmatch(attempt_id):
        _corrupt("运行尝试 ID 无效")
    state = attempt["state"]
    if state not in STATES:
        _corrupt("运行尝试状态无效")
    created = _ledger_timestamp(attempt["created_at"], "运行尝试创建时间无效")
    updated = _ledger_timestamp(attempt["updated_at"], "运行尝试更新时间无效")
    detail = _safe_ledger_text(attempt["detail"], "运行尝试详情无效")
    history = attempt["history"]
    if not isinstance(history, list) or not history:
        _corrupt("运行尝试历史无效")
    previous = None
    previous_time = None
    for item in history:
        if (
            not isinstance(item, dict)
            or set(item) != {"state", "timestamp", "detail"}
            or item.get("state") not in STATES
        ):
            _corrupt("运行尝试历史项无效")
        item_time = _ledger_timestamp(item["timestamp"], "运行尝试历史时间无效")
        _safe_ledger_text(item["detail"], "运行尝试历史详情无效")
        if previous is None:
            if item["state"] != "run_started" or item_time != created:
                _corrupt("运行尝试历史起点无效")
        else:
            if (
                item["state"] == previous
                or item["state"] not in TRANSITIONS.get(previous, set())
                or item_time < previous_time
            ):
                _corrupt("运行尝试历史迁移无效")
        previous = item["state"]
        previous_time = item_time
    if (
        previous != state
        or previous_time != updated
        or history[-1]["detail"] != detail
        or updated < created
    ):
        _corrupt("运行尝试当前状态与历史不一致")
    return attempt_id, created, updated


def _validate_entry(key: str, entry: object) -> None:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"metadata", "created_at", "updated_at", "attempts"}
    ):
        _corrupt("运行记录字段无效")
    _validate_metadata(key, entry["metadata"], corrupt=True)
    created = _ledger_timestamp(entry["created_at"], "运行记录创建时间无效")
    updated = _ledger_timestamp(entry["updated_at"], "运行记录更新时间无效")
    attempts = entry["attempts"]
    if not isinstance(attempts, list) or not attempts:
        _corrupt("运行尝试记录无效")
    ids = set()
    attempt_times = []
    for attempt in attempts:
        attempt_id, attempt_created, attempt_updated = _validate_attempt(attempt)
        if attempt_id in ids:
            _corrupt("运行尝试 ID 重复")
        ids.add(attempt_id)
        attempt_times.append((attempt_created, attempt_updated))
    if created != attempt_times[0][0] or updated != max(
        attempt_updated for _, attempt_updated in attempt_times
    ):
        _corrupt("运行记录时间与尝试不一致")


def _validate_ledger(data: dict) -> None:
    for key, entry in data["executions"].items():
        if not isinstance(key, str) or not key:
            _corrupt("execution key 无效")
        _validate_entry(key, entry)


class ScheduleLedger:
    """Versioned, locked and atomically persisted execution-attempt ledger."""

    def __init__(
        self,
        path: Path,
        keep_days: int = 14,
        *,
        lock_factory: Callable = _default_lock,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        if type(keep_days) is not int or keep_days < 0:
            raise ValueError("keep_days must be a nonnegative integer")
        self.keep_days = keep_days
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_factory = lock_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _timestamp(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def _load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"schema_version": 1, "executions": {}}
        if not raw.strip():
            raise LedgerError("corrupt_ledger", "运行账本为空")
        data = _json_object(
            raw,
            LedgerError,
            "corrupt_ledger",
            "运行账本损坏",
        )
        if (
            set(data) != {"schema_version", "executions"}
            or data.get("schema_version") != 1
            or not isinstance(data.get("executions"), dict)
        ):
            raise LedgerError("corrupt_ledger", "运行账本结构无效")
        _validate_ledger(data)
        return data

    def _save(self, data: dict) -> None:
        raw = (
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        _atomic_write_bytes(self.path, raw)

    def _prune(self, data: dict, now: datetime) -> bool:
        threshold = now.astimezone(timezone.utc) - timedelta(days=self.keep_days)
        changed = False
        for key, entry in list(data["executions"].items()):
            if not isinstance(entry, dict):
                raise LedgerError("corrupt_ledger", "运行记录无效")
            attempts = entry.get("attempts")
            if not isinstance(attempts, list):
                raise LedgerError("corrupt_ledger", "运行尝试记录无效")
            if not attempts or any(
                not isinstance(attempt, dict)
                or attempt.get("state") not in TERMINAL_STATES
                for attempt in attempts
            ):
                continue
            try:
                updated = datetime.fromisoformat(entry["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError) as exc:
                raise LedgerError("corrupt_ledger", "运行记录时间无效") from exc
            if updated.astimezone(timezone.utc) < threshold:
                del data["executions"][key]
                changed = True
        return changed

    def claim(
        self,
        key: str,
        payload: dict | Callable[[], dict],
        retry_attempt: str | None = None,
    ) -> ClaimResult:
        if not isinstance(key, str) or not key:
            raise LedgerError("invalid_key", "execution key 无效")
        if retry_attempt is not None and not ATTEMPT_RE.fullmatch(retry_attempt):
            raise LedgerError("invalid_attempt_id", "重试 attempt ID 无效")
        with self.lock_factory(self.lock_path):
            data = self._load()
            now = self.clock()
            self._prune(data, now)
            timestamp = self._timestamp()
            entry = data["executions"].get(key)
            if entry is None:
                if retry_attempt is not None:
                    raise LedgerError(
                        "retry_without_execution",
                        "显式重试不能创建不存在的 execution",
                    )
                resolved_payload = payload() if callable(payload) else payload
                _validate_metadata(key, resolved_payload, corrupt=False)
                attempt_id = "attempt-1"
                entry = {
                    "metadata": copy.deepcopy(resolved_payload),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "attempts": [],
                }
                data["executions"][key] = entry
            else:
                attempts = entry.get("attempts")
                if not isinstance(attempts, list):
                    raise LedgerError("corrupt_ledger", "运行尝试记录无效")
                if retry_attempt is None:
                    return ClaimResult("duplicate", attempts[-1]["id"])
                resolved_payload = payload() if callable(payload) else payload
                _validate_metadata(key, resolved_payload, corrupt=False)
                if entry.get("metadata") != resolved_payload:
                    raise LedgerError("binding_mismatch", "execution key 的冻结绑定不一致")
                attempt_id = retry_attempt
                if any(
                    isinstance(attempt, dict) and attempt.get("id") == attempt_id
                    for attempt in attempts
                ):
                    return ClaimResult("duplicate", attempt_id)
            history = [{"state": "run_started", "timestamp": timestamp, "detail": ""}]
            entry["attempts"].append(
                {
                    "id": attempt_id,
                    "state": "run_started",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "detail": "",
                    "history": history,
                }
            )
            entry["updated_at"] = timestamp
            self._save(data)
            return ClaimResult(
                "claimed",
                attempt_id,
                str(resolved_payload.get("preflight_omit_reason") or ""),
            )

    def mark(
        self,
        key: str,
        attempt_id: str,
        state: str,
        detail: str = "",
    ) -> str:
        if state not in STATES:
            raise LedgerError("invalid_state", "运行状态无效")
        if not ATTEMPT_RE.fullmatch(attempt_id or ""):
            raise LedgerError("invalid_attempt_id", "attempt ID 无效")
        if not isinstance(detail, str) or "\x00" in detail:
            raise LedgerError("invalid_detail", "状态详情无效")
        with self.lock_factory(self.lock_path):
            data = self._load()
            entry = data["executions"].get(key)
            if not isinstance(entry, dict):
                raise LedgerError("execution_not_found", "execution key 不存在")
            attempts = entry.get("attempts")
            if not isinstance(attempts, list):
                raise LedgerError("corrupt_ledger", "运行尝试记录无效")
            attempt = next(
                (
                    item
                    for item in attempts
                    if isinstance(item, dict) and item.get("id") == attempt_id
                ),
                None,
            )
            if attempt is None:
                raise LedgerError("attempt_not_found", "attempt ID 不存在")
            current = attempt.get("state")
            if current == state:
                return "idempotent"
            if state not in TRANSITIONS.get(current, set()):
                raise LedgerError(
                    "invalid_transition",
                    f"不允许从 {current} 迁移到 {state}",
                )
            timestamp = self._timestamp()
            attempt["state"] = state
            attempt["updated_at"] = timestamp
            attempt["detail"] = detail
            history = attempt.get("history")
            if not isinstance(history, list):
                raise LedgerError("corrupt_ledger", "状态历史无效")
            history.append(
                {"state": state, "timestamp": timestamp, "detail": detail}
            )
            entry["updated_at"] = timestamp
            self._save(data)
            return "marked"

    def status(self, key: str) -> dict | None:
        with self.lock_factory(self.lock_path):
            data = self._load()
            entry = data["executions"].get(key)
            return copy.deepcopy(entry) if entry is not None else None


def encode_protocol(fields: dict[str, object]) -> str:
    if not isinstance(fields, dict) or set(fields) - PROTOCOL_KEYS:
        raise ProtocolError("unknown_key", "协议包含未知字段")
    lines = []
    for key, value in fields.items():
        raw = str(value).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        lines.append(f"{key}={encoded}")
    return "\n".join(lines) + ("\n" if lines else "")


def _decode_base64url(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", value) or len(value) % 4 == 1:
        raise ProtocolError("invalid_base64url", "协议字段不是规范 base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value:
            raise ValueError
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("invalid_base64url", "协议字段无法解码") from exc


def decode_protocol(text: str) -> dict[str, str]:
    if not isinstance(text, str):
        raise ProtocolError("malformed_line", "协议输出必须是文本")
    result = {}
    for line in text.splitlines():
        if line.count("=") != 1:
            raise ProtocolError("malformed_line", "协议行格式无效")
        key, encoded = line.split("=", 1)
        if key not in PROTOCOL_KEYS:
            raise ProtocolError("unknown_key", "协议包含未知字段")
        if key in result:
            raise ProtocolError("duplicate_key", "协议字段重复")
        decoded = _decode_base64url(encoded)
        if key != "DOWNSTREAM_COUNTS" and any(
            value in decoded for value in ("\x00", "\r", "\n")
        ):
            raise ProtocolError("unsafe_value", "协议标量包含不安全字符")
        result[key] = decoded
    result_name = result.get("RESULT")
    required = RESULT_FIELDS.get(result_name)
    if required is None or set(result) != required:
        raise ProtocolError("invalid_result_fields", "RESULT 字段集合无效")
    if "INFRAST_OMITTED" in result and result["INFRAST_OMITTED"] not in {"0", "1"}:
        raise ProtocolError("invalid_omitted", "INFRAST_OMITTED 无效")
    if "DOWNSTREAM_COUNTS" in result:
        try:
            counts = json.loads(result["DOWNSTREAM_COUNTS"])
        except json.JSONDecodeError as exc:
            raise ProtocolError("invalid_downstream_counts", "下游计数 JSON 无效") from exc
        if canonical_downstream_counts(counts) != result["DOWNSTREAM_COUNTS"]:
            raise ProtocolError("invalid_downstream_counts", "下游计数不是规范 JSON")
    return result


def _claim_payload_from_selected(
    selected: dict[str, object],
    task_file: Path,
    infrast_dir: Path,
    logical_date: date,
    logical_weekday: str,
    scheduled_time: str,
) -> dict:
    manifest = selected["manifest"]
    tasks, task_raw = _require_nonempty_tasks(task_file)
    omit_reason = ""
    observed_sha = ""
    try:
        _validate_task_binding(
            tasks,
            manifest["filename"],
            selected["slot"],
            scheduled_time,
            manifest.get("cycle"),
        )
        observed_sha = _read_and_validate_plan(
            infrast_dir,
            manifest["filename"],
            manifest["json_sha256"],
            selected["slot"]["plan_index"],
        )
    except RuntimePreflightError as exc:
        omit_reason = exc.code
        try:
            observed_sha = _sha(
                read_plan_bytes(infrast_dir, manifest["filename"])
            )
        except (PlanValidationError, OSError):
            observed_sha = ""
    payload = {
        "manifest_transaction_id": manifest["transaction_id"],
        "manifest_sha256": selected["manifest_sha256"],
        "filename": manifest["filename"],
        "json_sha256": manifest["json_sha256"],
        "observed_json_sha256": observed_sha,
        "plan_index": selected["slot"]["plan_index"],
        "logical_date": logical_date.isoformat(),
        "logical_weekday": logical_weekday,
        "scheduled_time": scheduled_time,
        "task_sha256": _sha(task_raw),
        "slot": selected["slot"],
        "preflight_omit_reason": omit_reason,
    }
    return payload


def _claim_payload(
    task_file: Path,
    schedule_file: Path,
    infrast_dir: Path,
    logical_date: date,
    logical_weekday: str,
    scheduled_time: str,
) -> tuple[str, dict]:
    selected = _read_manifest_slot(
        schedule_file,
        logical_weekday,
        scheduled_time,
        logical_date,
    )
    manifest = selected["manifest"]
    key = execution_key(
        logical_date,
        scheduled_time,
        manifest["filename"],
        manifest["json_sha256"],
        selected["slot"]["plan_index"],
    )
    return key, _claim_payload_from_selected(
        selected,
        task_file,
        infrast_dir,
        logical_date,
        logical_weekday,
        scheduled_time,
    )


def _attempt_for(entry: dict, attempt_id: str) -> dict:
    attempts = entry.get("attempts")
    if not isinstance(attempts, list):
        raise LedgerError("corrupt_ledger", "运行尝试记录无效")
    attempt = next(
        (
            item
            for item in attempts
            if isinstance(item, dict) and item.get("id") == attempt_id
        ),
        None,
    )
    if attempt is None:
        raise LedgerError("attempt_not_found", "attempt ID 不存在")
    return attempt


def _revalidate_frozen(
    task_file: Path,
    schedule_file: Path,
    infrast_dir: Path,
    metadata: dict,
) -> tuple[list[dict], str]:
    tasks, task_raw = _require_nonempty_tasks(task_file)
    if _sha(task_raw) != metadata.get("task_sha256"):
        raise LedgerError("frozen_task_changed", "冻结的任务 TOML 已变化")
    try:
        manifest_raw = schedule_file.read_bytes()
    except OSError as exc:
        raise LedgerError("frozen_manifest_changed", "冻结的 Stage C 清单不可用") from exc
    if _sha(manifest_raw) != metadata.get("manifest_sha256"):
        raise LedgerError("frozen_manifest_changed", "冻结的 Stage C 清单已变化")
    try:
        frozen_date = date.fromisoformat(metadata.get("logical_date", ""))
    except (TypeError, ValueError) as exc:
        raise LedgerError("frozen_binding_changed", "冻结的逻辑日期无效") from exc
    selected = _read_manifest_slot(
        schedule_file,
        metadata.get("logical_weekday", ""),
        metadata.get("scheduled_time", ""),
        frozen_date,
    )
    manifest = selected["manifest"]
    if (
        manifest.get("transaction_id") != metadata.get("manifest_transaction_id")
        or manifest.get("filename") != metadata.get("filename")
        or manifest.get("json_sha256") != metadata.get("json_sha256")
        or selected["slot"] != metadata.get("slot")
        or selected["slot"].get("plan_index") != metadata.get("plan_index")
    ):
        raise LedgerError("frozen_binding_changed", "冻结的班次绑定已变化")
    reason = str(metadata.get("preflight_omit_reason") or "")
    if not reason:
        try:
            _validate_task_binding(
                tasks,
                metadata["filename"],
                metadata["slot"],
                metadata["scheduled_time"],
                manifest.get("cycle"),
            )
            observed = _read_and_validate_plan(
                infrast_dir,
                metadata["filename"],
                metadata["json_sha256"],
                metadata["plan_index"],
            )
            if observed != metadata.get("observed_json_sha256"):
                reason = "json_changed_after_claim"
        except RuntimePreflightError as exc:
            reason = exc.code
    return tasks, reason


def _latest_attempt(entry: dict) -> dict:
    attempts = entry.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise LedgerError("corrupt_ledger", "运行尝试记录无效")
    attempt = attempts[-1]
    if not isinstance(attempt, dict):
        raise LedgerError("corrupt_ledger", "运行尝试记录无效")
    return attempt


def _emit(fields: dict[str, object]) -> None:
    sys.stdout.write(encode_protocol(fields))


def _command_claim(args) -> int:
    current = getattr(args, "current", False)
    try:
        logical_date = date.fromisoformat(args.logical_date)
    except ValueError as exc:
        raise CliValidationError("logical-date 必须是 YYYY-MM-DD") from exc
    if args.weekday not in WEEKDAYS or not TIME_RE.fullmatch(args.scheduled_time):
        raise CliValidationError("weekday 或 scheduled-time 无效")
    if args.retry_attempt is not None and not ATTEMPT_RE.fullmatch(args.retry_attempt):
        raise CliValidationError("retry-attempt 无效")
    try:
        selected = _read_manifest_slot(
            Path(args.schedule_file),
            args.weekday,
            args.scheduled_time,
            logical_date,
            boundary_only=not current,
        )
    except RuntimeScheduleError as exc:
        if exc.code == "cycle_not_due":
            _emit(
                {
                    "RESULT": "not_due",
                    "INFRAST_OMITTED": "1",
                    "OMIT_REASON": "not_cycle_boundary",
                }
            )
            return 0
        _emit(
            {
                "RESULT": "stale_schedule",
                "INFRAST_OMITTED": "1",
                "OMIT_REASON": exc.code,
            }
        )
        return 0
    manifest = selected["manifest"]
    effective_date = (
        selected.get("slot_logical_date")
        if current
        else logical_date
    )
    if not isinstance(effective_date, date):
        effective_date = logical_date
    effective_time = (
        selected["slot"]["time"] if current else args.scheduled_time
    )
    effective_weekday = WEEKDAYS[effective_date.weekday()]
    key = execution_key(
        effective_date,
        effective_time,
        manifest["filename"],
        manifest["json_sha256"],
        selected["slot"]["plan_index"],
    )

    def payload_factory():
        return _claim_payload_from_selected(
            selected,
            Path(args.task_file),
            Path(args.infrast_dir),
            effective_date,
            effective_weekday,
            effective_time,
        )

    ledger = ScheduleLedger(Path(args.ledger))
    outcome = ledger.claim(key, payload_factory, args.retry_attempt)
    attempt_id = outcome.attempt_id
    _emit(
        (
            {
                "RESULT": "duplicate",
                "EXECUTION_KEY": key,
                "ATTEMPT_ID": attempt_id,
            }
            if outcome == "duplicate"
            else {
                "RESULT": "claimed",
                "EXECUTION_KEY": key,
                "ATTEMPT_ID": attempt_id,
                "INFRAST_OMITTED": "1" if outcome.omit_reason else "0",
                "OMIT_REASON": outcome.omit_reason,
            }
        )
    )
    return 0


def _command_prepare(args) -> int:
    ledger = ScheduleLedger(Path(args.ledger))
    entry = ledger.status(args.execution_key)
    if entry is None:
        raise LedgerError("execution_not_found", "execution key 不存在")
    attempt = _attempt_for(entry, args.attempt_id)
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        raise LedgerError("corrupt_ledger", "冻结元数据无效")
    tasks, preflight_reason = _revalidate_frozen(
        Path(args.task_file),
        Path(args.schedule_file),
        Path(args.infrast_dir),
        metadata,
    )
    effective, meta = prepare_effective_tasks(
        tasks,
        metadata.get("slot"),
        {
            "attempts": entry.get("attempts"),
            "preflight_omit_reason": preflight_reason,
        },
    )
    rendered = render_tasks(effective).encode("utf-8")
    # Validate the generated bytes without ever touching the stored weekday TOML.
    output = Path(args.output)
    _atomic_write_bytes(output, rendered)
    _emit(
        {
            "RESULT": "prepared",
            "EFFECTIVE_TASK": str(output),
            "INFRAST_OMITTED": "1" if meta["infrast_omitted"] else "0",
            "OMIT_REASON": meta["omit_reason"],
            "DOWNSTREAM_COUNTS": canonical_downstream_counts(
                meta["downstream_counts"]
            ),
        }
    )
    del attempt
    return 0


def _takeover_reason(schedule_file: Path) -> str:
    try:
        raw = Path(schedule_file).read_bytes()
    except FileNotFoundError:
        return ""
    except OSError:
        return "schedule_invalid"
    try:
        manifest = _json_object(
            raw,
            RuntimeScheduleError,
            "schedule_corrupt",
            "Stage C 排班清单损坏",
        )
    except RuntimeScheduleError:
        return "schedule_invalid"
    if manifest.get("enabled") is False:
        return ""
    schema = manifest.get("schema_version")
    required = {
        "schema_version",
        "enabled",
        "filename",
        "json_sha256",
        "mode",
        "days",
        "transaction_id",
        "updated_at",
    }
    if schema == 2:
        required.add("cycle")
    if (
        set(manifest) != required
        or schema not in {1, 2}
        or manifest.get("enabled") is not True
        or not isinstance(manifest.get("filename"), str)
        or not manifest["filename"]
        or not isinstance(manifest.get("json_sha256"), str)
        or not SHA_RE.fullmatch(manifest["json_sha256"])
        or manifest.get("mode") not in {"preserve", "replace"}
        or not isinstance(manifest.get("days"), dict)
    ):
        return "schedule_invalid"
    return "managed_schedule_active"


def _command_takeover(args) -> int:
    reason = _takeover_reason(Path(args.schedule_file))
    if not reason:
        _emit({"RESULT": "not_managed"})
        return 0
    tasks, _raw = _require_nonempty_tasks(Path(args.task_file))
    effective = [
        task
        for task in tasks
        if not (
            task.get("type") == "Infrast"
            and task.get("_enabled") is not False
        )
    ]
    output = Path(args.output)
    _atomic_write_bytes(output, render_tasks(effective).encode("utf-8"))
    _emit(
        {
            "RESULT": "prepared",
            "EFFECTIVE_TASK": str(output),
            "INFRAST_OMITTED": "1",
            "OMIT_REASON": reason,
            "DOWNSTREAM_COUNTS": canonical_downstream_counts(
                _downstream_counts(tasks, None)
            ),
        }
    )
    return 0


def _command_mark(args) -> int:
    outcome = ScheduleLedger(Path(args.ledger)).mark(
        args.execution_key,
        args.attempt_id,
        args.state,
        args.detail or "",
    )
    del outcome
    _emit(
        {
            "RESULT": "marked",
            "EXECUTION_KEY": args.execution_key,
            "ATTEMPT_ID": args.attempt_id,
            "STATE": args.state,
        }
    )
    return 0


def _command_status(args) -> int:
    entry = ScheduleLedger(Path(args.ledger)).status(args.execution_key)
    if entry is None:
        _emit({"RESULT": "not_found", "EXECUTION_KEY": args.execution_key})
        return 0
    attempt = _latest_attempt(entry)
    _emit(
        {
            "RESULT": "status",
            "EXECUTION_KEY": args.execution_key,
            "ATTEMPT_ID": attempt["id"],
            "STATE": attempt["state"],
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim = subparsers.add_parser("claim")
    for name in ("task-file", "schedule-file", "infrast-dir", "ledger"):
        claim.add_argument(f"--{name}", required=True)
    claim.add_argument("--logical-date", required=True)
    claim.add_argument("--weekday", required=True)
    claim.add_argument("--scheduled-time", required=True)
    claim.add_argument("--retry-attempt")
    claim.add_argument("--current", action="store_true")
    claim.set_defaults(handler=_command_claim)

    prepare = subparsers.add_parser("prepare")
    for name in (
        "task-file",
        "output",
        "schedule-file",
        "infrast-dir",
        "ledger",
        "execution-key",
        "attempt-id",
    ):
        prepare.add_argument(f"--{name}", required=True)
    prepare.set_defaults(handler=_command_prepare)

    takeover = subparsers.add_parser("takeover")
    for name in ("task-file", "schedule-file", "output"):
        takeover.add_argument(f"--{name}", required=True)
    takeover.set_defaults(handler=_command_takeover)

    mark = subparsers.add_parser("mark")
    for name in ("ledger", "execution-key", "attempt-id"):
        mark.add_argument(f"--{name}", required=True)
    mark.add_argument("--state", choices=sorted(STATES), required=True)
    mark.add_argument("--detail")
    mark.set_defaults(handler=_command_mark)

    status = subparsers.add_parser("status")
    status.add_argument("--ledger", required=True)
    status.add_argument("--execution-key", required=True)
    status.set_defaults(handler=_command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CliValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LedgerError, StrictTaskConfigError, RuntimePreflightError) as exc:
        print(f"{getattr(exc, 'code', 'runtime_error')}: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"io_error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
