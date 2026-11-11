#!/usr/bin/env python3
"""Generate schema-compliant channel manifests for stable and beta channels.

Reads exact byte sizes and SHA-256 digests from bin/maa-managerd and
dist/maa-phone-project-0.1.0-preview.tar.gz, produces dist/channels/{stable,beta}.json,
and mirrors them to manifest/channels/{stable,beta}.json.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


def compute_file_info(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise FileNotFoundError(f"required artifact not found: {path}")
    size = path.stat().st_size
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest().lower()
    return size, digest


def build_manifest(
    channel: str,
    sequence: int,
    manager_size: int,
    manager_sha256: str,
    project_size: int,
    project_sha256: str,
    generated_at: str,
    rootfs_size: int = 536870912,
    rootfs_sha256: str = "3333333333333333333333333333333333333333333333333333333333333333",
) -> dict:
    if channel not in ("stable", "beta"):
        raise ValueError(f"invalid channel: {channel}")

    return {
        "schema_version": 1,
        "sequence": sequence,
        "channel": channel,
        "generated_at": generated_at,
        "minimum_bootstrap_version": "0.1.0",
        "notices": [],
        "blocked_versions": [],
        "components": [
            {
                "id": "maa-managerd",
                "kind": "manager",
                "version": "0.1.0-preview",
                "required": True,
                "install_mode": "replace-file",
                "compatibility": {
                    "architectures": ["arm64-v8a"],
                    "android_api": {
                        "minimum": 30,
                        "maximum": 40
                    },
                    "root_managers": ["magisk", "kernelsu", "apatch"]
                },
                "artifacts": [
                    {
                        "url": "https://github.com/MaaXYZ/maa-phone-manager/releases/download/v0.1.0-preview/maa-managerd",
                        "mirrors": [],
                        "sha256": manager_sha256,
                        "size": manager_size,
                        "media_type": "application/octet-stream",
                        "compression": "none",
                        "provenance": {
                            "publisher": "MAA Phone Manager",
                            "source_url": "https://github.com/MaaXYZ/maa-phone-manager/tree/v0.1.0-preview",
                            "license_url": "https://github.com/MaaXYZ/maa-phone-manager/blob/v0.1.0-preview/LICENSE"
                        }
                    }
                ],
                "health_checks": ["manager-api"]
            },
            {
                "id": "maa-phone-project",
                "kind": "project",
                "version": "0.1.0-preview",
                "required": True,
                "install_mode": "extract-versioned",
                "compatibility": {
                    "architectures": ["arm64-v8a"],
                    "android_api": {
                        "minimum": 30,
                        "maximum": 40
                    },
                    "root_managers": ["magisk", "kernelsu", "apatch"]
                },
                "artifacts": [
                    {
                        "url": "https://github.com/MaaXYZ/maa-phone-manager/releases/download/v0.1.0-preview/maa-phone-project-0.1.0-preview.tar.gz",
                        "mirrors": [],
                        "sha256": project_sha256,
                        "size": project_size,
                        "media_type": "application/gzip",
                        "compression": "tar.gz",
                        "provenance": {
                            "publisher": "MAA Phone Manager",
                            "source_url": "https://github.com/MaaXYZ/maa-phone-manager/tree/v0.1.0-preview",
                            "license_url": "https://github.com/MaaXYZ/maa-phone-manager/blob/v0.1.0-preview/LICENSE"
                        }
                    }
                ],
                "health_checks": ["webui-static", "config-parse"]
            },
            {
                "id": "debian-rootfs",
                "kind": "runtime",
                "version": "12.11.0",
                "required": True,
                "install_mode": "extract-versioned",
                "compatibility": {
                    "architectures": ["arm64-v8a"],
                    "android_api": {
                        "minimum": 30,
                        "maximum": 40
                    },
                    "root_managers": ["magisk", "kernelsu", "apatch"]
                },
                "artifacts": [
                    {
                        "url": "https://github.com/MaaXYZ/maa-phone-manager/releases/download/rootfs-12.11.0/debian-rootfs-12.11.0-arm64.tar.gz",
                        "mirrors": [],
                        "sha256": rootfs_sha256,
                        "size": rootfs_size,
                        "media_type": "application/gzip",
                        "compression": "tar.gz",
                        "provenance": {
                            "publisher": "MAA Phone Manager CI from Debian packages",
                            "source_url": "https://snapshot.debian.org/archive/debian/20260901T000000Z/",
                            "license_url": "https://www.debian.org/legal/licenses/"
                        }
                    }
                ],
                "health_checks": ["debian-exec", "maa-version"]
            }
        ],
        "upstream_policy": {
            "maa-cli": {
                "strategy": "maa-self-update",
                "channel": channel,
                "automatic": False,
                "allowed_versions": ">=0.5.0 <1.0.0"
            },
            "maacore": {
                "strategy": "maa-update",
                "channel": channel,
                "automatic": True
            },
            "maa-resource": {
                "strategy": "resource-sync",
                "channel": channel,
                "automatic": True
            }
        }
    }


def validate_manifest_semantics(data: dict) -> None:
    assert data["schema_version"] == 1, "bad schema_version"
    assert data["sequence"] >= 1, "bad sequence"
    assert data["channel"] in ("stable", "beta"), "bad channel"
    assert VERSION_PATTERN.match(data["minimum_bootstrap_version"]), "bad minimum_bootstrap_version"
    assert len(data["components"]) == 3, "must have 3 components"

    expected_policies = {
        "maa-cli": "maa-self-update",
        "maacore": "maa-update",
        "maa-resource": "resource-sync"
    }
    assert set(data["upstream_policy"].keys()) == set(expected_policies.keys()), "bad upstream_policy keys"
    for name, strategy in expected_policies.items():
        pol = data["upstream_policy"][name]
        assert pol["strategy"] == strategy, f"bad strategy for {name}"
        assert pol["channel"] in ("stable", "beta"), f"bad channel for {name}"

    for comp in data["components"]:
        assert ID_PATTERN.match(comp["id"]), f"bad comp id: {comp['id']}"
        assert comp["kind"] in ("manager", "project", "runtime"), f"bad kind: {comp['kind']}"
        assert VERSION_PATTERN.match(comp["version"]), f"bad comp version: {comp['version']}"
        assert comp["install_mode"] in ("replace-file", "extract-versioned"), f"bad install_mode: {comp['install_mode']}"
        assert comp["compatibility"]["architectures"] == ["arm64-v8a"]
        assert comp["compatibility"]["root_managers"] == ["magisk", "kernelsu", "apatch"]
        assert len(comp["artifacts"]) >= 1, "missing artifacts"
        for art in comp["artifacts"]:
            assert art["url"].startswith("https://"), "url must be https"
            assert comp["version"] in art["url"], f"url must contain version {comp['version']}"
            assert SHA256_PATTERN.match(art["sha256"]), f"bad sha256: {art['sha256']}"
            assert art["size"] > 0, f"bad size: {art['size']}"
            assert art["compression"] in ("none", "tar.gz"), f"bad compression: {art['compression']}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate release channel manifests")
    parser.add_argument("--manager", type=Path, default=Path("bin/maa-managerd"), help="Path to manager binary")
    parser.add_argument("--project", type=Path, default=Path("dist/maa-phone-project-0.1.0-preview.tar.gz"), help="Path to project archive")
    parser.add_argument("--rootfs", type=Path, default=None, help="Optional path to debian rootfs archive")
    parser.add_argument("--dist-out", type=Path, default=Path("dist/channels"), help="Directory for dist manifests")
    parser.add_argument("--manifest-out", type=Path, default=Path("manifest/channels"), help="Directory for mirrored manifests")
    args = parser.parse_args()

    mgr_size, mgr_sha256 = compute_file_info(args.manager)
    prj_size, prj_sha256 = compute_file_info(args.project)
    rootfs_size = 536870912
    rootfs_sha256 = "3333333333333333333333333333333333333333333333333333333333333333"
    if args.rootfs is not None:
        rootfs_size, rootfs_sha256 = compute_file_info(args.rootfs)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    args.dist_out.mkdir(parents=True, exist_ok=True)
    args.manifest_out.mkdir(parents=True, exist_ok=True)

    for ch in ("stable", "beta"):
        manifest_data = build_manifest(
            channel=ch,
            sequence=1,
            manager_size=mgr_size,
            manager_sha256=mgr_sha256,
            project_size=prj_size,
            project_sha256=prj_sha256,
            generated_at=now_iso,
            rootfs_size=rootfs_size,
            rootfs_sha256=rootfs_sha256,
        )
        validate_manifest_semantics(manifest_data)

        content = json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n"

        dist_target = args.dist_out / f"{ch}.json"
        manifest_target = args.manifest_out / f"{ch}.json"

        dist_target.write_bytes(content.encode("utf-8"))
        manifest_target.write_bytes(content.encode("utf-8"))

        print(f"MANIFEST_OK channel={ch} path={dist_target} mirrored={manifest_target}")


if __name__ == "__main__":
    main()
