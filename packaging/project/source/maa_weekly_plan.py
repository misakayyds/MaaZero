#!/usr/bin/env python3
"""Weekly task configuration selection shared by the MAA runner and WebUI."""

import argparse
import json
import os
import shlex
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maa_task_config import validate_task_bytes


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_SETTINGS = {"enabled": False, "initialized": False}
SETTINGS_FILENAME = "weekly-plan.json"
CN_TIMEZONE = timezone(timedelta(hours=8))


class WeeklyPlanError(RuntimeError):
    """Raised when weekly-plan state cannot be safely created or resolved."""


def settings_path(config_dir: Path) -> Path:
    return Path(config_dir) / SETTINGS_FILENAME


def daily_task_path(config_dir: Path) -> Path:
    return Path(config_dir) / "tasks" / "daily.toml"


def weekday_task_path(config_dir: Path, weekday: str) -> Path:
    if weekday not in WEEKDAYS:
        raise ValueError(f"invalid weekday: {weekday}")
    return Path(config_dir) / "tasks" / f"daily-{weekday}.toml"


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_name = temp_file.name
        os.replace(temp_name, path)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(Path(path), content.encode("utf-8"))


def load_settings(config_dir: Path) -> dict:
    try:
        data = json.loads(settings_path(config_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        return dict(DEFAULT_SETTINGS)
    return {
        "enabled": data.get("enabled") is True,
        "initialized": data.get("initialized") is True,
    }


def _write_settings(config_dir: Path, settings: dict) -> dict:
    normalized = {
        "enabled": settings.get("enabled") is True,
        "initialized": settings.get("initialized") is True,
    }
    atomic_write_text(
        settings_path(config_dir),
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
    )
    return normalized


def validate_task_file(path: Path) -> tuple[bool, str]:
    path = Path(path)
    if not path.is_file():
        return False, "missing"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, f"read_error:{exc}"
    return validate_task_bytes(raw)


def initialize_weekday_files(config_dir: Path) -> list[str]:
    config_dir = Path(config_dir)
    source = daily_task_path(config_dir)
    source_valid, source_error = validate_task_file(source)
    if not source_valid:
        raise WeeklyPlanError(f"daily.toml is not usable: {source_error}")

    source_content = source.read_bytes()
    created = []
    for weekday in WEEKDAYS:
        target = weekday_task_path(config_dir, weekday)
        target_valid, _ = validate_task_file(target)
        if target_valid:
            continue
        _atomic_write_bytes(target, source_content)
        created.append(weekday)
    return created


def set_enabled(config_dir: Path, enabled: bool) -> dict:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    current = load_settings(config_dir)
    if enabled:
        initialize_weekday_files(config_dir)
        current = {"enabled": True, "initialized": True}
    else:
        current["enabled"] = False
    return _write_settings(config_dir, current)


def resolve_daily_task(config_dir: Path, weekday: str | None = None) -> dict:
    config_dir = Path(config_dir)
    settings = load_settings(config_dir)
    if weekday is not None and weekday not in WEEKDAYS:
        raise ValueError(f"invalid weekday: {weekday}")

    # CN stages reset at 04:00; midnight cron still belongs to the previous game day.
    logical_weekday = weekday or WEEKDAYS[(datetime.now(CN_TIMEZONE) - timedelta(hours=4)).weekday()]
    use_weekday = weekday is not None or settings["enabled"]
    daily_path = daily_task_path(config_dir)

    if not use_weekday:
        return {
            "effective_task": "daily",
            "source_path": str(daily_path),
            "requested_path": str(daily_path),
            "weekly_enabled": False,
            "logical_weekday": logical_weekday,
            "fallback_reason": "",
        }

    requested_path = weekday_task_path(config_dir, logical_weekday)
    valid, error = validate_task_file(requested_path)
    if valid:
        return {
            "effective_task": f"daily-{logical_weekday}",
            "source_path": str(requested_path),
            "requested_path": str(requested_path),
            "weekly_enabled": settings["enabled"],
            "logical_weekday": logical_weekday,
            "fallback_reason": "",
        }

    daily_valid, daily_error = validate_task_file(daily_path)
    if not daily_valid:
        raise WeeklyPlanError(
            f"weekday config failed ({error}) and daily.toml is not usable ({daily_error})"
        )
    return {
        "effective_task": "daily",
        "source_path": str(daily_path),
        "requested_path": str(requested_path),
        "weekly_enabled": settings["enabled"],
        "logical_weekday": logical_weekday,
        "fallback_reason": error,
    }


def format_shell(result: dict) -> str:
    fields = {
        "WEEKLY_EFFECTIVE_TASK": result["effective_task"],
        "WEEKLY_SOURCE_PATH": result["source_path"],
        "WEEKLY_REQUESTED_PATH": result["requested_path"],
        "WEEKLY_LOGICAL_WEEKDAY": result["logical_weekday"],
        "WEEKLY_ENABLED": "1" if result["weekly_enabled"] else "0",
        "WEEKLY_FALLBACK_REASON": result["fallback_reason"],
    }
    return "\n".join(
        f"{key}={shlex.quote(str(value))}" for key, value in fields.items()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve MAA weekly task configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--config-dir", required=True, type=Path)
    resolve_parser.add_argument("--weekday", choices=WEEKDAYS)
    resolve_parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args()

    try:
        result = resolve_daily_task(args.config_dir, args.weekday)
    except (OSError, ValueError, WeeklyPlanError) as exc:
        print(f"weekly plan resolve failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "shell":
        print(format_shell(result))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
