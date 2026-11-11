"""Tier 1: Feature Coverage tests for Packaging, Root Module, and Compliance (Features 12, 13, 14)."""

import os
import re
import json
import hashlib
import subprocess
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.elf import create_arm64_elf
from tests.e2e.common.pwsh import run_pwsh_file
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets


class Tier1PackagingTest(E2ETestCase):
    """Tier 1 test suite covering Features 12, 13, and 14."""

    # ------------------------------------------------------------------------
    # Feature 12: 上游与自有边界隔离 (Upstream Boundary & Manifest Pinning)
    # ------------------------------------------------------------------------

    def test_f12_t1_01_rootfs_candidate_recipe_planning(self) -> None:
        """TC-F12-01: Verify rootfs candidate recipe plan invariants."""
        plan_dir = self.sandbox_path / "recipe_plan"
        recipe_json = self.repo_root / "packaging" / "rootfs" / "recipe.json"
        recipe_py = self.repo_root / "packaging" / "rootfs" / "recipe.py"

        res = self.run_python([str(recipe_py), "--recipe", str(recipe_json), "--output", str(plan_dir)])
        self.assertEqual(res.returncode, 0, f"recipe.py failed:\n{res.stdout}\n{res.stderr}")

        plan_data = json.loads(res.stdout)
        self.assertFalse(plan_data["runtime_ready"], "Raw Debian base must not report runtime_ready")
        self.assertEqual(plan_data["architecture"], "arm64")
        self.assertEqual(plan_data["output_kind"], "raw-debian-base-not-installable")
        self.assertIn("snapshot.debian.org", " ".join(plan_data.get("argv", [])))
        self.assertIn("20260901T000000Z", " ".join(plan_data.get("argv", [])))
        self.assertIn("bookworm", " ".join(plan_data.get("argv", [])))

    def test_f12_t1_02_upstream_component_policy_schema_validation(self) -> None:
        """TC-F12-02: Validate upstream component policy definitions in channel manifest."""
        manifest_file = self.repo_root / "manifest" / "example-manifest.json"
        schema_file = self.repo_root / "manifest" / "manifest.schema.json"

        self.assertTrue(manifest_file.is_file(), f"Missing {manifest_file}")
        self.assertTrue(schema_file.is_file(), f"Missing {schema_file}")

        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        upstream = manifest_data.get("upstream_policy", {})

        self.assertIn("maa-cli", upstream)
        self.assertEqual(upstream["maa-cli"]["strategy"], "maa-self-update")

        self.assertIn("maacore", upstream)
        self.assertEqual(upstream["maacore"]["strategy"], "maa-update")

        self.assertIn("maa-resource", upstream)
        self.assertEqual(upstream["maa-resource"]["strategy"], "resource-sync")

    def test_f12_t1_03_component_sha256_digest_integrity(self) -> None:
        """TC-F12-03: Verify component artifacts declare lowercase 64-character SHA-256 digests."""
        manifest_file = self.repo_root / "manifest" / "example-manifest.json"
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))

        sha256_pattern = re.compile(r"^[a-f0-9]{64}$")
        for comp in manifest_data.get("components", []):
            for art in comp.get("artifacts", []):
                h = art.get("sha256", "")
                self.assertTrue(
                    sha256_pattern.match(h),
                    f"Component {comp.get('id')} has invalid sha256 digest: {h}",
                )
                self.assertGreater(art.get("size", 0), 0, "Artifact size must be positive")

    def test_f12_t1_04_ed25519_manifest_signature_verification(self) -> None:
        """TC-F12-04: Verify Ed25519 channel manifest signature verification in manager."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/manifest", "-run", "TestLoadAndVerifyAcceptsValidSignedManifest", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Manifest Ed25519 verification failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Go test did not pass target test:\n{res.stdout}")

    def test_f12_t1_05_rootfs_materialization_and_project_hash_verification(self) -> None:
        """TC-F12-05: Verify rootfs layout validation rules and project verification."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/runtime", "-run", "TestCheckFixtureBundlePasses", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Rootfs bundle verification failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Go test did not pass target test:\n{res.stdout}")

    # ------------------------------------------------------------------------
    # Feature 13: Root 引导模块与救援入口 (Root Module & Rescue Entry)
    # ------------------------------------------------------------------------

    def test_f13_t1_01_module_source_allowlist_and_permissions(self) -> None:
        """TC-F13-01: Run module layout verification script."""
        script_path = self.repo_root / "module" / "tests" / "test_module_layout.ps1"
        res = self.run_pwsh_file(script_path)
        self.assertEqual(res.returncode, 0, f"test_module_layout.ps1 failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("MODULE_LAYOUT_OK", res.stdout)

    def test_f13_t1_02_service_sh_late_start_loopback_binding(self) -> None:
        """TC-F13-02: Verify service.sh late-start loopback binding and security parameters."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")

        self.assertIn("DATA_ROOT=/data/local/maa-platform", service_sh)
        self.assertIn("LISTEN=127.0.0.1:8088", service_sh)
        self.assertIn('--listen "$LISTEN"', service_sh)
        self.assertIn('--config "$CONFIG"', service_sh)
        self.assertIn('--assets "$ASSETS"', service_sh)
        self.assertNotIn("0.0.0.0", service_sh, "service.sh must not bind 0.0.0.0")
        self.assertTrue(service_sh.strip().endswith("exit 0"), "service.sh must end with exit 0")

    def test_f13_t1_03_service_sh_socket_inode_verification(self) -> None:
        """TC-F13-03: Verify socket inode ownership check in service.sh."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")

        self.assertIn("/proc/$target_pid/fd", service_sh)
        self.assertIn("socket:[", service_sh)
        self.assertIn("/proc/net/tcp", service_sh)
        self.assertIn("0100007F:1F98", service_sh)  # 127.0.0.1:8088 in hex

    def test_f13_t1_04_webroot_index_html_zero_input_guarantee(self) -> None:
        """TC-F13-04: Verify module webroot/index.html contains zero inputs and fixed command execution."""
        html_path = self.repo_root / "module" / "webroot" / "index.html"
        content = html_path.read_text(encoding="utf-8")

        self.assertNotIn("<input", content)
        self.assertNotIn("<textarea", content)
        self.assertNotIn("<form", content)
        self.assertIn("/data/adb/modules/maa-phone-manager/bin/maa-managerd", content)
        self.assertIn("open-ui --data-root", content)
        self.assertIn("/data/local/maa-platform", content)
        self.assertIn("ksu.exec", content)

    def test_f13_t1_05_safe_uninstaller_user_data_preservation(self) -> None:
        """TC-F13-05: Verify uninstall.sh preserves user data at /data/local/maa-platform."""
        uninstall_sh = (self.repo_root / "module" / "uninstall.sh").read_text(encoding="utf-8")

        self.assertIn('DATA_ROOT="${DATA_ROOT:-/data/local/maa-platform}"', uninstall_sh)
        self.assertIn('"$BIN" stop --data-root "$DATA_ROOT"', uninstall_sh)
        self.assertIn("/proc/$pid/cmdline", uninstall_sh)
        self.assertNotIn("rm -rf /data/local/maa-platform", uninstall_sh)
        self.assertNotIn("pkill", uninstall_sh)
        self.assertIn("user data kept at $DATA_ROOT", uninstall_sh)

    # ------------------------------------------------------------------------
    # Feature 14: 规范与合规红线 (Compliance, Clean-Room Hygiene & Security)
    # ------------------------------------------------------------------------

    def test_f14_t1_01_repo_line_endings_pure_lf(self) -> None:
        """TC-F14-01: Ensure 100% of tracked repository text files contain 0 CR bytes."""
        violations = scan_cr_bytes(self.repo_root)
        self.assertEqual(len(violations), 0, f"Found CR byte violations in {len(violations)} files: {violations[:5]}")

    def test_f14_t1_02_repo_utf8_without_bom(self) -> None:
        """TC-F14-02: Ensure 100% of tracked repository text files have no UTF-8 BOM."""
        violations = scan_utf8_bom(self.repo_root)
        self.assertEqual(len(violations), 0, f"Found UTF-8 BOM in {len(violations)} files: {violations[:5]}")

    def test_f14_t1_03_repo_zero_credentials_and_secrets(self) -> None:
        """TC-F14-03: Ensure zero private keys, Telegram tokens, or secret files exist in repository."""
        violations = scan_secrets(self.repo_root)
        self.assertEqual(len(violations), 0, f"Found secret violations in {len(violations)} files: {violations[:5]}")

    def test_f14_t1_04_deterministic_project_packaging_reproduction(self) -> None:
        """TC-F14-04: Run project build and allowlist verification tests."""
        script_allowlist = self.repo_root / "packaging" / "project" / "tests" / "test_build_project_allowlist.ps1"
        res1 = self.run_pwsh_file(script_allowlist)
        self.assertEqual(res1.returncode, 0, f"test_build_project_allowlist.ps1 failed:\n{res1.stdout}\n{res1.stderr}")

        script_build = self.repo_root / "packaging" / "project" / "tests" / "test_build_project.ps1"
        res2 = self.run_pwsh_file(script_build)
        self.assertEqual(res2.returncode, 0, f"test_build_project.ps1 failed:\n{res2.stdout}\n{res2.stderr}")

    def test_f14_t1_05_deterministic_module_packaging_reproduction(self) -> None:
        """TC-F14-05: Run packaging module test suite verifying binary validation and package construction."""
        test_script = self.repo_root / "packaging" / "test-module.ps1"
        res = self.run_pwsh_file(test_script)
        self.assertEqual(res.returncode, 0, f"packaging/test-module.ps1 failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("MODULE_PACKAGING_OK", res.stdout)
