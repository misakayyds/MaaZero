#!/usr/bin/env python3
"""Sign and verify MaaZero channel manifests using external Ed25519 keypair.

Wraps `go run ./cmd/maa-release-sign` so signing uses the exact `manifest.LoadAndVerify`
implementation shipped in `maa-managerd`, while enforcing strict OS file ACLs on the
external signing key (`~/.ssh/maa_phone_manager_release_ed25519` by default).
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True

DEFAULT_MANIFEST_URL = "https://misakayyds.github.io/MaaZero/channels/stable.json"


def harden_private_key_permissions(key_path: Path) -> None:
    if not key_path.is_file():
        return
    if os.name == "nt":
        username = os.environ.get("USERNAME", "")
        if username:
            subprocess.run(
                [
                    "icacls",
                    str(key_path),
                    "/inheritance:r",
                    "/grant:r",
                    f"{username}:(R,W)",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
    else:
        os.chmod(key_path, 0o600)


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    default_key = Path.home() / ".ssh" / "maa_phone_manager_release_ed25519"

    parser = argparse.ArgumentParser(description="Sign or verify MaaZero release manifests")
    parser.add_argument("--key-file", type=Path, default=default_key, help="External Ed25519 seed file (outside repo)")
    parser.add_argument("--init-key", action="store_true", help="Create external Ed25519 keypair if missing")
    parser.add_argument("--verify-only", action="store_true", help="Verify existing .sig files without private key")
    parser.add_argument("--verify-artifacts", action="store_true", help="Stage-verify all built release artifacts in dist/")
    parser.add_argument("--dist-dir", type=Path, default=repo_root / "dist" / "channels", help="dist channels dir")
    parser.add_argument("--mirror-dir", type=Path, default=repo_root / "manifest" / "channels", help="manifest channels dir")
    parser.add_argument("--release-out", type=Path, default=repo_root / "dist" / "release.json", help="dist release.json path")
    parser.add_argument("--release-mirror", type=Path, default=repo_root / "manifest" / "release.json", help="manifest release.json path")
    parser.add_argument("--manifest-url", type=str, default=DEFAULT_MANIFEST_URL, help="Public HTTPS manifest URL")
    parser.add_argument("--channel", type=str, default="stable", choices=("stable", "beta"), help="Bootstrap channel")
    args = parser.parse_args()

    cmd = [
        "go",
        "run",
        "./cmd/maa-release-sign",
        "-repo-root",
        str(repo_root),
        "-dist-dir",
        str(args.dist_dir.resolve()),
        "-release-out",
        str(args.release_out.resolve()),
    ]
    if args.verify_only:
        cmd.append("-verify-only")
        if args.verify_artifacts:
            cmd.extend(["-verify-artifacts", str((repo_root / "dist").resolve())])
    else:
        resolved_key = args.key_file.expanduser().resolve()
        try:
            resolved_key.relative_to(repo_root)
            raise SystemExit(f"ERROR: refusing signing key inside repository: {resolved_key}")
        except ValueError:
            pass
        cmd.extend(["-key", str(resolved_key)])
        if args.init_key:
            cmd.append("-init-key")
        if args.mirror_dir:
            cmd.extend(["-mirror-dir", str(args.mirror_dir.resolve())])
        if args.release_mirror:
            cmd.extend(["-release-mirror", str(args.release_mirror.resolve())])
        cmd.extend(["-manifest-url", args.manifest_url, "-channel", args.channel])

    proc = subprocess.run(cmd, cwd=str(repo_root / "manager"), text=True, capture_output=True)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)

    if not args.verify_only:
        harden_private_key_permissions(args.key_file.expanduser().resolve())


if __name__ == "__main__":
    main()
