#!/usr/bin/env python3
"""MaaCore OperBox callback parsing, snapshots, and plan comparison."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import NoReturn
import re


SNAPSHOT_SCHEMA_VERSION = 1
STALE_AFTER_SECONDS = 8 * 24 * 60 * 60
CALLBACK_MARKER = "OperBox: "
CORE_CALLBACK_MARKER = "Assistant::append_callback | SubTaskExtraInfo "
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_CRON = {day: (index + 1) % 7 for index, day in enumerate(WEEKDAYS)}
MANAGED_BEGIN = b"# BEGIN MAA_WEBUI_OPERATOR_SCAN"
MANAGED_END = b"# END MAA_WEBUI_OPERATOR_SCAN"


def _get_scan_command() -> bytes:
    py_bin = sys.executable or "/usr/bin/python3"
    scan_script = str(Path(os.environ.get("MAA_HOME", "/root/maa-scripts")) / "maa-operator-scan.py")
    log_path = str(Path(os.environ.get("MAA_LOG_DIR", "/var/log/maa")) / "operator-scan-cron.log")
    return f"{py_bin} {scan_script} >> {log_path} 2>&1".encode("utf-8")


SCAN_COMMAND = _get_scan_command()

UNMANAGED_SCAN_RE = re.compile(
    rb"(?:[^\s;]+[/\\])?(?:maa-operator-scan\.py|maa-run(?:\.sh)?[ \t]+operator-scan)(?:[ \t]|$)"
)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class OperatorBoxError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return self.message


def _error(code: str, path: str, message: str) -> NoReturn:
    raise OperatorBoxError(code, path, message)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, path: str) -> datetime:
    if not isinstance(value, str):
        _error("invalid_snapshot", path, "时间字段无效")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError as exc:
        raise OperatorBoxError("invalid_snapshot", path, "时间字段无效") from exc


def _validate_operator(
    value: object, path: str, *, detailed: bool
) -> dict[str, object]:
    if not isinstance(value, dict):
        _error("invalid_callback", path, "干员记录必须是对象")
    required = {"id": str, "name": str, "own": bool, "rarity": int}
    if detailed:
        required.update({"elite": int, "level": int, "potential": int})
    result: dict[str, object] = {}
    for field, expected in required.items():
        item = value.get(field)
        if expected is int:
            valid = type(item) is int
        else:
            valid = isinstance(item, expected)
        if not valid or (expected is str and not item):
            _error("invalid_callback", f"{path}.{field}", "干员字段无效")
        result[field] = item
    for field in ("name_en", "name_jp", "name_kr", "name_tw"):
        if isinstance(value.get(field), str):
            result[field] = value[field]
    return result


def validate_final_callback(callback: object) -> dict[str, object]:
    if not isinstance(callback, dict) or callback.get("what") != "OperBoxInfo":
        _error("invalid_callback", "$", "不是 OperBoxInfo 回调")
    details = callback.get("details")
    if not isinstance(details, dict):
        _error("invalid_callback", "$.details", "缺少 OperBox 结果")
    if details.get("done") is not True:
        _error("scan_incomplete", "$.details.done", "扫描没有返回最终完成结果")
    all_values = details.get("all_opers", details.get("all_oper"))
    own_values = details.get("own_opers")
    if not isinstance(all_values, list) or not isinstance(own_values, list):
        _error("invalid_callback", "$.details", "干员列表字段无效")
    all_opers = [
        _validate_operator(item, f"$.details.all_opers[{index}]", detailed=False)
        for index, item in enumerate(all_values)
    ]
    own_opers = [
        _validate_operator(item, f"$.details.own_opers[{index}]", detailed=True)
        for index, item in enumerate(own_values)
    ]
    if not all_opers:
        _error("invalid_callback", "$.details.all_opers", "全干员列表不能为空")
    if len({item["id"] for item in all_opers}) != len(all_opers):
        _error("invalid_callback", "$.details.all_opers", "全干员列表 ID 重复")
    if any(item["own"] is not True for item in own_opers):
        _error("invalid_callback", "$.details.own_opers", "已拥有列表包含未拥有干员")
    return {"done": True, "all_opers": all_opers, "own_opers": own_opers}


def _extract_callbacks_with_marker(
    log_text: str, marker_text: str
) -> list[dict[str, object]]:
    if not isinstance(log_text, str):
        _error("invalid_log", "$", "扫描日志必须是文本")
    decoder = json.JSONDecoder()
    callbacks: list[dict[str, object]] = []
    position = 0
    while True:
        marker = log_text.find(marker_text, position)
        if marker < 0:
            break
        start = marker + len(marker_text)
        try:
            value, end = decoder.raw_decode(log_text, start)
        except json.JSONDecodeError:
            position = start
            continue
        position = end
        if isinstance(value, dict) and value.get("what") == "OperBoxInfo":
            callbacks.append(value)
    return callbacks


def extract_callbacks(log_text: str) -> list[dict[str, object]]:
    return _extract_callbacks_with_marker(log_text, CALLBACK_MARKER)


def extract_final_callback(log_text: str) -> dict[str, object]:
    callbacks = extract_callbacks(log_text)
    for callback in reversed(callbacks):
        if callback.get("details", {}).get("done") is True:
            validate_final_callback(callback)
            return callback
    if callbacks:
        _error("scan_incomplete", "$.details.done", "扫描未完成，未保存本次结果")
    _error("callback_missing", "$", "扫描日志中没有 OperBox 回调")


def extract_final_callback_from_sources(
    runner_log: str, core_log: str
) -> tuple[dict[str, object], str]:
    runner_failure: OperatorBoxError
    try:
        return extract_final_callback(runner_log), "maa_cli"
    except OperatorBoxError as error:
        if error.code not in {"scan_incomplete", "callback_missing"}:
            raise
        runner_failure = error
    core_callbacks = _extract_callbacks_with_marker(
        core_log, CORE_CALLBACK_MARKER
    )
    for callback in reversed(core_callbacks):
        if callback.get("details", {}).get("done") is True:
            validate_final_callback(callback)
            return callback, "maacore_debug"
    if core_callbacks or runner_failure.code == "scan_incomplete":
        _error("scan_incomplete", "$.details.done", "扫描未完成，未保存本次结果")
    raise runner_failure


def build_snapshot(
    result: dict[str, object],
    *,
    scanned_at: str | None = None,
    source_log: str,
    core_version: str,
    resource_version: str,
) -> dict[str, object]:
    normalized = (
        validate_final_callback(result)
        if "details" in result
        else result
    )
    if normalized.get("done") is not True:
        _error("scan_incomplete", "$.done", "扫描没有完成")
    timestamp = scanned_at or _iso_utc()
    _parse_time(timestamp, "$.scanned_at")
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "scanned_at": timestamp,
        "core_version": str(core_version or ""),
        "resource_version": str(resource_version or ""),
        "source_sha256": hashlib.sha256(source_log.encode("utf-8")).hexdigest(),
        "all_count": len(normalized["all_opers"]),
        "owned_count": len(normalized["own_opers"]),
        "all_opers": normalized["all_opers"],
        "own_opers": normalized["own_opers"],
    }
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    snapshot["sha256"] = hashlib.sha256(canonical).hexdigest()
    return snapshot


def _atomic_write_json(path: Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_successful_scan(
    snapshot_path: Path,
    state_path: Path,
    callback: dict[str, object],
    *,
    scanned_at: str | None = None,
    source_log: str,
    core_version: str,
    resource_version: str,
) -> dict[str, object]:
    snapshot = build_snapshot(
        validate_final_callback(callback),
        scanned_at=scanned_at,
        source_log=source_log,
        core_version=core_version,
        resource_version=resource_version,
    )
    _atomic_write_json(snapshot_path, snapshot)
    _atomic_write_json(
        state_path,
        {
            "last_attempt": {
                "status": "success",
                "attempted_at": snapshot["scanned_at"],
                "code": "",
                "message": "",
                "source_sha256": snapshot["source_sha256"],
            }
        },
    )
    return snapshot


def save_failed_attempt(
    state_path: Path,
    *,
    code: str,
    message: str,
    attempted_at: str | None = None,
    source_log: str = "",
) -> None:
    timestamp = attempted_at or _iso_utc()
    _parse_time(timestamp, "$.attempted_at")
    _atomic_write_json(
        state_path,
        {
            "last_attempt": {
                "status": "failed",
                "attempted_at": timestamp,
                "code": str(code),
                "message": str(message),
                "source_sha256": hashlib.sha256(
                    source_log.encode("utf-8")
                ).hexdigest(),
            }
        },
    )


def _read_json_object(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_scan_status(
    snapshot_path: Path,
    state_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    snapshot = _read_json_object(snapshot_path)
    state = _read_json_object(state_path) or {}
    last_attempt = state.get("last_attempt")
    if not isinstance(last_attempt, dict):
        last_attempt = None
    valid_snapshot = False
    if isinstance(snapshot, dict):
        try:
            if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
                raise ValueError("schema")
            all_opers = snapshot.get("all_opers")
            own_opers = snapshot.get("own_opers")
            if not isinstance(all_opers, list) or not isinstance(own_opers, list):
                raise ValueError("lists")
            for index, item in enumerate(all_opers):
                _validate_operator(item, f"$.all_opers[{index}]", detailed=False)
            for index, item in enumerate(own_opers):
                _validate_operator(item, f"$.own_opers[{index}]", detailed=True)
            if snapshot.get("all_count") != len(all_opers) or snapshot.get(
                "owned_count"
            ) != len(own_opers):
                raise ValueError("counts")
            expected_sha = snapshot.get("sha256")
            unsigned = {key: value for key, value in snapshot.items() if key != "sha256"}
            canonical = json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if (
                not isinstance(expected_sha, str)
                or hashlib.sha256(canonical).hexdigest() != expected_sha
            ):
                raise ValueError("sha256")
            _parse_time(snapshot.get("scanned_at"), "$.scanned_at")
            valid_snapshot = True
        except (OperatorBoxError, ValueError):
            valid_snapshot = False
    age_seconds = None
    if valid_snapshot:
        try:
            age_seconds = max(
                0,
                int(
                    (
                        (now or _utc_now()).astimezone(timezone.utc)
                        - _parse_time(snapshot["scanned_at"], "$.scanned_at")
                    ).total_seconds()
                ),
            )
        except OperatorBoxError:
            valid_snapshot = False
    stale = (
        not valid_snapshot
        or age_seconds is None
        or age_seconds > STALE_AFTER_SECONDS
        or bool(last_attempt and last_attempt.get("status") == "failed")
    )
    return {
        "available": bool(valid_snapshot),
        "stale": stale,
        "age_seconds": age_seconds,
        "snapshot": snapshot if valid_snapshot else None,
        "last_attempt": last_attempt,
    }


def _add_names(target: set[str], values: object) -> None:
    if isinstance(values, list):
        target.update(item for item in values if isinstance(item, str) and item)


def collect_plan_references(
    details: dict[str, object], plan_indexes: list[int] | None = None
) -> dict[str, list[str]]:
    plans = details.get("plans", [])
    if not isinstance(plans, list):
        _error("invalid_plan", "$.plans", "排班详情无效")
    selected = set(plan_indexes) if plan_indexes is not None else None
    required: set[str] = set()
    candidates: set[str] = set()
    for position, plan in enumerate(plans):
        if not isinstance(plan, dict):
            continue
        index = plan.get("index", position)
        if selected is not None and index not in selected:
            continue
        groups = {
            group.get("name"): group.get("operators", [])
            for group in plan.get("groups", [])
            if isinstance(group, dict) and isinstance(group.get("name"), str)
        }
        rooms = plan.get("rooms", {})
        if isinstance(rooms, dict):
            for facility_rooms in rooms.values():
                if not isinstance(facility_rooms, list):
                    continue
                for room in facility_rooms:
                    if not isinstance(room, dict):
                        continue
                    room_operators = room.get("operators", [])
                    if room.get("use_operator_groups") is True:
                        if isinstance(room_operators, list):
                            for group_name in room_operators:
                                _add_names(required, groups.get(group_name))
                    else:
                        _add_names(required, room_operators)
                    _add_names(candidates, room.get("candidates"))
        fiammetta = plan.get("Fiammetta")
        if (
            isinstance(fiammetta, dict)
            and fiammetta.get("enable") is True
            and isinstance(fiammetta.get("target"), str)
            and fiammetta["target"]
        ):
            required.add(fiammetta["target"])
    candidates.difference_update(required)
    return {
        "required": sorted(required),
        "candidates": sorted(candidates),
    }


def compare_plan(
    details: dict[str, object],
    snapshot: dict[str, object],
    plan_indexes: list[int] | None = None,
) -> dict[str, object]:
    references = collect_plan_references(details, plan_indexes)
    owned = {
        item.get("name")
        for item in snapshot.get("all_opers", [])
        if isinstance(item, dict)
        and item.get("own") is True
        and isinstance(item.get("name"), str)
    }
    return {
        **references,
        "required_missing": sorted(set(references["required"]) - owned),
        "candidate_missing": sorted(set(references["candidates"]) - owned),
        "owned_count": len(owned),
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _split_managed_crontab(raw: bytes) -> tuple[bytes, bytes | None]:
    begin_count = raw.count(MANAGED_BEGIN)
    end_count = raw.count(MANAGED_END)
    if begin_count == 0 and end_count == 0:
        return raw.rstrip(b"\n"), None
    if begin_count != 1 or end_count != 1:
        _error("invalid_crontab", "$.crontab", "每周扫描受管区块损坏")
    start = raw.index(MANAGED_BEGIN)
    end_start = raw.index(MANAGED_END)
    if end_start < start:
        _error("invalid_crontab", "$.crontab", "每周扫描受管区块顺序无效")
    end = end_start + len(MANAGED_END)
    block = raw[start:end]
    remaining = (raw[:start] + raw[end:]).strip(b"\n")
    return remaining, block


def _parse_clock_time(value: object, path: str) -> tuple[int, int]:
    if not isinstance(value, str):
        _error("invalid_time", path, "时间必须是 HH:MM")
    match = TIME_RE.fullmatch(value)
    if not match:
        _error("invalid_time", path, "时间必须是有效的 HH:MM")
    return int(match.group(1)), int(match.group(2))


def _cron_conflicts(raw: bytes, weekday: str, hour: int, minute: int) -> list[dict[str, object]]:
    target_day = WEEKDAY_CRON[weekday]
    target_minute = hour * 60 + minute
    conflicts = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(b"#"):
            continue
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        try:
            cron_minute = int(fields[0])
            cron_hour = int(fields[1])
        except ValueError:
            continue
        day_field = fields[4]
        if day_field != b"*":
            try:
                days = {int(item) % 7 for item in day_field.split(b",")}
            except ValueError:
                continue
            if target_day not in days:
                continue
        distance = abs((cron_hour * 60 + cron_minute) - target_minute)
        distance = min(distance, 24 * 60 - distance)
        if distance < 120:
            conflicts.append(
                {
                    "distance_minutes": distance,
                    "line": line.decode("utf-8", "replace"),
                }
            )
    return sorted(conflicts, key=lambda item: item["distance_minutes"])


def preview_weekly_schedule(
    config_dir: Path, request: dict[str, object], crontab_raw: bytes
) -> dict[str, object]:
    if not isinstance(request, dict) or set(request) != {
        "enabled",
        "weekday",
        "time",
        "confirmed_conflicts",
    }:
        _error("invalid_request", "$", "每周扫描请求字段无效")
    enabled = request["enabled"]
    if type(enabled) is not bool or type(request["confirmed_conflicts"]) is not bool:
        _error("invalid_request", "$", "开关与确认字段必须是布尔值")
    weekday = request["weekday"]
    if weekday not in WEEKDAYS:
        _error("invalid_weekday", "$.weekday", "星期字段无效")
    hour, minute = _parse_clock_time(request["time"], "$.time")
    base, current_block = _split_managed_crontab(crontab_raw)
    scan_cmd = _get_scan_command()
    if scan_cmd in base or UNMANAGED_SCAN_RE.search(base):
        _error("unmanaged_duplicate", "$.crontab", "发现未受管的重复干员扫描定时")
    if enabled:
        status = load_scan_status(
            Path(config_dir) / "operator-box.json",
            Path(config_dir) / "operator-box-state.json",
        )
        if not status["available"]:
            _error("snapshot_missing", "$.enabled", "首次完整扫描成功后才能启用")
    conflicts = _cron_conflicts(base, weekday, hour, minute) if enabled else []
    if conflicts and request["confirmed_conflicts"] is not True:
        status = "conflict"
    else:
        status = "changed"
    if enabled:
        cron_day = WEEKDAY_CRON[weekday]
        managed = b"\n".join(
            (
                MANAGED_BEGIN,
                f"{minute} {hour} * * {cron_day} ".encode() + scan_cmd,
                MANAGED_END,
            )
        )
        candidate = (base + (b"\n" if base else b"") + managed + b"\n")
    else:
        candidate = base + (b"\n" if base else b"")
    if candidate == crontab_raw:
        status = "unchanged"
    return {
        "status": status,
        "enabled": enabled,
        "weekday": weekday,
        "time": request["time"],
        "conflicts": conflicts,
        "expected_crontab_sha256": _sha256_bytes(crontab_raw),
        "candidate_crontab_sha256": _sha256_bytes(candidate),
        "current_managed": current_block is not None,
        "candidate_crontab": candidate,
    }


def commit_weekly_schedule(
    config_dir: Path,
    request: dict[str, object],
    expected_crontab_sha256: str,
    read_crontab,
    install_crontab,
) -> dict[str, object]:
    current = read_crontab()
    if _sha256_bytes(current) != expected_crontab_sha256:
        _error("stale_preview", "$.crontab", "定时内容已变化，请重新预览")
    preview = preview_weekly_schedule(config_dir, request, current)
    if preview["status"] == "conflict":
        _error("conflict_unconfirmed", "$.confirmed_conflicts", "请先确认时间冲突提醒")
    candidate = preview.pop("candidate_crontab")
    changed = candidate != current
    if changed:
        install_crontab(candidate)
        if read_crontab() != candidate:
            try:
                install_crontab(current)
            finally:
                _error("write_failed", "$.crontab", "定时写入后校验失败")
    try:
        _atomic_write_json(
            Path(config_dir) / "operator-scan-schedule.json",
            {
                "schema_version": 1,
                "enabled": request["enabled"],
                "weekday": request["weekday"],
                "time": request["time"],
                "updated_at": _iso_utc(),
            },
        )
    except Exception:
        if changed:
            install_crontab(current)
        raise
    return {**preview, "status": "committed"}


def load_weekly_schedule(config_dir: Path, crontab_raw: bytes) -> dict[str, object]:
    settings = _read_json_object(Path(config_dir) / "operator-scan-schedule.json") or {}
    _, block = _split_managed_crontab(crontab_raw)
    return {
        "enabled": bool(settings.get("enabled")) and block is not None,
        "weekday": settings.get("weekday") if settings.get("weekday") in WEEKDAYS else "sun",
        "time": settings.get("time") if isinstance(settings.get("time"), str) and TIME_RE.fullmatch(settings["time"]) else "07:30",
        "managed": block is not None,
    }
