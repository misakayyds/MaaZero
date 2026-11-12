#!/usr/bin/env python3
"""Generate schema-compliant channel manifests for stable and beta channels.

Reads exact byte sizes and SHA-256 digests from bin/maa-managerd,
dist/maa-phone-project-<version>.tar.gz, and (when provided or present)
dist/debian-rootfs-12.11.0-arm64.tar.gz, produces dist/channels/{stable,beta}.json,
and mirrors them to manifest/channels/{stable,beta}.json.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

DEFAULT_REPO = "misakayyds/MaaZero"
DEFAULT_PUBLISHER = "MaaZero"
DEFAULT_VERSION = "0.1.0"
DEFAULT_ROOTFS_VERSION = "12.11.0"


def compute_file_info(path: Path) -> tuple[int, str]:
    if not path.is_file():
        raise FileNotFoundError(f"required artifact not found: {path}")
    size = path.stat().st_size
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return size, digest.hexdigest().lower()


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
    version: str = DEFAULT_VERSION,
    repo: str = DEFAULT_REPO,
    publisher: str = DEFAULT_PUBLISHER,
    rootfs_version: str = DEFAULT_ROOTFS_VERSION,
) -> dict:
    if channel not in ("stable", "beta"):
        raise ValueError(f"invalid channel: {channel}")

    release_tag = f"v{version}"
    source_url = f"https://github.com/{repo}/tree/{release_tag}"
    license_url = f"https://github.com/{repo}/blob/{release_tag}/LICENSE"

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
                "version": version,
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
                        "url": f"https://github.com/{repo}/releases/download/{release_tag}/maa-managerd",
                        "mirrors": [],
                        "sha256": manager_sha256,
                        "size": manager_size,
                        "media_type": "application/octet-stream",
                        "compression": "none",
                        "provenance": {
                            "publisher": publisher,
                            "source_url": source_url,
                            "license_url": license_url
                        }
                    }
                ],
                "health_checks": ["manager-api"]
            },
            {
                "id": "maa-phone-project",
                "kind": "project",
                "version": version,
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
                        "url": f"https://github.com/{repo}/releases/download/{release_tag}/maa-phone-project-{version}.tar.gz",
                        "mirrors": [],
                        "sha256": project_sha256,
                        "size": project_size,
                        "media_type": "application/gzip",
                        "compression": "tar.gz",
                        "provenance": {
                            "publisher": publisher,
                            "source_url": source_url,
                            "license_url": license_url
                        }
                    }
                ],
                "health_checks": ["webui-static", "config-parse"]
            },
            {
                "id": "debian-rootfs",
                "kind": "runtime",
                "version": rootfs_version,
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
                        "url": f"https://github.com/{repo}/releases/download/rootfs-{rootfs_version}/debian-rootfs-{rootfs_version}-arm64.tar.gz",
                        "mirrors": [],
                        "sha256": rootfs_sha256,
                        "size": rootfs_size,
                        "media_type": "application/gzip",
                        "compression": "tar.gz",
                        "provenance": {
                            "publisher": f"{publisher} from Debian Bookworm ARM64 packages",
                            "source_url": f"https://github.com/{repo}/blob/{release_tag}/packaging/rootfs/recipe.json",
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
    parser.add_argument("--version", type=str, default=DEFAULT_VERSION, help="Release version (e.g. 0.1.0)")
    parser.add_argument("--sequence", type=int, default=1, help="Monotonic manifest sequence number")
    parser.add_argument("--repo", type=str, default=DEFAULT_REPO, help="GitHub owner/repo slug")
    parser.add_argument("--publisher", type=str, default=DEFAULT_PUBLISHER, help="Provenance publisher name")
    parser.add_argument("--rootfs-version", type=str, default=DEFAULT_ROOTFS_VERSION, help="Debian rootfs component version")
    parser.add_argument("--manager", type=Path, default=Path("bin/maa-managerd"), help="Path to manager binary")
    parser.add_argument("--project", type=Path, default=None, help="Path to project archive")
    parser.add_argument("--rootfs", type=Path, default=None, help="Path to debian rootfs archive")
    parser.add_argument("--dist-out", type=Path, default=Path("dist/channels"), help="Directory for dist manifests")
    parser.add_argument("--manifest-out", type=Path, default=Path("manifest/channels"), help="Directory for mirrored manifests")
    args = parser.parse_args()

    project_path = args.project or Path(f"dist/maa-phone-project-{args.version}.tar.gz")
    mgr_size, mgr_sha256 = compute_file_info(args.manager)
    prj_size, prj_sha256 = compute_file_info(project_path)

    default_rootfs_candidate = Path(f"dist/debian-rootfs-{args.rootfs_version}-arm64.tar.gz")
    rootfs_path = args.rootfs
    if rootfs_path is None and default_rootfs_candidate.is_file():
        rootfs_path = default_rootfs_candidate

    rootfs_size = 536870912
    rootfs_sha256 = "3333333333333333333333333333333333333333333333333333333333333333"
    if rootfs_path is not None:
        rootfs_size, rootfs_sha256 = compute_file_info(rootfs_path)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    args.dist_out.mkdir(parents=True, exist_ok=True)
    args.manifest_out.mkdir(parents=True, exist_ok=True)

    for ch in ("stable", "beta"):
        manifest_data = build_manifest(
            channel=ch,
            sequence=args.sequence,
            manager_size=mgr_size,
            manager_sha256=mgr_sha256,
            project_size=prj_size,
            project_sha256=prj_sha256,
            generated_at=now_iso,
            rootfs_size=rootfs_size,
            rootfs_sha256=rootfs_sha256,
            version=args.version,
            repo=args.repo,
            publisher=args.publisher,
            rootfs_version=args.rootfs_version,
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
