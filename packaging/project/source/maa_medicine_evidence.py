"""Parse conservative medicine-use evidence from MaaCore logs.

The parser deliberately treats ``init_count`` as a reservation, not a
consumption.  Consumption is only deducted from a successful UseMedicine
callback, or from a later count-only callback when a compatible inventory
baseline is already available.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any


CN = timezone(timedelta(hours=8))
_TIMESTAMP = re.compile(
    r"\[(?P<value>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)\]"
)
_PID = re.compile(r"\[Px(?P<pid>\d+)\]")
_TRC_INIT = re.compile(
    r"init_count\s+medicine\s+using\s+count\s*:\s*(?P<using>-?\d+)"
    r"\s*,\s*inventory\s+count\s*:\s*(?P<inventory>-?\d+)"
    r"\s*,\s*expire\s+days\s*:\s*(?P<expire>-?\d+)",
    re.IGNORECASE,
)
_TRC_FINISHED = re.compile(
    r"Using\s+medicines\s+init\s+finished\s*,\s*using\s*:\s*(?P<using>-?\d+)"
    r"\s*,\s*used\s*:\s*(?P<used>-?\d+)\s*,\s*max\s*:\s*(?P<max>-?\d+)",
    re.IGNORECASE,
)
_CALLBACK_MARKER = "Assistant::append_callback"
_DEDUP_WINDOW = timedelta(seconds=5)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CN)
    return parsed.astimezone(CN)


def _normalize_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("since must be a datetime or None")
    if value.tzinfo is None:
        return value.replace(tzinfo=CN)
    return value.astimezone(CN)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    return None


def _timestamp_and_pid(line: str) -> tuple[datetime | None, str | None]:
    timestamp_match = _TIMESTAMP.search(line)
    timestamp = _parse_timestamp(timestamp_match.group("value")) if timestamp_match else None
    pid_match = _PID.search(line)
    return timestamp, pid_match.group("pid") if pid_match else None


def _callback_payload(line: str) -> dict[str, Any] | None:
    if _CALLBACK_MARKER not in line:
        return None
    start = line.find("{", line.find(_CALLBACK_MARKER))
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(line[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("what") != "UseMedicine":
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return {"payload": payload, "details": None}
    return {"payload": payload, "details": details}


def _new_trc_group(timestamp: datetime, pid: str | None, match: re.Match[str]) -> dict[str, Any]:
    return {
        "kind": "trc",
        "pid": pid,
        "observed_at": timestamp,
        "event_at": timestamp,
        "batches": [
            {
                "expire_days": int(match.group("expire")),
                "inventory": int(match.group("inventory")),
                "use": 0,
                "observed_at": timestamp,
            }
        ],
    }


def _complete_trc_group(group: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    completed = deepcopy(group)
    completed["event_at"] = timestamp
    completed["complete"] = True
    return completed


def _trace_events(lines: list[str]) -> list[dict[str, Any]]:
    active: dict[str | None, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for line in lines:
        timestamp, pid = _timestamp_and_pid(line)
        if timestamp is None:
            continue
        init_match = _TRC_INIT.search(line)
        if init_match:
            group = active.get(pid)
            if group is None or group.get("complete"):
                group = _new_trc_group(timestamp, pid, init_match)
                active[pid] = group
            else:
                group["batches"].append(
                    {
                        "expire_days": int(init_match.group("expire")),
                        "inventory": int(init_match.group("inventory")),
                        "use": 0,
                        "observed_at": timestamp,
                    }
                )
            continue

        if not _TRC_FINISHED.search(line):
            continue
        group = active.get(pid)
        if group is None and pid is None:
            candidates = [item for item in active.values() if not item.get("complete")]
            if candidates:
                group = max(candidates, key=lambda item: item["event_at"])
        if group is None or group.get("complete"):
            continue
        completed = _complete_trc_group(group, timestamp)
        group["complete"] = True
        events.append(completed)
    return events


def _callback_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        timestamp, pid = _timestamp_and_pid(line)
        payload = _callback_payload(line)
        if timestamp is None or payload is None:
            continue
        details = payload["details"]
        medicines = details.get("medicines") if isinstance(details, dict) else None
        kind = "callback_array" if isinstance(medicines, list) and medicines else "callback_count"
        events.append(
            {
                "kind": kind,
                "pid": pid,
                "event_at": timestamp,
                "payload": payload["payload"],
                "details": details,
            }
        )

    deduplicated: list[dict[str, Any]] = []
    for event in events:
        fingerprint = json.dumps(
            {key: event["payload"].get(key) for key in ("details", "taskchain", "taskid", "what")},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        duplicate = next(
            (
                prior
                for prior in reversed(deduplicated)
                if prior["fingerprint"] == fingerprint
                and prior["pid"] == event["pid"]
                and abs(event["event_at"] - prior["event_at"]) <= _DEDUP_WINDOW
            ),
            None,
        )
        if duplicate is None:
            event["fingerprint"] = fingerprint
            deduplicated.append(event)
    return deduplicated


def _expiry_lower_bound(observed_at: datetime, expire_days: Any, source: str) -> tuple[datetime | None, str | None]:
    days = _as_int(expire_days)
    if days is None:
        return None, "invalid expire_days"
    if days < 0:
        return None, "unknown expire_days"
    if source == "callback":
        safe_days = max(0, days - 1)
    else:
        safe_days = days
    if safe_days <= 0:
        return None, "zero-day safe expiry window"
    return observed_at + timedelta(days=safe_days), None


def _build_batches(records: Any, *, source: str, observed_at: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(records, list) or not records:
        return [], ["missing medicines array"]
    batches: list[dict[str, Any]] = []
    errors: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            errors.append("invalid medicine batch")
            continue
        expire_days = _as_int(record.get("expire_days"))
        inventory = _as_int(record.get("inventory"))
        use = _as_int(record.get("use")) if source == "callback" else 0
        batch_observed_at = observed_at
        if source == "trc":
            batch_observed_at = record.get("observed_at", observed_at)
        if inventory is None or inventory < 0:
            errors.append("invalid inventory")
            continue
        if use is None or use < 0 or use > inventory:
            errors.append("invalid use")
            continue
        expires_at, expiry_error = _expiry_lower_bound(batch_observed_at, expire_days, source)
        if expiry_error:
            errors.append(expiry_error)
        batches.append(
            {
                "expire_days": expire_days,
                "inventory": inventory,
                "use": use,
                "remaining": inventory - use,
                "observed_at": batch_observed_at,
                "expires_at": expires_at,
            }
        )
    return batches, errors


def _result(
    *,
    observed_at: datetime,
    batches: list[dict[str, Any]],
    source: str,
    errors: list[str] | None = None,
    reason: str | None = None,
    confirmed_used: int = 0,
) -> dict[str, Any]:
    errors = list(errors or [])
    if not batches:
        errors.append("no usable medicine batches")
    if any(batch["expires_at"] is None for batch in batches):
        errors.append("expiry is not a positive conservative lower bound")
    if errors:
        blocked_parts = ([reason] if reason else []) + errors
        return {
            "observed_at": observed_at,
            "expires_at": None,
            "remaining_count": (
                sum(batch["remaining"] for batch in batches) if batches else None
            ),
            "source": source,
            "batches": batches,
            "confirmed_used": confirmed_used,
            "accurate": False,
            "reason": "blocked: " + "; ".join(dict.fromkeys(blocked_parts)),
        }
    return {
        "observed_at": observed_at,
        "expires_at": min(batch["expires_at"] for batch in batches),
        "remaining_count": sum(batch["remaining"] for batch in batches),
        "source": source,
        "batches": batches,
        "confirmed_used": confirmed_used,
        "accurate": True,
        "reason": reason or "conservative expiry lower bound",
    }


def _apply_count_only(state: dict[str, Any] | None, count: Any, observed_at: datetime) -> dict[str, Any]:
    amount = _as_int(count)
    if state is None:
        return _result(
            observed_at=observed_at,
            batches=[],
            source="callback_count",
            errors=["count-only callback has no inventory baseline"],
        )
    if amount is None or amount < 0:
        return _result(
            observed_at=observed_at,
            batches=[],
            source="callback_count",
            errors=["invalid callback count"],
        )
    batches = deepcopy(state["batches"])
    remaining_to_apply = amount
    for batch in sorted(batches, key=lambda item: (item["expires_at"] is None, item["expires_at"] or datetime.max.replace(tzinfo=CN))):
        if remaining_to_apply <= 0:
            break
        consumed = min(batch["remaining"], remaining_to_apply)
        batch["use"] += consumed
        batch["remaining"] -= consumed
        remaining_to_apply -= consumed
    if remaining_to_apply:
        return _result(
            observed_at=observed_at,
            batches=[],
            source="callback_count",
            errors=["callback count exceeds known inventory"],
        )
    return _result(
        observed_at=observed_at,
        batches=batches,
        source="callback_count",
        reason="count-only confirmation reduced a prior inventory baseline",
    )


def _confirmed_used(event: dict[str, Any]) -> int:
    details = event.get("details")
    if not isinstance(details, dict):
        return 0
    medicines = details.get("medicines")
    if isinstance(medicines, list) and medicines:
        uses = [_as_int(item.get("use")) for item in medicines if isinstance(item, dict)]
        if len(uses) == len(medicines) and all(use is not None and use >= 0 for use in uses):
            return sum(uses)
    count = _as_int(details.get("count"))
    return count if count is not None and count >= 0 else 0


def parse_medicine_evidence(text: str, *, since: datetime | None = None) -> dict | None:
    """Return the newest conservative medicine evidence in ``text``.

    Naive log timestamps and a naive ``since`` value are interpreted as
    Beijing time (UTC+8).  ``expires_at`` is an earliest safe bound, never an
    invented exact expiry time.  Evidence with no positive safe bound is
    returned as ``accurate=False`` and ``reason`` beginning with ``blocked``.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    since_value = _normalize_datetime(since) if since is not None else None
    lines = text.splitlines()
    events = _trace_events(lines) + _callback_events(lines)
    events.sort(key=lambda event: event["event_at"])
    if not events:
        return None

    state: dict[str, Any] | None = None
    state_pid: str | None = None
    newest: dict[str, Any] | None = None
    confirmed_used = 0
    for event in events:
        if event["kind"] == "trc":
            batches, errors = _build_batches(
                event["batches"], source="trc", observed_at=event["observed_at"]
            )
            state = _result(
                observed_at=event["observed_at"],
                batches=batches,
                source="trc",
                errors=errors,
                reason="complete TRC reservation; no confirmed consumption",
            )
            state_pid = event["pid"]
        elif event["kind"] == "callback_array":
            details = event["details"]
            batches, errors = _build_batches(
                details.get("medicines") if isinstance(details, dict) else None,
                source="callback",
                observed_at=event["event_at"],
            )
            state = _result(
                observed_at=event["event_at"],
                batches=batches,
                source="callback",
                errors=errors,
                reason="successful UseMedicine callback",
            )
            state_pid = event["pid"] or state_pid
        else:
            details = event["details"]
            if event["pid"] and state_pid and event["pid"] != state_pid:
                state = _result(
                    observed_at=event["event_at"],
                    batches=[],
                    source="callback_count",
                    errors=["count-only callback PID does not match inventory baseline"],
                )
            else:
                state = _apply_count_only(
                    state,
                    details.get("count") if isinstance(details, dict) else None,
                    event["event_at"],
                )
            state_pid = event["pid"] or state_pid
        if since_value is None or event["event_at"] >= since_value:
            if event["kind"] != "trc":
                confirmed_used += _confirmed_used(event)
            state = dict(state)
            state["confirmed_used"] = confirmed_used
            newest = state
    return newest


__all__ = ["parse_medicine_evidence"]
