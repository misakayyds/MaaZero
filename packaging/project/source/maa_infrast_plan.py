#!/usr/bin/env python3
"""Safe loading and validation for MAA custom infrastructure plans."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import NoReturn


MAX_PLAN_BYTES = 1_048_576

KNOWN_FACILITIES = {
    "control",
    "manufacture",
    "trading",
    "power",
    "meeting",
    "hire",
    "dormitory",
    "processing",
}
KNOWN_PRODUCTS = {
    "Battle Record",
    "Pure Gold",
    "Dualchip",
    "Originium Shard",
    "LMD",
    "Orundum",
}
KNOWN_ORDERS = {"pre", "post"}
KNOWN_DRONE_ROOMS = {"trading", "manufacture"}


@dataclass(frozen=True)
class PlanValidationError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class PlanImportError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _schema_error(path: str, message: str) -> NoReturn:
    raise PlanValidationError("invalid_schema", path, message)


def _require_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _schema_error(path, "必须是对象")
    return value


def _require_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        _schema_error(path, "必须是数组")
    return value


def _optional_string(
    value: dict[str, object], key: str, path: str, *, allow_empty: bool = True
) -> str | None:
    if key not in value:
        return None
    result = value[key]
    if not isinstance(result, str) or (not allow_empty and not result):
        _schema_error(path, "必须是字符串")
    return result


def _optional_bool(value: dict[str, object], key: str, path: str) -> bool | None:
    if key not in value:
        return None
    result = value[key]
    if type(result) is not bool:
        _schema_error(path, "必须是布尔值")
    return result


def _validate_string_list(value: object, path: str) -> list[object]:
    items = _require_list(value, path)
    if any(not isinstance(item, str) or not item for item in items):
        _schema_error(path, "必须是非空字符串数组")
    return items


def _validate_groups(plan: dict[str, object], plan_path: str) -> set[str]:
    if "groups" not in plan:
        return set()

    groups_path = f"{plan_path}.groups"
    groups = _require_list(plan["groups"], groups_path)
    names: set[str] = set()
    for group_index, group_value in enumerate(groups):
        group_path = f"{groups_path}[{group_index}]"
        group = _require_object(group_value, group_path)
        name = _optional_string(
            group, "name", f"{group_path}.name", allow_empty=False
        )
        if name is None:
            _schema_error(f"{group_path}.name", "缺少干员组名称")
        if name in names:
            _schema_error(f"{group_path}.name", "干员组名称不能重复")
        names.add(name)
        if "operators" not in group:
            _schema_error(f"{group_path}.operators", "缺少干员列表")
        _validate_string_list(group["operators"], f"{group_path}.operators")
    return names


def _validate_room(
    room_value: object, room_path: str, group_names: set[str]
) -> None:
    room = _require_object(room_value, room_path)
    for field in ("operators", "candidates"):
        if field in room:
            _validate_string_list(room[field], f"{room_path}.{field}")
    for field in ("skip", "autofill", "sort", "use_operator_groups"):
        _optional_bool(room, field, f"{room_path}.{field}")

    if "product" in room:
        product = _optional_string(room, "product", f"{room_path}.product")
        if product not in KNOWN_PRODUCTS:
            _schema_error(f"{room_path}.product", "product 不是已知产品")

    if room.get("use_operator_groups", False):
        for operator_index, reference in enumerate(room.get("operators", [])):
            if reference not in group_names:
                _schema_error(
                    f"{room_path}.operators[{operator_index}]",
                    "启用干员组时 operators 必须引用已定义的组名",
                )


def _validate_fiammetta(plan: dict[str, object], plan_path: str) -> None:
    if "Fiammetta" not in plan:
        return
    path = f"{plan_path}.Fiammetta"
    value = _require_object(plan["Fiammetta"], path)
    _optional_bool(value, "enable", f"{path}.enable")
    _optional_string(value, "target", f"{path}.target")
    order = _optional_string(value, "order", f"{path}.order")
    if order is not None and order not in KNOWN_ORDERS:
        _schema_error(f"{path}.order", "order 必须是 pre 或 post")


def _validate_drones(plan: dict[str, object], plan_path: str) -> None:
    if "drones" not in plan:
        return
    path = f"{plan_path}.drones"
    value = _require_object(plan["drones"], path)
    _optional_bool(value, "enable", f"{path}.enable")

    room = _optional_string(value, "room", f"{path}.room")
    if room is not None and room not in KNOWN_DRONE_ROOMS:
        _schema_error(f"{path}.room", "room 必须是 trading 或 manufacture")

    if "index" in value:
        index = value["index"]
        if type(index) is not int or not 1 <= index <= 5:
            _schema_error(f"{path}.index", "index 必须是 1 到 5 的整数")

    order = _optional_string(value, "order", f"{path}.order")
    if order is not None and order not in KNOWN_ORDERS:
        _schema_error(f"{path}.order", "order 必须是 pre 或 post")


def _validate_document(document: object) -> None:
    root = _require_object(document, "$")
    _optional_string(root, "title", "$.title")
    _optional_string(root, "author", "$.author")
    _optional_string(root, "description", "$.description")

    if "plans" not in root:
        _schema_error("$.plans", "缺少 plans")
    plans = _require_list(root["plans"], "$.plans")
    if not plans:
        _schema_error("$.plans", "plans 不能为空")

    for plan_index, plan_value in enumerate(plans):
        plan_path = f"$.plans[{plan_index}]"
        plan = _require_object(plan_value, plan_path)
        _optional_string(plan, "name", f"{plan_path}.name")
        _optional_string(plan, "description", f"{plan_path}.description")
        group_names = _validate_groups(plan, plan_path)

        if "rooms" not in plan:
            _schema_error(f"{plan_path}.rooms", "缺少 rooms")
        rooms_path = f"{plan_path}.rooms"
        rooms = _require_object(plan["rooms"], rooms_path)
        for facility, room_values in rooms.items():
            facility_path = f"{rooms_path}.{facility}"
            if facility not in KNOWN_FACILITIES:
                _schema_error(facility_path, "未知设施类型")
            for room_index, room_value in enumerate(
                _require_list(room_values, facility_path)
            ):
                _validate_room(
                    room_value,
                    f"{facility_path}[{room_index}]",
                    group_names,
                )

        _validate_fiammetta(plan, plan_path)
        _validate_drones(plan, plan_path)


def _validate_plan_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".json")
    ):
        raise PlanValidationError(
            "invalid_filename", "$", "文件名必须是单个 .json 文件名"
        )


def _resolve_plan_path(infrast_dir: Path, filename: str) -> Path:
    _validate_plan_filename(filename)
    try:
        base = Path(infrast_dir).resolve()
        candidate = base / filename
        resolved = candidate.resolve(strict=False)
    except RuntimeError as exc:
        raise PlanValidationError(
            "invalid_filename", "$", "文件路径无法安全解析"
        ) from exc
    if resolved != base and base not in resolved.parents:
        raise PlanValidationError(
            "invalid_filename", "$", "文件路径超出基建排班目录"
        )
    return resolved


def read_plan_bytes(
    infrast_dir: Path, filename: str, max_bytes: int = MAX_PLAN_BYTES
) -> bytes:
    path = _resolve_plan_path(infrast_dir, filename)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise PlanValidationError("file_not_found", "$", "基建排班文件不存在") from exc
    except IsADirectoryError as exc:
        raise PlanValidationError(
            "file_not_regular", "$", "基建排班路径不是普通文件"
        ) from exc
    except PermissionError as exc:
        # Windows os.open cannot obtain a read-only fd for a directory. This
        # fallback only classifies that platform limitation; other permission
        # failures remain unexpected OSError instances and propagate.
        if os.name == "nt" and path.is_dir():
            raise PlanValidationError(
                "file_not_regular", "$", "基建排班路径不是普通文件"
            ) from exc
        raise

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PlanValidationError(
                "file_not_regular", "$", "基建排班路径不是普通文件"
            )
        if file_stat.st_size > max_bytes:
            raise PlanValidationError(
                "file_too_large", "$", "基建排班文件超过大小限制"
            )

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise PlanValidationError("file_too_large", "$", "基建排班文件超过大小限制")
    return raw


def parse_plan_document(raw: bytes, filename: str) -> dict[str, object]:
    del filename
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlanValidationError(
            "invalid_utf8", "$", "文件不是有效的 UTF-8 JSON"
        ) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError("invalid_json", "$", "JSON 解析失败") from exc
    _validate_document(document)
    return document


def _normalize_room(room: dict[str, object], index: int) -> dict[str, object]:
    result: dict[str, object] = {
        "index": index,
        "operators": list(room.get("operators", [])),
        "candidates": list(room.get("candidates", [])),
        "skip": room.get("skip", False),
        "autofill": room.get("autofill", False),
        "sort": room.get("sort", False),
        "use_operator_groups": room.get("use_operator_groups", False),
    }
    if "product" in room:
        result["product"] = room["product"]
    return result


def _normalize_groups(plan: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "name": group["name"],
            "operators": list(group["operators"]),
        }
        for group in plan.get("groups", [])
    ]


def _normalize_options(
    source: object, optional_fields: tuple[str, ...]
) -> dict[str, object]:
    value = source if isinstance(source, dict) else {}
    result: dict[str, object] = {"enable": value.get("enable", False)}
    for field in optional_fields:
        if field in value:
            result[field] = value[field]
    return result


def _normalize_plan(plan: dict[str, object], index: int) -> dict[str, object]:
    normalized_rooms: dict[str, list[dict[str, object]]] = {}
    room_count = 0
    operator_count = 0
    for facility, source_rooms in plan["rooms"].items():
        rooms = [
            _normalize_room(room, room_index)
            for room_index, room in enumerate(source_rooms)
        ]
        normalized_rooms[facility] = rooms
        room_count += len(rooms)
        operator_count += sum(
            len(room["operators"]) + len(room["candidates"]) for room in rooms
        )

    return {
        "index": index,
        "name": plan["name"] if "name" in plan else f"计划 {index + 1}",
        "description": plan.get("description", ""),
        "facility_count": len(normalized_rooms),
        "room_count": room_count,
        "operator_count": operator_count,
        "groups": _normalize_groups(plan),
        "Fiammetta": _normalize_options(
            plan.get("Fiammetta"), ("target", "order")
        ),
        "drones": _normalize_options(
            plan.get("drones"), ("room", "index", "order")
        ),
        "rooms": normalized_rooms,
    }


def _base_plan_payload(
    document: dict[str, object], filename: str, raw: bytes
) -> dict[str, object]:
    result: dict[str, object] = {
        "filename": filename,
        "valid": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "title": document.get("title") or filename,
    }
    for field in ("author", "description"):
        if field in document:
            result[field] = document[field]
    return result


def _plan_details_from_bytes(raw: bytes, filename: str) -> dict[str, object]:
    document = parse_plan_document(raw, filename)
    result = _base_plan_payload(document, filename, raw)
    result["plan_count"] = len(document["plans"])
    result["plans"] = [
        _normalize_plan(plan, index)
        for index, plan in enumerate(document["plans"])
    ]
    return result


def summarize_plan_document(
    document: dict[str, object], filename: str, raw: bytes
) -> dict[str, object]:
    normalized_plans = [
        _normalize_plan(plan, index)
        for index, plan in enumerate(document["plans"])
    ]
    result = _base_plan_payload(document, filename, raw)
    result["plan_count"] = len(normalized_plans)
    result["plans"] = [
        {
            field: plan[field]
            for field in (
                "index",
                "name",
                "description",
                "facility_count",
                "room_count",
                "operator_count",
            )
        }
        for plan in normalized_plans
    ]
    return result


def get_plan_details(infrast_dir: Path, filename: str) -> dict[str, object]:
    raw = read_plan_bytes(infrast_dir, filename)
    return _plan_details_from_bytes(raw, filename)


def _encode_import_content(content: str) -> bytes:
    if not isinstance(content, str):
        raise PlanImportError("invalid_request", "content 必须是字符串")
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlanImportError(
            "invalid_request", "content 不是有效的 Unicode 字符串"
        ) from exc
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanValidationError("file_too_large", "$", "基建排班文件超过大小限制")
    return raw


def _resolve_import_target(infrast_dir: Path, filename: str) -> Path:
    _validate_plan_filename(filename)
    try:
        return Path(infrast_dir).resolve() / filename
    except RuntimeError as exc:
        raise PlanValidationError(
            "invalid_filename", "$", "文件路径无法安全解析"
        ) from exc


def _read_import_target_bytes(target: Path) -> bytes | None:
    try:
        target_stat = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(target_stat.st_mode):
        raise PlanValidationError(
            "file_not_regular", "$", "基建排班路径不是普通文件"
        )
    if target_stat.st_size > MAX_PLAN_BYTES:
        raise PlanValidationError(
            "file_too_large", "$", "基建排班文件超过大小限制"
        )

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | no_follow
    )
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        return None
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PlanValidationError(
                "file_not_regular", "$", "基建排班路径不是普通文件"
            )
        if no_follow == 0 and (
            target_stat.st_dev != file_stat.st_dev
            or target_stat.st_ino != file_stat.st_ino
        ):
            raise PlanValidationError(
                "file_not_regular", "$", "基建排班目标在读取时发生变化"
            )
        if file_stat.st_size > MAX_PLAN_BYTES:
            raise PlanValidationError(
                "file_too_large", "$", "基建排班文件超过大小限制"
            )

        chunks: list[bytes] = []
        remaining = MAX_PLAN_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanValidationError(
            "file_too_large", "$", "基建排班文件超过大小限制"
        )
    return raw


def preview_plan_import(
    infrast_dir: Path,
    filename: str,
    content: str,
) -> dict[str, object]:
    raw = _encode_import_content(content)
    target = _resolve_import_target(infrast_dir, filename)
    current_raw = _read_import_target_bytes(target)
    details = _plan_details_from_bytes(raw, filename)
    sha256 = hashlib.sha256(raw).hexdigest()
    current_sha256 = (
        hashlib.sha256(current_raw).hexdigest()
        if current_raw is not None
        else None
    )
    state = (
        "new"
        if current_raw is None
        else "identical"
        if current_sha256 == sha256
        else "conflict"
    )
    return {
        "filename": filename,
        "sha256": sha256,
        "size": len(raw),
        "state": state,
        "current_sha256": current_sha256,
        "details": details,
    }


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _import_result(
    filename: str,
    sha256: str,
    state: str,
    written: bool,
    details: dict[str, object],
) -> dict[str, object]:
    return {
        "ok": True,
        "filename": filename,
        "sha256": sha256,
        "state": state,
        "written": written,
        "details": details,
    }


def _remove_import_temp(temp_path: Path | None) -> None:
    if temp_path is None:
        return
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass


def _cleanup_import_temp(temp_path: Path | None) -> None:
    try:
        _remove_import_temp(temp_path)
    except OSError as exc:
        raise PlanImportError("write_failed", "清理基建排班临时文件失败") from exc


def commit_plan_import(
    infrast_dir: Path,
    filename: str,
    content: str,
    preview_sha256: str,
    expected_current_sha256: str | None,
    replace: bool,
) -> dict[str, object]:
    if not _is_sha256(preview_sha256):
        raise PlanImportError("invalid_request", "preview_sha256 格式无效")
    if expected_current_sha256 is not None and not _is_sha256(
        expected_current_sha256
    ):
        raise PlanImportError(
            "invalid_request", "expected_current_sha256 格式无效"
        )
    if type(replace) is not bool:
        raise PlanImportError("invalid_request", "replace 必须是布尔值")

    target = _resolve_import_target(infrast_dir, filename)
    raw = _encode_import_content(content)
    details = _plan_details_from_bytes(raw, filename)
    sha256 = hashlib.sha256(raw).hexdigest()
    if sha256 != preview_sha256:
        raise PlanImportError("preview_changed", "导入内容已变化，请重新预览")

    current_raw = _read_import_target_bytes(target)
    current_sha256 = (
        hashlib.sha256(current_raw).hexdigest()
        if current_raw is not None
        else None
    )
    if current_sha256 == sha256:
        return _import_result(filename, sha256, "identical", False, details)
    if current_sha256 != expected_current_sha256:
        raise PlanImportError("stale_preview", "目标文件已变化，请重新预览")
    if current_raw is not None and replace is not True:
        raise PlanImportError("conflict", "同名文件已存在，需要确认覆盖")

    temp_path: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=target.parent
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(raw)
            temp_file.flush()
            os.fsync(temp_file.fileno())
    except OSError as exc:
        _cleanup_import_temp(temp_path)
        raise PlanImportError("write_failed", "写入基建排班失败") from exc

    try:
        final_raw = _read_import_target_bytes(target)
        final_sha256 = (
            hashlib.sha256(final_raw).hexdigest()
            if final_raw is not None
            else None
        )
        if final_sha256 != current_sha256:
            raise PlanImportError("stale_preview", "目标文件已变化，请重新预览")
        try:
            os.replace(temp_path, target)
        except OSError as exc:
            raise PlanImportError("write_failed", "写入基建排班失败") from exc
    finally:
        _cleanup_import_temp(temp_path)

    return _import_result(filename, sha256, "imported", True, details)


def list_plan_summaries(infrast_dir: Path) -> list[dict[str, object]]:
    directory = Path(infrast_dir)
    try:
        directory_entries = list(directory.iterdir())
    except FileNotFoundError:
        return []

    entries = sorted(
        (
            entry
            for entry in directory_entries
            if entry.name.lower().endswith(".json")
        ),
        key=lambda entry: (entry.name.casefold(), entry.name),
    )
    results: list[dict[str, object]] = []
    for entry in entries:
        try:
            details = get_plan_details(directory, entry.name)
        except PlanValidationError as error:
            results.append(
                {
                    "filename": entry.name,
                    "valid": False,
                    "error": {
                        "code": error.code,
                        "path": error.path,
                        "message": error.message,
                    },
                }
            )
            continue

        results.append(
            {
                key: value
                for key, value in details.items()
                if key not in {"plans"}
            }
            | {
                "plans": [
                    {
                        field: plan[field]
                        for field in (
                            "index",
                            "name",
                            "description",
                            "facility_count",
                            "room_count",
                            "operator_count",
                        )
                    }
                    for plan in details["plans"]
                ]
            }
        )
    return results
