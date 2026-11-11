"""Byte-level scanners for pure LF line endings, UTF-8 BOM, and secret patterns."""

import os
import re
from pathlib import Path
from typing import List, Tuple, Set, Optional

DEFAULT_TEXT_EXTENSIONS: Set[str] = {
    ".py",
    ".sh",
    ".go",
    ".js",
    ".mjs",
    ".json",
    ".md",
    ".css",
    ".html",
    ".prop",
    ".ps1",
    ".yml",
    ".yaml",
    ".txt",
    ".toml",
}

DEFAULT_EXCLUDE_DIRS: Set[str] = {
    ".git",
    ".agents",
    "__pycache__",
    "node_modules",
    ".codex_diagnostics",
}

DEFAULT_CREDENTIAL_FILENAME_PATTERNS = [
    re.compile(r"(?i)^tg\.txt$"),
    re.compile(r"(?i)^\.env(\..+)?$"),
    re.compile(r"(?i)\.(pem|key|p12|pfx|netrc)$"),
    re.compile(r"(?i)^id_rsa"),
]

DEFAULT_SECRET_CONTENT_PATTERNS = [
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "Private key header"),
    (re.compile(rb"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"), "Telegram bot token"),
    (re.compile(rb"(?i)\bpassword\s*=\s*['\"][^'\"]{6,}['\"]"), "Plaintext password assignment"),
]


def scan_cr_bytes(
    root_dir: str | Path,
    extensions: Optional[Set[str]] = None,
    exclude_dirs: Optional[Set[str]] = None,
) -> List[Tuple[str, int]]:
    """Scan all text files under root_dir for Carriage Return (0x0D, \\r) bytes.

    Returns a list of (file_path, count_of_cr_bytes).
    """
    root_path = Path(root_dir)
    target_exts = extensions or DEFAULT_TEXT_EXTENSIONS
    ignored_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS

    violations: List[Tuple[str, int]] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext in target_exts or fname in {"module.prop", "inventory.json"}:
                fpath = Path(dirpath) / fname
                try:
                    data = fpath.read_bytes()
                except OSError:
                    continue
                cr_count = data.count(b"\r")
                if cr_count > 0:
                    violations.append((str(fpath), cr_count))

    return violations


def scan_utf8_bom(
    root_dir: str | Path,
    extensions: Optional[Set[str]] = None,
    exclude_dirs: Optional[Set[str]] = None,
) -> List[str]:
    """Scan all text files under root_dir for UTF-8 Byte Order Mark (0xEF, 0xBB, 0xBF).

    Returns a list of file paths that start with BOM.
    """
    root_path = Path(root_dir)
    target_exts = extensions or DEFAULT_TEXT_EXTENSIONS
    ignored_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS

    violations: List[str] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext in target_exts or fname in {"module.prop", "inventory.json"}:
                fpath = Path(dirpath) / fname
                try:
                    data = fpath.read_bytes()
                except OSError:
                    continue
                if data.startswith(b"\xef\xbb\xbf"):
                    violations.append(str(fpath))

    return violations


def scan_secrets(
    root_dir: str | Path,
    exclude_files: Optional[Set[str]] = None,
    exclude_dirs: Optional[Set[str]] = None,
) -> List[Tuple[str, str]]:
    """Scan directory tree for potential credentials, private keys, or tokens.

    Returns a list of (file_path, match_description).
    """
    root_path = Path(root_dir)
    ignored_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS
    ignored_files = exclude_files or {
        "scanner.py",
        "build-project.ps1",
        "test_build_project.ps1",
        "test_module_layout.ps1",
        "test_tier1_packaging.py",
        "test_tier1_daemon_webui.py",
    }

    violations: List[Tuple[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fname in ignored_files:
                continue

            # Check filename against credential patterns
            for pat in DEFAULT_CREDENTIAL_FILENAME_PATTERNS:
                if pat.search(fname):
                    violations.append((str(fpath), f"Suspicious credential filename: {fname}"))
                    break

            # Check file content
            try:
                data = fpath.read_bytes()
            except OSError:
                continue

            for pat, desc in DEFAULT_SECRET_CONTENT_PATTERNS:
                if pat.search(data):
                    violations.append((str(fpath), desc))

    return violations
