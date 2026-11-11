"""Base test case with sandboxed environments, mock binary dispatching, and cleanup."""

import os
import sys
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any

from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom
from tests.e2e.common.pwsh import run_pwsh_file, run_pwsh_command


class E2ETestCase(unittest.TestCase):
    """Base class for all E2E test cases, guaranteeing sandboxing and safety."""

    repo_root: Path = Path(__file__).resolve().parent.parent.parent.parent

    def setUp(self) -> None:
        """Create isolated sandbox directory and set up environment variable overrides."""
        self.sandbox = tempfile.mkdtemp(prefix="maa_e2e_")
        self.sandbox_path = Path(self.sandbox)

        # Standard sandboxed directories
        self.home_dir = self.sandbox_path / "home"
        self.config_dir = self.sandbox_path / "config"
        self.data_dir = self.sandbox_path / "data"
        self.state_dir = self.sandbox_path / "state"
        self.log_dir = self.sandbox_path / "log"
        self.cache_dir = self.sandbox_path / "cache"
        self.bin_dir = self.sandbox_path / "bin"

        for d in [
            self.home_dir,
            self.config_dir,
            self.config_dir / "tasks",
            self.data_dir,
            self.state_dir,
            self.state_dir / "debug",
            self.log_dir,
            self.cache_dir,
            self.bin_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

        # Environment configuration
        self.env: Dict[str, str] = os.environ.copy()
        self.env.update({
            "MAA_HOME": str(self.home_dir),
            "MAA_CONFIG_DIR": str(self.config_dir),
            "MAA_DATA_DIR": str(self.data_dir),
            "MAA_STATE_DIR": str(self.state_dir),
            "MAA_LOG_DIR": str(self.log_dir),
            "MAA_CACHE_DIR": str(self.cache_dir),
            "MAA_ADB_ADDR": "127.0.0.1:5555",
            "PATH": f"{self.bin_dir}{os.pathsep}{self.env.get('PATH', '')}",
            "PYTHONPATH": f"{self.repo_root / 'packaging' / 'project' / 'source'}{os.pathsep}{self.repo_root}",
        })

    def tearDown(self) -> None:
        """Safely clean up temporary sandbox."""
        if hasattr(self, "sandbox") and os.path.exists(self.sandbox):
            try:
                shutil.rmtree(self.sandbox, ignore_errors=True)
            except Exception:
                pass

    def create_mock_bin(
        self,
        name: str,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        record_invocations: bool = True,
    ) -> Path:
        """Create mock binary scripts (both Unix and Windows .cmd) in self.bin_dir."""
        log_file = self.bin_dir / f"{name}.log"

        # 1. POSIX shell script (for bash invocations)
        sh_path = self.bin_dir / name
        sh_lines = ["#!/bin/sh"]
        if record_invocations:
            sh_lines.append(f'echo "$@" >> "{log_file.as_posix()}"')
        if stdout:
            sh_lines.append(f'echo "{stdout}"')
        if stderr:
            sh_lines.append(f'echo "{stderr}" >&2')
        sh_lines.append(f"exit {exit_code}\n")
        self.write_file(sh_path, "\n".join(sh_lines))
        try:
            sh_path.chmod(0o755)
        except OSError:
            pass

        # 2. Windows batch script (for Windows cmd / pwsh invocations)
        cmd_path = self.bin_dir / f"{name}.cmd"
        cmd_lines = ["@echo off"]
        if record_invocations:
            cmd_lines.append(f'echo %* >> "{log_file}"')
        if stdout:
            cmd_lines.append(f"echo {stdout}")
        if stderr:
            cmd_lines.append(f"echo {stderr} 1>&2")
        cmd_lines.append(f"exit /b {exit_code}\n")
        self.write_file(cmd_path, "\n".join(cmd_lines))

        return sh_path

    def get_mock_invocations(self, name: str) -> List[str]:
        """Read the logged invocation arguments of a mock binary."""
        log_file = self.bin_dir / f"{name}.log"
        if not log_file.exists():
            return []
        return log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()

    def run_cmd(
        self,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout: int = 30,
        cwd: Optional[str | Path] = None,
    ) -> subprocess.CompletedProcess:
        """Execute a command within the sandboxed environment."""
        merged_env = self.env.copy()
        if env:
            merged_env.update(env)

        executable = shutil.which(cmd[0], path=merged_env.get("PATH"))
        use_shell = False
        exec_cmd = list(cmd)
        if sys.platform == "win32":
            if executable:
                if executable.lower().endswith((".cmd", ".bat")):
                    use_shell = True
                    exec_cmd[0] = executable
                else:
                    exec_cmd[0] = executable
            else:
                use_shell = True

        return subprocess.run(
            exec_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd or self.sandbox_path),
            env=merged_env,
            shell=use_shell,
        )

    def run_python(
        self,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout: int = 30,
        cwd: Optional[str | Path] = None,
    ) -> subprocess.CompletedProcess:
        """Execute Python in non-buffered (-B) mode within the sandboxed environment."""
        cmd = [sys.executable, "-B"] + args
        return self.run_cmd(cmd, env=env, timeout=timeout, cwd=cwd)

    def run_pwsh_file(
        self,
        script_path: str | Path,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: int = 60,
        cwd: Optional[str | Path] = None,
    ) -> subprocess.CompletedProcess:
        """Execute a PowerShell script using PowerShell 7."""
        merged_env = self.env.copy()
        if env:
            merged_env.update(env)
        return run_pwsh_file(
            script_path=script_path,
            args=args,
            env=merged_env,
            timeout=timeout,
            cwd=cwd or self.sandbox_path,
        )

    def write_file(self, path: str | Path, content: str, encoding: str = "utf-8") -> Path:
        """Write text file enforcing pure LF line endings and UTF-8 without BOM."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Ensure only \n is written
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        with open(target, "w", encoding=encoding, newline="\n") as f:
            f.write(normalized)
        return target

    def read_file(self, path: str | Path, encoding: str = "utf-8") -> str:
        """Read text file safely."""
        return Path(path).read_text(encoding=encoding, errors="replace")

    def assert_no_cr(self, path_or_dir: str | Path) -> None:
        """Assert that file or directory contains 0 CR (\\r) bytes."""
        target = Path(path_or_dir)
        if target.is_file():
            data = target.read_bytes()
            cr_count = data.count(b"\r")
            self.assertEqual(cr_count, 0, f"File contains {cr_count} CR bytes: {target}")
        else:
            violations = scan_cr_bytes(target)
            self.assertEqual(len(violations), 0, f"Found CR byte violations in {len(violations)} files: {violations}")

    def assert_no_bom(self, path_or_dir: str | Path) -> None:
        """Assert that file or directory contains 0 UTF-8 BOM prefixes."""
        target = Path(path_or_dir)
        if target.is_file():
            data = target.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"File starts with UTF-8 BOM: {target}")
        else:
            violations = scan_utf8_bom(target)
            self.assertEqual(len(violations), 0, f"Found UTF-8 BOM violations in {len(violations)} files: {violations}")
