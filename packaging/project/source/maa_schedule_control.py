#!/usr/bin/env python3
"""One-shot controls for scheduled MAA daily runs."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


SKIP_FILENAME = "skip-next-daily.json"
DAILY_CRON_RE = re.compile(
    rb"^[ \t]*(\d{1,2})[ \t]+(\d{1,2})[ \t]+\*[ \t]+\*[ \t]+([^ \t]+)"
    rb"[ \t]+/root/maa-scripts/maa-run\.sh[ \t]+daily(?:[ \t]|$)"
)


def skip_file(config_dir: str | Path) -> Path:
    return Path(config_dir) / SKIP_FILENAME


def _cron_weekdays(value: str) -> set[int]:
    if value == "*":
        return set(range(7))
    result: set[int] = set()
    for token in value.split(","):
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text) % 7, int(end_text) % 7
            current = start
            while True:
                result.add(current)
                if current == end:
                    break
                current = (current + 1) % 7
        else:
            result.add(int(token) % 7)
    return result


def next_daily_run(crontab_raw: bytes, now: datetime | None = None) -> datetime:
    if not isinstance(crontab_raw, bytes):
        raise ValueError("crontab must be bytes")
    now = now or datetime.now().astimezone()
    entries: list[tuple[int, int, set[int]]] = []
    for line in crontab_raw.splitlines():
        match = DAILY_CRON_RE.search(line)
        if not match:
            continue
        minute, hour = int(match.group(1)), int(match.group(2))
        if minute > 59 or hour > 23:
            continue
        try:
            weekdays = _cron_weekdays(match.group(3).decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        entries.append((hour, minute, weekdays))
    if not entries:
        raise ValueError("没有可识别的 maa-run.sh daily 定时")

    candidates = []
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        cron_weekday = (day.weekday() + 1) % 7
        for hour, minute, weekdays in entries:
            if cron_weekday not in weekdays:
                continue
            candidate = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo).replace(
                hour=hour, minute=minute
            )
            if candidate > now:
                candidates.append(candidate)
    if not candidates:
        raise ValueError("无法计算下一次日常定时")
    return min(candidates)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_skip_next_daily(config_dir: str | Path) -> dict:
    path = skip_file(config_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active": False, "scheduled_for": "", "armed_at": ""}
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("scheduled_for"), str)
        or not isinstance(data.get("armed_at"), str)
    ):
        return {"active": False, "scheduled_for": "", "armed_at": ""}
    return {
        "active": True,
        "scheduled_for": data["scheduled_for"],
        "armed_at": data["armed_at"],
    }


def arm_skip_next_daily(
    config_dir: str | Path, crontab_raw: bytes, now: datetime | None = None
) -> dict:
    now = now or datetime.now().astimezone()
    scheduled_for = next_daily_run(crontab_raw, now=now)
    payload = {
        "schema_version": 1,
        "armed_at": now.isoformat(timespec="seconds"),
        "scheduled_for": scheduled_for.isoformat(timespec="seconds"),
    }
    _atomic_write_json(skip_file(config_dir), payload)
    return load_skip_next_daily(config_dir)


def cancel_skip_next_daily(config_dir: str | Path) -> bool:
    try:
        skip_file(config_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def consume_skip_next_daily(config_dir: str | Path, *, is_cron: bool) -> bool:
    if not is_cron:
        return False
    path = skip_file(config_dir)
    claim = path.with_name(f".{path.name}.consumed-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        os.replace(path, claim)
    except FileNotFoundError:
        return False
    try:
        try:
            data = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("schema_version") == 1
            and isinstance(data.get("scheduled_for"), str)
        )
    finally:
        try:
            claim.unlink()
        except FileNotFoundError:
            pass


def _parent_pid(proc_root: Path, pid: int) -> int | None:
    try:
        for line in (proc_root / str(pid) / "status").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.startswith("PPid:"):
                return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def is_cron_invocation(parent_pid: int, proc_root: str | Path = "/proc") -> bool:
    root = Path(proc_root)
    pid = int(parent_pid)
    for _ in range(6):
        if pid <= 1:
            return False
        try:
            comm = (root / str(pid) / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return False
        if comm.lower() in {"cron", "crond"}:
            return True
        parent = _parent_pid(root, pid)
        if parent is None or parent == pid:
            return False
        pid = parent
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    consume = subparsers.add_parser("consume")
    consume.add_argument("--config-dir", required=True)
    consume.add_argument("--parent-pid", type=int, required=True)
    consume.add_argument("--proc-root", default="/proc")
    args = parser.parse_args(argv)
    if args.command == "consume":
        cron = is_cron_invocation(args.parent_pid, args.proc_root)
        if consume_skip_next_daily(args.config_dir, is_cron=cron):
            print("SKIP_NEXT_DAILY_CONSUMED")
            return 0
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
