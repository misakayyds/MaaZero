#!/usr/bin/env python3
"""Deterministic packager for maa-phone-project component archive.

Archives flat scripts from dist/project/project/* directly into
dist/maa-phone-project-<version>.tar.gz with normalized mtime, root
ownership, and POSIX permissions (0755 for .sh, 0644 for others).
"""

import argparse
import gzip
import hashlib
import os
from pathlib import Path
import sys
import tarfile

sys.dont_write_bytecode = True

DEFAULT_MTIME = 978307200  # 2001-01-01 00:00:00 UTC
ALLOWED_EXTENSIONS = {".py", ".sh", ".js", ".json"}


def package_project_archive(src_dir: Path, out_file: Path, mtime: int = DEFAULT_MTIME) -> tuple[int, str]:
    if not src_dir.is_dir():
        raise FileNotFoundError(f"source directory not found: {src_dir}")

    entries = sorted(os.listdir(src_dir))
    if not entries:
        raise ValueError(f"source directory is empty: {src_dir}")

    out_file.parent.mkdir(parents=True, exist_ok=True)

    with open(out_file, "wb") as f_out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f_out, mtime=float(mtime)) as gz_out:
            with tarfile.open(mode="w", fileobj=gz_out, format=tarfile.GNU_FORMAT) as tar:
                for name in entries:
                    entry_path = src_dir / name
                    if not entry_path.is_file():
                        raise ValueError(f"project archive must be flat, found non-file: {entry_path}")
                    suffix = entry_path.suffix.lower()
                    if suffix not in ALLOWED_EXTENSIONS:
                        raise ValueError(f"unsupported project extension {suffix}: {entry_path}")

                    ti = tarfile.TarInfo(name=name)
                    ti.size = entry_path.stat().st_size
                    ti.mtime = mtime
                    ti.type = tarfile.REGTYPE
                    ti.uid = 0
                    ti.gid = 0
                    ti.uname = "root"
                    ti.gname = "root"
                    if suffix == ".sh":
                        ti.mode = 0o755
                    else:
                        ti.mode = 0o644

                    with open(entry_path, "rb") as f_in:
                        tar.addfile(ti, f_in)

    size = out_file.stat().st_size
    with open(out_file, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest().lower()

    return size, sha256


def main() -> None:
    parser = argparse.ArgumentParser(description="Package maa-phone-project component archive")
    parser.add_argument("--src", type=Path, default=Path("dist/project/project"), help="Path to project scripts")
    parser.add_argument("--out", type=Path, default=Path("dist/maa-phone-project-0.1.0.tar.gz"), help="Output tar.gz archive")
    parser.add_argument("--mtime", type=int, default=DEFAULT_MTIME, help="Deterministic timestamp")
    args = parser.parse_args()

    size, sha256 = package_project_archive(args.src, args.out, args.mtime)
    print(f"PROJECT_ARCHIVE_OK file={args.out} size={size} sha256={sha256}")


if __name__ == "__main__":
    main()
