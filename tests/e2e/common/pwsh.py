"""PowerShell 7 runner with cross-platform fallback for E2E tests."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any


def find_pwsh() -> str:
    """Locate the PowerShell 7 (pwsh) executable, or fall back to system powershell."""
    env_pwsh = os.environ.get("PWSH_PATH")
    if env_pwsh and os.path.isfile(env_pwsh):
        return env_pwsh

    for candidate in ["pwsh", "pwsh.exe", "powershell", "powershell.exe"]:
        path = shutil.which(candidate)
        if path:
            return path

    # Check typical Windows installation locations
    typical_paths = [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Program Files (x86)\PowerShell\7\pwsh.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ]
    for p in typical_paths:
        if os.path.isfile(p):
            return p

    raise RuntimeError("Neither pwsh (PowerShell 7) nor powershell was found in PATH.")


def run_pwsh_file(
    script_path: str | Path,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    cwd: Optional[str | Path] = None,
) -> subprocess.CompletedProcess:
    """Execute a PowerShell script file with standard bypass arguments."""
    pwsh_bin = find_pwsh()
    cmd = [
        pwsh_bin,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    if args:
        cmd.extend(args)

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=run_env,
    )


def run_pwsh_command(
    command: str,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    cwd: Optional[str | Path] = None,
) -> subprocess.CompletedProcess:
    """Execute an inline PowerShell command."""
    pwsh_bin = find_pwsh()
    cmd = [
        pwsh_bin,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
    ]

    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=run_env,
    )
