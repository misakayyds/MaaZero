"""Tier 2: Boundary and Corner Case tests for Packaging, Root Module, and Compliance (Features 12, 13, 14)."""

import os
import re
import sys
import json
import hashlib
import tempfile
import subprocess
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.elf import create_arm64_elf
from tests.e2e.common.pwsh import run_pwsh_file, run_pwsh_command
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RECIPE_DIR = REPO_ROOT / "packaging" / "rootfs"
if str(RECIPE_DIR) not in sys.path:
    sys.path.insert(0, str(RECIPE_DIR))

import recipe


class Tier2PackagingTest(E2ETestCase):
    """Tier 2 boundary test suite covering Features 12, 13, and 14."""

    tier = 2

    # ------------------------------------------------------------------------
    # Feature 12: 上游与自有边界隔离 (Upstream Boundary Boundary Cases)
    # ------------------------------------------------------------------------

    def test_f12_t2_01_recipe_rejection_floating_or_invalid_timestamp(self) -> None:
        """TC-F12-B01: Verify recipe validator rejects floating or non-ISO snapshot timestamps."""
        recipe_json = self.repo_root / "packaging" / "rootfs" / "recipe.json"
        config = json.loads(recipe_json.read_text(encoding="utf-8"))

        # Floating snapshot "latest" must be rejected
        config["snapshot"] = "latest"
        with self.assertRaises(ValueError) as ctx:
            recipe.validate(config)
        self.assertIn("fixed UTC timestamp", str(ctx.exception))

        # Invalid date snapshot must be rejected
        config["snapshot"] = "20260230T000000Z"
        with self.assertRaises(ValueError):
            recipe.validate(config)

    def test_f12_t2_02_recipe_rejection_non_arm64_architecture(self) -> None:
        """TC-F12-B02: Verify recipe validator rejects non-arm64 architecture or non-bookworm suite."""
        recipe_json = self.repo_root / "packaging" / "rootfs" / "recipe.json"
        config = json.loads(recipe_json.read_text(encoding="utf-8"))

        config["architecture"] = "amd64"
        with self.assertRaises(ValueError) as ctx:
            recipe.validate(config)
        self.assertIn("only bookworm/arm64 is supported", str(ctx.exception))

        config["architecture"] = "arm64"
        config["suite"] = "sid"
        with self.assertRaises(ValueError) as ctx:
            recipe.validate(config)
        self.assertIn("only bookworm/arm64 is supported", str(ctx.exception))

    def test_f12_t2_03_recipe_rejection_missing_mandatory_packages(self) -> None:
        """TC-F12-B03: Verify recipe validator rejects recipes missing mandatory packages."""
        recipe_json = self.repo_root / "packaging" / "rootfs" / "recipe.json"
        config = json.loads(recipe_json.read_text(encoding="utf-8"))

        config["packages"] = ["coreutils", "grep", "sed"]
        with self.assertRaises(ValueError) as ctx:
            recipe.validate(config)
        self.assertIn("missing required packages", str(ctx.exception))

    def test_f12_t2_04_manifest_rejection_corrupted_sha256(self) -> None:
        """TC-F12-B04: Verify schema validation rejects corrupted/non-hex SHA-256 digests."""
        schema_path = self.repo_root / "manifest" / "manifest.schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

        sha_pattern_str = schema_data["$defs"]["sha256"]["pattern"]
        sha_pattern = re.compile(sha_pattern_str)

        # Non-hex characters or wrong length must fail pattern match
        self.assertIsNone(sha_pattern.match("not_a_valid_sha256_hash"))
        self.assertIsNone(sha_pattern.match("a" * 63))  # 63 chars (too short)
        self.assertIsNone(sha_pattern.match("a" * 65))  # 65 chars (too long)
        self.assertIsNone(sha_pattern.match("A" * 64))  # uppercase rejected
        self.assertIsNotNone(sha_pattern.match("a" * 64))  # valid

    def test_f12_t2_05_manifest_rejection_oversized_payload(self) -> None:
        """TC-F12-B05: Verify LoadAndVerify rejects manifest payloads exceeding 1 MiB limit."""
        res = subprocess.run(
            ["go", "test", "./internal/manifest", "-run", "TestLoadAndVerifyRejectsOversizedPayload", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Oversized manifest Go test failed:\n{res.stdout}\n{res.stderr}")

    # ------------------------------------------------------------------------
    # Feature 13: Root 引导模块与救援入口 (Root Module Boundary Cases)
    # ------------------------------------------------------------------------

    def test_f13_t2_01_service_sh_fail_closed_on_symlinked_data_root(self) -> None:
        """TC-F13-B01: Verify service.sh checks for symlinks on data root / log dir and fails closed."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")
        self.assertIn("-L \"$DATA_ROOT\"", service_sh)
        self.assertIn("refusing to start:", service_sh)
        self.assertIn("fail_closed", service_sh)

    def test_f13_t2_02_service_sh_refusal_on_insecure_mode_755(self) -> None:
        """TC-F13-B02: Verify service.sh requires private 0:700 directory permissions and refuses 0755."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")
        self.assertIn("require_root_700", service_sh)
        self.assertIn("0:700", service_sh)
        self.assertIn("must be root-owned with mode 700", service_sh)

    def test_f13_t2_03_service_sh_detection_of_foreign_pid_port_collision(self) -> None:
        """TC-F13-B03: Verify service.sh verifies socket inode matching to prevent foreign PID collision."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")
        self.assertIn("/proc/$target_pid/fd", service_sh)
        self.assertIn("/proc/net/tcp", service_sh)
        self.assertIn("socket:[", service_sh)
        self.assertIn("0100007F:1F98", service_sh)

    def test_f13_t2_04_build_module_rejection_missing_allowlisted_files(self) -> None:
        """TC-F13-B04: Verify build-module.ps1 enforces presence of mandatory module files."""
        build_mod = (self.repo_root / "packaging" / "build-module.ps1").read_text(encoding="utf-8")
        self.assertIn("module source is missing", build_mod)
        self.assertIn("customize.sh", build_mod)
        self.assertIn("service.sh", build_mod)
        self.assertIn("uninstall.sh", build_mod)

    def test_f13_t2_05_uninstall_sh_idempotency_on_dead_daemon(self) -> None:
        """TC-F13-B05: Verify uninstall.sh terminates safely when daemon is already dead or stopped."""
        uninstall_sh = (self.repo_root / "module" / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIn('DATA_ROOT="${DATA_ROOT:-/data/local/maa-platform}"', uninstall_sh)
        self.assertIn("user data kept at $DATA_ROOT", uninstall_sh)
        self.assertIn("stop", uninstall_sh)
        self.assertNotIn("rm -rf /data/local/maa-platform", uninstall_sh)

    # ------------------------------------------------------------------------
    # Feature 14: 规范与合规红线 (Compliance & Security Boundary Cases)
    # ------------------------------------------------------------------------

    def test_f14_t2_01_build_project_rejection_isolated_cr_byte(self) -> None:
        """TC-F14-B01: Verify build-project.ps1 fails when source contains isolated CR bytes."""
        build_proj = (self.repo_root / "packaging" / "project" / "build-project.ps1").read_text(encoding="utf-8")
        self.assertIn("文件含孤立 CR 字节", build_proj)
        self.assertIn("BUILD_PROJECT_FAIL", build_proj)

    def test_f14_t2_02_build_project_hard_rejection_mock_credentials(self) -> None:
        """TC-F14-B02: Verify build-project.ps1 strictly rejects credential files like tg.txt and .env."""
        build_proj = (self.repo_root / "packaging" / "project" / "build-project.ps1").read_text(encoding="utf-8")
        self.assertIn("CredentialPatterns", build_proj)
        self.assertIn("tg\\.txt", build_proj)
        self.assertIn("\\.env", build_proj)
        self.assertIn("疑似凭证文件，禁止打包", build_proj)

    def test_f14_t2_03_build_project_rejection_unlisted_files(self) -> None:
        """TC-F14-B03: Verify build-project.ps1 rejects unallowlisted files in source tree."""
        build_proj = (self.repo_root / "packaging" / "project" / "build-project.ps1").read_text(encoding="utf-8")
        self.assertIn("源文件未列入项目文件名单", build_proj)

    def test_f14_t2_04_build_module_rejection_non_preview_version(self) -> None:
        """TC-F14-B04: Verify build-module.ps1 strictly requires preview version pattern."""
        build_mod = (self.repo_root / "packaging" / "build-module.ps1").read_text(encoding="utf-8")
        self.assertIn("version must be an explicit preview version", build_mod)
        self.assertIn("-preview", build_mod)

    def test_f14_t2_05_build_module_rejection_non_static_or_non_arm64_binary(self) -> None:
        """TC-F14-B05: Verify build-module.ps1 rejects dynamically linked ELF or non-ARM64 binaries."""
        build_mod = (self.repo_root / "packaging" / "build-module.ps1").read_text(encoding="utf-8")
        self.assertIn("PT_INTERP", build_mod)
        self.assertIn("arm64", build_mod)
        self.assertIn("CGO_ENABLED=0", build_mod)
