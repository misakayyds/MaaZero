#!/usr/bin/env python3
"""Build and consume non-blocking RIIC skill unlock recommendations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import urllib.request


SCHEMA_VERSION = 1
SOURCE_REPOSITORY = "yuanyan3060/ArknightsGameResource"
SOURCE_BASE_URL = (
    "https://raw.githubusercontent.com/yuanyan3060/"
    "ArknightsGameResource/main/gamedata/excel"
)
BUILDING_URL = f"{SOURCE_BASE_URL}/building_data.json"
CHARACTER_URL = f"{SOURCE_BASE_URL}/character_table.json"
ROOM_TYPES = {
    "CONTROL": "control",
    "POWER": "power",
    "MANUFACTURE": "manufacture",
    "TRADING": "trading",
    "MEETING": "meeting",
    "HIRE": "hire",
    "DORMITORY": "dormitory",
    "WORKSHOP": "processing",
}


def _iso_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _requirement_key(value: dict[str, object]) -> tuple[int, int]:
    return int(value["elite"]), int(value["level"])


def build_requirement_index(
    building_data: dict[str, object],
    character_table: dict[str, object],
    *,
    source_ref: str = "main",
) -> dict[str, object]:
    chars = building_data.get("chars")
    buffs = building_data.get("buffs")
    if not isinstance(chars, dict) or not isinstance(buffs, dict):
        raise ValueError("building_data 缺少 chars/buffs")
    operators: dict[str, dict[str, dict[str, object]]] = {}
    for char_id, char_skills in chars.items():
        character = character_table.get(char_id)
        if not isinstance(character, dict) or not isinstance(character.get("name"), str):
            continue
        name = character["name"].strip()
        if not name or not isinstance(char_skills, dict):
            continue
        facility_requirements = operators.setdefault(name, {})
        for group in char_skills.get("buffChar", []):
            if not isinstance(group, dict):
                continue
            for skill in group.get("buffData", []):
                if not isinstance(skill, dict):
                    continue
                buff = buffs.get(skill.get("buffId"))
                cond = skill.get("cond")
                if not isinstance(buff, dict) or not isinstance(cond, dict):
                    continue
                facility = ROOM_TYPES.get(buff.get("roomType"))
                elite, level = cond.get("phase"), cond.get("level")
                if facility is None or type(elite) is not int or type(level) is not int:
                    continue
                candidate = {
                    "elite": elite,
                    "level": level,
                    "skills": [
                        buff["buffName"]
                    ] if isinstance(buff.get("buffName"), str) and buff["buffName"] else [],
                }
                current = facility_requirements.get(facility)
                if current is None or _requirement_key(candidate) > _requirement_key(current):
                    facility_requirements[facility] = candidate
                elif _requirement_key(candidate) == _requirement_key(current):
                    current["skills"] = sorted(
                        set(current.get("skills", [])) | set(candidate["skills"])
                    )
        if not facility_requirements:
            operators.pop(name, None)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_utc(),
        "source": {
            "repository": SOURCE_REPOSITORY,
            "ref": source_ref,
        },
        "operators": operators,
    }


def validate_requirement_index(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("基建门槛索引版本无效")
    if not isinstance(value.get("source"), dict) or not isinstance(
        value.get("operators"), dict
    ):
        raise ValueError("基建门槛索引结构无效")
    for name, facilities in value["operators"].items():
        if not isinstance(name, str) or not name or not isinstance(facilities, dict):
            raise ValueError("基建门槛干员结构无效")
        for facility, requirement in facilities.items():
            if facility not in ROOM_TYPES.values() or not isinstance(requirement, dict):
                raise ValueError("基建门槛设施结构无效")
            elite, level = requirement.get("elite"), requirement.get("level")
            if type(elite) is not int or elite not in (0, 1, 2):
                raise ValueError("基建门槛精英等级无效")
            if type(level) is not int or level < 1:
                raise ValueError("基建门槛等级无效")
            if not isinstance(requirement.get("skills", []), list):
                raise ValueError("基建门槛技能列表无效")
    return value


def save_requirement_index(path: Path, value: dict[str, object]) -> None:
    _atomic_write_json(path, validate_requirement_index(value))


def load_requirement_index(path: Path) -> dict[str, object]:
    try:
        value = json.loads(Path(path).read_text("utf-8"))
        index = validate_requirement_index(value)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"available": False, "index": None}
    return {
        "available": True,
        "index": index,
        "generated_at": index.get("generated_at"),
        "source": index.get("source"),
    }


def _add_facility(
    result: dict[str, set[str]], names: object, facility: str
) -> None:
    if isinstance(names, list):
        for name in names:
            if isinstance(name, str) and name:
                result.setdefault(name, set()).add(facility)


def collect_plan_facilities(
    details: dict[str, object], plan_indexes: list[int] | None = None
) -> dict[str, list[str]]:
    selected = set(plan_indexes) if plan_indexes is not None else None
    result: dict[str, set[str]] = {}
    plans = details.get("plans", [])
    if not isinstance(plans, list):
        return {}
    for position, plan in enumerate(plans):
        if not isinstance(plan, dict):
            continue
        if selected is not None and plan.get("index", position) not in selected:
            continue
        groups = {
            group.get("name"): group.get("operators", [])
            for group in plan.get("groups", [])
            if isinstance(group, dict) and isinstance(group.get("name"), str)
        }
        rooms = plan.get("rooms", {})
        if not isinstance(rooms, dict):
            continue
        for facility, facility_rooms in rooms.items():
            if facility not in ROOM_TYPES.values() or not isinstance(facility_rooms, list):
                continue
            for room in facility_rooms:
                if not isinstance(room, dict):
                    continue
                names = room.get("operators", [])
                if room.get("use_operator_groups") is True and isinstance(names, list):
                    for group_name in names:
                        _add_facility(result, groups.get(group_name), facility)
                else:
                    _add_facility(result, names, facility)
    return {name: sorted(facilities) for name, facilities in sorted(result.items())}


def compare_training(
    details: dict[str, object],
    snapshot: dict[str, object],
    index: dict[str, object] | None,
    plan_indexes: list[int] | None = None,
) -> dict[str, object]:
    facilities_by_name = collect_plan_facilities(details, plan_indexes)
    owned = {
        item.get("name"): item
        for item in snapshot.get("own_opers", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    requirements_by_name = (
        index.get("operators", {}) if isinstance(index, dict) else {}
    )
    rows = []
    counts = {"ready": 0, "below": 0, "missing": 0, "unknown": 0}
    for name, facilities in facilities_by_name.items():
        current = owned.get(name)
        applicable = []
        operator_requirements = requirements_by_name.get(name, {})
        if isinstance(operator_requirements, dict):
            applicable = [
                operator_requirements[facility]
                for facility in facilities
                if isinstance(operator_requirements.get(facility), dict)
            ]
        recommended = max(applicable, key=_requirement_key) if applicable else None
        if current is None:
            status = "missing"
        elif recommended is None:
            status = "unknown"
        else:
            current_key = (int(current.get("elite", 0)), int(current.get("level", 0)))
            status = "ready" if current_key >= _requirement_key(recommended) else "below"
        counts[status] += 1
        rows.append(
            {
                "name": name,
                "facilities": facilities,
                "status": status,
                "current": (
                    {
                        "elite": int(current.get("elite", 0)),
                        "level": int(current.get("level", 0)),
                        "potential": int(current.get("potential", 0)),
                    }
                    if current is not None
                    else None
                ),
                "recommended": (
                    {
                        "elite": recommended["elite"],
                        "level": recommended["level"],
                    }
                    if recommended is not None
                    else None
                ),
            }
        )
    priority = {"missing": 0, "below": 1, "unknown": 2, "ready": 3}
    rows.sort(key=lambda item: (priority[item["status"]], item["name"]))
    return {
        "available": index is not None,
        "blocking": False,
        "label": "完整基建技能建议门槛",
        "summary": {"total": len(rows), **counts},
        "operators": rows,
    }


def _fetch_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "maa-infrast-hints/1"})
    with urllib.request.urlopen(request, timeout=45) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("上游资源不是 JSON 对象")
    return value


def refresh_requirement_index(
    output: Path,
    *,
    fetch_json=_fetch_json,
    source_ref: str = "main",
) -> dict[str, object]:
    building = fetch_json(BUILDING_URL)
    characters = fetch_json(CHARACTER_URL)
    value = build_requirement_index(building, characters, source_ref=source_ref)
    save_requirement_index(output, value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--building-data", type=Path)
    parser.add_argument("--character-table", type=Path)
    parser.add_argument("--source-ref", default="main")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    if args.update:
        value = refresh_requirement_index(args.output, source_ref=args.source_ref)
    elif args.building_data and args.character_table:
        value = build_requirement_index(
            json.loads(args.building_data.read_text("utf-8")),
            json.loads(args.character_table.read_text("utf-8")),
            source_ref=args.source_ref,
        )
        save_requirement_index(args.output, value)
    else:
        parser.error("使用 --update 或同时提供两个本地数据文件")
    print(f"operators={len(value['operators'])} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
