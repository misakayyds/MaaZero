#!/usr/bin/env python3
"""Run OperBox through the existing MAA runner and persist only complete scans."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from maa_operator_box import (
    OperatorBoxError,
    extract_final_callback_from_sources,
    save_failed_attempt,
    save_successful_scan,
)


SCRIPT_DIR = Path("/root/maa-scripts")
CONFIG_DIR = Path("/root/.config/maa")
RESOURCE_DIR = Path("/root/.local/share/maa/resource")
ASST_LOG_PATH = Path("/root/.local/state/maa/debug/asst.log")
TASK_TEXT = """[[tasks]]
name = "开始唤醒"
type = "StartUp"
params = { client_type = "Official", start_game_enabled = true }

[[tasks]]
name = "干员扫描"
type = "OperBox"
"""


def atomic_task_file() -> None:
    path = CONFIG_DIR / "tasks" / "operator-scan.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text("utf-8") == TASK_TEXT:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(TASK_TEXT, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def maa_version() -> str:
    try:
        result = subprocess.run(
            ["/root/.local/bin/maa", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def resource_version() -> str:
    try:
        return datetime.fromtimestamp(
            RESOURCE_DIR.stat().st_mtime, timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except OSError:
        return ""


def main() -> int:
    atomic_task_file()
    scan_started_ns = time.time_ns()
    process = subprocess.Popen(
        [
            "/bin/bash",
            str(SCRIPT_DIR / "maa-run.sh"),
            "operator-scan",
            "--debug",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output.append(line)
    return_code = process.wait()
    log_text = "".join(output)
    snapshot_path = CONFIG_DIR / "operator-box.json"
    state_path = CONFIG_DIR / "operator-box-state.json"
    try:
        if return_code != 0:
            raise OperatorBoxError(
                "runner_failed", "$", f"扫描执行失败，exit={return_code}"
            )
        core_log = ""
        try:
            if ASST_LOG_PATH.stat().st_mtime_ns >= scan_started_ns:
                core_log = ASST_LOG_PATH.read_text("utf-8", errors="replace")
        except OSError:
            pass
        callback, callback_source = extract_final_callback_from_sources(
            log_text, core_log
        )
        source_log = log_text
        if callback_source == "maacore_debug":
            source_log += (
                "\n--- MAA_CORE_FINAL_CALLBACK ---\n"
                + json.dumps(
                    callback,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        save_successful_scan(
            snapshot_path,
            state_path,
            callback,
            source_log=source_log,
            core_version=maa_version(),
            resource_version=resource_version(),
        )
        print(f"--- OPERATOR_CALLBACK_SOURCE: {callback_source} ---")
        print("--- OPERATOR_SNAPSHOT_SAVED ---")
        return 0
    except OperatorBoxError as error:
        save_failed_attempt(
            state_path,
            code=error.code,
            message=error.message,
            source_log=log_text,
        )
        print(f"--- OPERATOR_SCAN_FAILED: {error.code} ---", file=sys.stderr)
        return 2
    except Exception:
        save_failed_attempt(
            state_path,
            code="scan_failed",
            message="扫描结果处理失败",
            source_log=log_text,
        )
        print("--- OPERATOR_SCAN_FAILED: scan_failed ---", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
