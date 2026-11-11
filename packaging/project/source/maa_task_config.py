#!/usr/bin/env python3
"""Shared TOML codec for MAA task configuration files."""

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class StrictTaskConfigError(Exception):
    """A non-lossy task-config loading failure."""

    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _tasks_from_document(data: object) -> list[dict[str, object]]:
    if not isinstance(data, dict):
        raise StrictTaskConfigError("invalid", "任务 TOML 顶层必须是表")
    active = data.get("tasks", [])
    disabled = data.get("disabled_tasks", [])
    if not isinstance(active, list) or not isinstance(disabled, list):
        raise StrictTaskConfigError("invalid", "tasks 和 disabled_tasks 必须是数组")
    if any(not isinstance(task, dict) for task in (*active, *disabled)):
        raise StrictTaskConfigError("invalid", "任务项必须是表")

    active = [dict(task) for task in active]
    disabled = [dict(task) for task in disabled]
    for task in active:
        task["_enabled"] = True
    for task in disabled:
        task["_enabled"] = False

    total_len = len(active) + len(disabled)
    result: list[dict[str, object] | None] = [None] * total_len
    unplaced_disabled = []
    for task in disabled:
        index = task.pop("_original_index", None)
        if (
            type(index) is int
            and 0 <= index < total_len
            and result[index] is None
        ):
            result[index] = task
        else:
            unplaced_disabled.append(task)

    active_index = 0
    for index in range(total_len):
        if result[index] is None and active_index < len(active):
            result[index] = active[active_index]
            active_index += 1

    merged = [task for task in result if task is not None]
    merged.extend(unplaced_disabled)
    for task in merged:
        task.pop("_original_index", None)
    return merged


def load_tasks_strict(path: Path) -> list[dict[str, object]]:
    """Load a TOML task file without converting any failure into an empty list."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise StrictTaskConfigError("missing", "任务 TOML 不存在") from exc
    except OSError as exc:
        raise StrictTaskConfigError("unreadable", "任务 TOML 无法读取") from exc
    if not raw.strip():
        raise StrictTaskConfigError("empty", "任务 TOML 为空")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise StrictTaskConfigError("invalid", f"任务 TOML 无效: {exc}") from exc
    return _tasks_from_document(data)


def _atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp_file:
            temp_file.write(content.encode("utf-8"))
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


def load_tasks(path: Path) -> list[dict[str, object]]:
    """Read a task TOML and merge active and disabled tasks in display order."""
    try:
        raw = Path(path).read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
        return _tasks_from_document(data)
    except Exception as exc:
        import sys

        print(f"Error loading tasks: {exc}", file=sys.stderr)
        return []


def _toml_val(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_val(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{key} = {_toml_val(item)}" for key, item in value.items())
        return "{ " + items + " }"
    return json.dumps(str(value))


def render_tasks(tasks: list[dict[str, object]], generated_at: str | None = None) -> str:
    lines = [
        "# MAA 日常任务配置 (由 WebUI 生成)",
        f"# 更新时间: {generated_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    def clean_task(task, index=None):
        cleaned = {key: value for key, value in task.items() if not key.startswith("_")}
        if index is not None:
            cleaned["_original_index"] = index
        return cleaned

    active_tasks = []
    disabled_tasks = []
    for index, task in enumerate(tasks):
        if task.get("_enabled") is False:
            disabled_tasks.append(clean_task(task, index))
        else:
            active_tasks.append(clean_task(task))

    def format_task(task, array_name):
        task_lines = [f"[[{array_name}]]"]
        if "_original_index" in task:
            task_lines.append(f'_original_index = {task["_original_index"]}')
        if task.get("name"):
            task_lines.append(f'name = {_toml_val(task["name"])}')
        task_lines.append(f'type = {_toml_val(task["type"])}')
        if task.get("strategy"):
            task_lines.append(f'strategy = {_toml_val(task["strategy"])}')

        params = task.get("params", {})
        variants = task.get("variants", [])
        if params and not variants:
            if len(params) <= 3 and all(
                not isinstance(value, (dict, list))
                or (isinstance(value, list) and len(value) <= 5)
                for value in params.values()
            ):
                task_lines.append(f"params = {_toml_val(params)}")
            else:
                task_lines.append("")
                task_lines.append(f"[{array_name}.params]")
                for key, value in params.items():
                    task_lines.append(f"{key} = {_toml_val(value)}")

        if variants:
            if params:
                task_lines.append("")
                task_lines.append(f"[{array_name}.params]")
                for key, value in params.items():
                    task_lines.append(f"{key} = {_toml_val(value)}")
            for variant in variants:
                task_lines.append("")
                task_lines.append(f"[[{array_name}.variants]]")
                condition = variant.get("condition")
                if condition:
                    task_lines.append(f"condition = {_toml_val(condition)}")
                variant_params = variant.get("params", {})
                if variant_params:
                    task_lines.append(f"params = {_toml_val(variant_params)}")
        task_lines.append("")
        return task_lines

    for task in active_tasks:
        lines.extend(format_task(task, "tasks"))
    for task in disabled_tasks:
        lines.extend(format_task(task, "disabled_tasks"))
    return "\n".join(lines)


def save_tasks(
    tasks: list[dict[str, object]], path: Path, generated_at: str | None = None
) -> bool:
    content = render_tasks(tasks, generated_at=generated_at)
    try:
        _atomic_write_text(Path(path), content)
        return True
    except OSError:
        return False


def validate_task_bytes(raw: bytes) -> tuple[bool, str]:
    if not raw.strip():
        return False, "empty"
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"invalid_toml:{exc}"
    if not (data.get("tasks") or data.get("disabled_tasks")):
        return False, "no_tasks"
    return True, ""
