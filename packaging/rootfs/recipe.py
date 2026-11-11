"""Build a raw Debian base candidate, never a signed/installable MAA runtime.

Planning is cross-platform and offline. --build requires an already provisioned,
native ARM64 Linux builder; this tool never installs host packages or uses sudo.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
KEYRING = "/usr/share/keyrings/debian-archive-keyring.gpg"
REQUIRED = {"bash", "python3", "coreutils", "util-linux", "grep", "sed", "mawk",
            "findutils", "procps", "curl", "ca-certificates", "tar", "gzip", "git",
            "adb", "cron", "tzdata", "locales", "debian-archive-keyring"}


def validate(config):
    if not isinstance(config, dict) or set(config) != {
        "schema_version", "suite", "architecture", "snapshot", "packages"
    }:
        raise ValueError("unknown or missing recipe fields")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("unsupported schema")
    if config["suite"] != "bookworm" or config["architecture"] != "arm64":
        raise ValueError("only bookworm/arm64 is supported by this candidate recipe")
    stamp = config["snapshot"]
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp):
        raise ValueError("snapshot must be a fixed UTC timestamp")
    datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
    packages = config["packages"]
    if (not isinstance(packages, list) or not packages or len(packages) > 100
            or any(not isinstance(p, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", p) or p.endswith("-")
                   for p in packages) or len(set(packages)) != len(packages)):
        raise ValueError("packages must be unique literal Debian package names")
    if not REQUIRED.issubset(packages):
        raise ValueError("missing required packages")


def build_environment(config):
    validate(config)
    epoch = datetime.strptime(config["snapshot"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    # Do not inherit APT_CONFIG, loader variables, proxy credentials, or hook inputs.
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(Path.home()),
            "LC_ALL": "C.UTF-8", "TZ": "UTC", "DEBIAN_FRONTEND": "noninteractive",
            "SOURCE_DATE_EPOCH": str(int(epoch.timestamp()))}


def make_plan(config, output):
    validate(config)
    output = Path(output).absolute()
    argv = ["/usr/bin/mmdebstrap", "--mode=unshare", "--variant=minbase", "--format=tar",
            "--architectures=arm64", f"--keyring={KEYRING}",
            "--skip=cleanup/apt/lists",
            '--aptopt=Acquire::Check-Valid-Until "false"',
            '--aptopt=Acquire::Languages "none"',
            '--aptopt=APT::Install-Recommends "false"',
            "--include=" + ",".join(sorted(config["packages"])),
            "--customize-hook=sh -c " + shlex.quote((HERE / "customize.sh").read_text(encoding="utf-8")) + ' -- "$1"',
            "bookworm", "-"]
    for archive, suite in [("debian", "bookworm"), ("debian", "bookworm-updates"),
                           ("debian-security", "bookworm-security")]:
        argv.append(f"deb https://snapshot.debian.org/archive/{archive}/{config['snapshot']}/ {suite} main")
    return {"schema_version": 1, "architecture": "arm64", "runtime_ready": False,
            "output_kind": "raw-debian-base-not-installable", "output_directory": str(output), "snapshot_verified": False,
            "dependency_closure_locked": False, "argv": argv,
            "source_date_epoch": build_environment(config)["SOURCE_DATE_EPOCH"],
            "remaining": ["resolve and retain signed snapshot metadata and package closure",
                          "pin maa-cli, MaaCore and resources with verified artifact hashes",
                          "normalize archive for manager rootfs policy and test installation",
                          "validate runtime driver, mutable data and Android integration"]}


def preflight(*, system=None, machine=None):
    if (system or platform.system()) != "Linux" or (machine or platform.machine()) not in {"aarch64", "arm64"}:
        raise ValueError("build requires native Linux ARM64; no implicit QEMU or host setup")
    if os.geteuid() == 0:
        raise ValueError("use an unprivileged isolated builder with user namespaces, not host root")
    for name in ("mmdebstrap", "gpgv", "apt-get", "dpkg", "chroot", "sh", "newuidmap", "newgidmap"):
        if not shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin"):
            raise ValueError(f"missing builder dependency: {name}; install separately with authorization")
    if not Path("/usr/bin/mmdebstrap").is_file() or not Path(KEYRING).is_file():
        raise ValueError("missing standard mmdebstrap executable or trusted Debian archive keyring")


def prepare_output(output):
    # Existing trees (including links) are never reused, overwritten, or cleaned up.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)


def build(config, output):
    plan = make_plan(config, output)
    preflight()
    output = Path(output).absolute()
    if min(shutil.disk_usage(output.parent).free, shutil.disk_usage("/tmp").free) < 6 * 1024**3:
        raise ValueError("at least 6 GiB free builder space required (not a phone space estimate)")
    prepare_output(output)
    env = build_environment(config)
    version = subprocess.run(["/usr/bin/mmdebstrap", "--version"], env=env,
                             capture_output=True, text=True, check=True)
    with Path(KEYRING).open("rb") as stream:
        keyring_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    plan["builder_keyring_sha256"] = keyring_hash
    (output / "build-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    # mmdebstrap runs package scripts inside its user namespace; its default
    # policy-rc.d/start-stop-daemon protection is deliberately not bypassed.
    # Parent opens private files; the unshared UID only needs inherited FDs.
    # Keep .partial until mmdebstrap succeeds. No cleanup or implicit retry.
    partial = output / "debian-base.raw.tar.partial"
    with (output / "build.log").open("xb") as log, partial.open("xb") as archive_stream:
        subprocess.run(plan["argv"], env=env, cwd="/tmp", stdout=archive_stream, stderr=log, check=True)
    archive = output / "debian-base.raw.tar"
    partial.rename(archive)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"runtime_ready": False, "output_kind": plan["output_kind"],
              "sha256": digest, "size": archive.stat().st_size,
              "builder_version": (version.stdout + version.stderr).strip(),
              "builder_keyring_sha256": keyring_hash,
              "recipe": config, "package_inventory": "usr/share/maa-build/packages.tsv",
              "warning": "raw base only; not a release lock, not manager-installable"}
    (output / "build-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, default=HERE / "recipe.json")
    parser.add_argument("--output", type=Path, required=True, help="new directory under an existing parent")
    parser.add_argument("--build", action="store_true", help="execute on a prepared native ARM64 Linux builder")
    args = parser.parse_args()
    try:
        config = json.loads(args.recipe.read_text(encoding="utf-8"))
        result = build(config, args.output) if args.build else make_plan(config, args.output)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"rootfs candidate failed: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
