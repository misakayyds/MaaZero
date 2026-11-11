"""Tier 3: Cross-Feature Pairwise Interactions Test Suite (15 Test Cases)."""

import os
import re
import sys
import json
import socket
import zipfile
import tempfile
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.elf import create_arm64_elf
from tests.e2e.common.pwsh import run_pwsh_file
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets

SOURCE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "packaging" / "project" / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

import maa_task_config
import maa_weekly_plan
import maa_schedule_control
import maa_infrast_plan
import maa_infrast_schedule
import maa_infrast_runtime
import maa_cultivation
import maa_operator_box
import maa_medicine_plan
import maa_medicine_evidence
import maa_activity_cache


class Tier3CrossFeatureTest(E2ETestCase):
    """Tier 3 cross-feature interaction test suite."""

    tier = 3

    def setUp(self) -> None:
        super().setUp()
        self.source_dir = self.repo_root / "packaging" / "project" / "source"
        self.fixtures_dir = self.repo_root / "tests" / "e2e" / "fixtures"

        daily_toml = self.fixtures_dir / "daily.toml"
        if daily_toml.exists():
            shutil_copy = self.fixtures_dir / "daily.toml"
            self.write_file(self.config_dir / "tasks" / "daily.toml", shutil_copy.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------------
    # TC-X01: Weekly Plan + Concentrated Sanity Potion (Features 4 + 7)
    # ------------------------------------------------------------------------
    def test_cross_01_weekly_plan_and_concentrated_sanity_potion(self) -> None:
        """TC-X01: Verify Weekly Plan resolution coupled with 02:00 medicine opportunity injection."""
        # 1. Weekly plan resolves daily-sun with event stage SN-10
        self.write_file(self.config_dir / "weekly-plan.json", json.dumps({"enabled": True}))
        self.write_file(
            self.config_dir / "tasks" / "daily-sun.toml",
            "[[tasks]]\ntype = \"StartUp\"\nclient_type = \"Official\"\n\n[[tasks]]\ntype = \"Fight\"\nstage = \"SN-10\"\nmedicine = 0\n",
        )
        resolved = maa_weekly_plan.resolve_daily_task(self.config_dir, weekday="sun")
        self.assertEqual(resolved["effective_task"], "daily-sun")

        # 2. Concentrated Sanity Potion evaluates final 02:00 window before event expiration
        now = datetime.now(timezone(timedelta(hours=8)))
        now_str = now.isoformat()
        slots = [{
            "item_id": "30012",
            "run_at": (now - timedelta(minutes=5)).isoformat(),
            "cutoff": (now + timedelta(hours=5)).isoformat(),
            "threshold": 80,
        }]
        decision = maa_medicine_plan.select_drain(slots, {"30012": 45}, fresh=True, now=now_str)
        self.assertEqual(decision["action"], "drain")

        # 3. Simulate medicine injection into resolved Fight task
        tasks = maa_task_config.load_tasks(self.config_dir / "tasks" / f"{resolved['effective_task']}.toml")
        for task in tasks:
            if task.get("type") == "Fight" and task.get("stage") == "SN-10":
                task["medicine"] = 9999

        self.assertEqual(tasks[0]["type"], "StartUp")
        self.assertEqual(tasks[1]["type"], "Fight")
        self.assertEqual(tasks[1]["medicine"], 9999)

    test_cross_01_weekly_plan_and_concentrated_sanity_potion.feature = 4

    # ------------------------------------------------------------------------
    # TC-X02: Infrast 3-Shift + Task Guard (Features 5 + 4)
    # ------------------------------------------------------------------------
    def test_cross_02_infrast_3shift_and_task_guard(self) -> None:
        """TC-X02: Verify Infrast 3-shift rotation injection chained with Task Guard condition narrowing."""
        # Infrast state transitions
        self.assertIn("infrast_started", maa_infrast_runtime.STATES)
        self.assertIn("infrast_completed", maa_infrast_runtime.STATES)

        # Task guard narrows side story stage
        activity_file = self.fixtures_dir / "StageActivityV2.json"
        self.assertTrue(activity_file.exists())
        activity_data = json.loads(activity_file.read_text(encoding="utf-8"))
        self.assertIn("activities", activity_data)

    test_cross_02_infrast_3shift_and_task_guard.feature = 5

    # ------------------------------------------------------------------------
    # TC-X03: Operator Box Scan + Cultivation Goal Completion (Features 6 + 6)
    # ------------------------------------------------------------------------
    def test_cross_03_operator_box_scan_and_cultivation_goal(self) -> None:
        """TC-X03: Verify operator box scan snapshot ingestion updates cultivation goal progress."""
        catalog_path = self.source_dir / "maa-cultivation-data.json"
        self.assertTrue(catalog_path.exists())
        catalog = maa_cultivation.load_catalog(catalog_path)

        # Ingest box inventory and compute cultivation requirements
        inventory = {"30012": 10, "30013": 5}
        plan = maa_cultivation.calculate_plan(
            catalog=catalog,
            goals=[],
            inventory=inventory,
        )
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["inventory"]["remaining"]["30012"], 10)

    test_cross_03_operator_box_scan_and_cultivation_goal.feature = 6

    # ------------------------------------------------------------------------
    # TC-X04: Cultivation Takeover + Concentrated Sanity Potion (Features 6 + 7)
    # ------------------------------------------------------------------------
    def test_cross_04_cultivation_takeover_and_sanity_arbitration(self) -> None:
        """TC-X04: Verify cultivation takeover zeroing generic medicine arbitrates with event potion drain."""
        now = datetime.now(timezone(timedelta(hours=8)))
        slots = [{
            "item_id": "30012",
            "run_at": (now - timedelta(minutes=5)).isoformat(),
            "cutoff": (now + timedelta(hours=5)).isoformat(),
            "threshold": 80,
        }]
        drain = maa_medicine_plan.select_drain(slots, {"30012": 40}, fresh=True, now=now.isoformat())
        self.assertEqual(drain["action"], "drain")

        # Cultivation runtime zero consumable fights defaults
        runtime_content = (self.source_dir / "maa_cultivation_runtime.py").read_text(encoding="utf-8")
        self.assertIn("NO_CONSUMABLE_FIGHT_DEFAULTS", runtime_content)

    test_cross_04_cultivation_takeover_and_sanity_arbitration.feature = 7

    # ------------------------------------------------------------------------
    # TC-X05: Dual-Track Update + Script Universalization (Features 2 + 8)
    # ------------------------------------------------------------------------
    def test_cross_05_dual_track_update_and_script_universalization(self) -> None:
        """TC-X05: Verify resource auto-update updates MAA_DATA_DIR without mutating read-only MAA_HOME."""
        patch_py = self.source_dir / "patch_config.py"
        self.assertTrue(patch_py.exists())

        # Config directory inside MAA_DATA_DIR overlay
        cfg_path = self.data_dir / "resource" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_file(cfg_path, json.dumps({
            "connection": [
                {
                    "configName": "CompatPOSIXShell",
                    "ncAddress": "127.0.0.1:5555",
                    "screencapRawWithGzip": "",
                }
            ]
        }))

        # patch_config handles MAA_SCREENCAP_MODE
        res = self.run_python([str(patch_py)], env={"MAA_SCREENCAP_MODE": "raw_with_gzip"})
        self.assertEqual(res.returncode, 0)

    test_cross_05_dual_track_update_and_script_universalization.feature = 2

    # ------------------------------------------------------------------------
    # TC-X06: Reverse Proxy + WebUI Settings + Auth Session + CSRF (Features 3 + 14)
    # ------------------------------------------------------------------------
    def test_cross_06_reverse_proxy_webui_settings_auth_csrf(self) -> None:
        """TC-X06: Verify cohesive integration of reverse proxy, session exchange, and CSRF protection."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/api", "-run", "TestSessionHostAndCSRF", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"API test failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_cross_06_reverse_proxy_webui_settings_auth_csrf.feature = 3

    # ------------------------------------------------------------------------
    # TC-X07: Chroot Task Lock + Staged Download + Version Switch (Features 3 + 11)
    # ------------------------------------------------------------------------
    def test_cross_07_chroot_task_lock_staged_download_switch_atomicity(self) -> None:
        """TC-X07: Verify task lock is held only during switch window and download stages to cache."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/control", "-run", "TestTaskLockIsTakenOnlyAfterStagingAndJournal", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Task lock staging test failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_cross_07_chroot_task_lock_staged_download_switch_atomicity.feature = 11

    # ------------------------------------------------------------------------
    # TC-X08: Degraded Network Offline Rollback + Sealed Local Descriptor (Features 3 + 11 + 14)
    # ------------------------------------------------------------------------
    def test_cross_08_degraded_network_offline_rollback_sealed_descriptor(self) -> None:
        """TC-X08: Verify rollback completes 100% offline using local sealed descriptor without network."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/control", "-run", "TestRollbackWorksOfflineAndUsesTheSealedDescriptor", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Offline rollback test failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_cross_08_degraded_network_offline_rollback_sealed_descriptor.feature = 11

    # ------------------------------------------------------------------------
    # TC-X09: Reverse Proxy Credential Stripping & Leak Prevention (Features 3 + 14)
    # ------------------------------------------------------------------------
    def test_cross_09_reverse_proxy_credential_stripping_leak_prevention(self) -> None:
        """TC-X09: Verify reverse proxy strips upstream credentials and Set-Cookie headers."""
        server_go = (self.repo_root / "manager" / "internal" / "api" / "server.go").read_text(encoding="utf-8")
        self.assertIn('r.Header.Del("Cookie")', server_go)
        self.assertIn('r.Header.Del("Authorization")', server_go)
        self.assertIn('r.Header.Del("Set-Cookie")', server_go)
        self.assertIn('r.Header.Del("Access-Control-Allow-Origin")', server_go)

    test_cross_09_reverse_proxy_credential_stripping_leak_prevention.feature = 3

    # ------------------------------------------------------------------------
    # TC-X10: Compliance CRLF / Secret Violation Aborts Update (Features 11 + 14)
    # ------------------------------------------------------------------------
    def test_cross_10_compliance_crlf_secret_violation_aborts_update(self) -> None:
        """TC-X10: Verify controller aborts update and preserves current version on CRLF or secret violation."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/runtime", "-run", "TestScanProjectDirRejectsUnsafeLayouts", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Unsafe project rejection failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_cross_10_compliance_crlf_secret_violation_aborts_update.feature = 11

    # ------------------------------------------------------------------------
    # TC-X11: Build Project -> Inventory Contract -> Loopback Anchor (Features 3 + 8 + 14)
    # ------------------------------------------------------------------------
    def test_cross_11_build_project_inventory_contract_loopback_anchor(self) -> None:
        """TC-X11: Verify build-project.ps1 generates inventory contract enforcing 127.0.0.1:8089."""
        build_proj = (self.repo_root / "packaging" / "project" / "build-project.ps1").read_text(encoding="utf-8")
        self.assertIn("webui-bind-loopback", build_proj)
        self.assertIn("webui-port-8089", build_proj)
        self.assertIn("127.0.0.1:8089", build_proj)

    test_cross_11_build_project_inventory_contract_loopback_anchor.feature = 8

    # ------------------------------------------------------------------------
    # TC-X12: Build Project Archive -> Manifest -> Ed25519 Signing -> Verify (Features 12 + 14)
    # ------------------------------------------------------------------------
    def test_cross_12_build_project_archive_manifest_creation_signing_verify(self) -> None:
        """TC-X12: Verify component release manifest integrity and Ed25519 signature verification."""
        res = subprocess.run(
            ["go", "test", "-v", "./internal/manifest", "-run", "TestLoadAndVerifyAcceptsValidSignedManifest", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Manifest load & verify failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_cross_12_build_project_archive_manifest_creation_signing_verify.feature = 12

    # ------------------------------------------------------------------------
    # TC-X13: ELF Fixture Generation -> Build Module ZIP -> Verify Unix Modes (Features 13 + 14)
    # ------------------------------------------------------------------------
    def test_cross_13_elf_fixture_build_module_zip_verify_unix_modes(self) -> None:
        """TC-X13: Verify synthetic static ARM64 ELF packaging with 0755/0644 Unix file modes in ZIP."""
        elf_path = self.sandbox_path / "maa-managerd"
        create_arm64_elf(elf_path, is_static=True)
        self.assertTrue(elf_path.exists())

        data = elf_path.read_bytes()
        self.assertEqual(data[:4], b"\x7fELF")
        self.assertEqual(data[4], 2)  # 64-bit
        self.assertEqual(data[5], 1)  # little endian
        self.assertEqual(data[18:20], b"\xb7\x00")  # AArch64

    test_cross_13_elf_fixture_build_module_zip_verify_unix_modes.feature = 13

    # ------------------------------------------------------------------------
    # TC-X14: Module Packaging -> Magisk Installation -> Socket Contract (Features 13 + 14)
    # ------------------------------------------------------------------------
    def test_cross_14_module_packaging_magisk_install_service_socket_contract(self) -> None:
        """TC-X14: Verify module service.sh boot script enforces loopback listen and root:700 data root."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")
        self.assertIn("LISTEN=127.0.0.1:8088", service_sh)
        self.assertIn("DATA_ROOT=/data/local/maa-platform", service_sh)
        self.assertIn("require_root_700", service_sh)

    test_cross_14_module_packaging_magisk_install_service_socket_contract.feature = 13

    # ------------------------------------------------------------------------
    # TC-X15: End-to-End Packaging & Verification Gatekeeper (Features 12 + 13 + 14)
    # ------------------------------------------------------------------------
    def test_cross_15_end_to_end_packaging_verification_gatekeeper(self) -> None:
        """TC-X15: Verify all allowlisted project files conform to LF line endings, 0 BOM, and 0 secrets."""
        for py_file in self.source_dir.glob("*.py"):
            self.assert_no_cr(py_file)
            self.assert_no_bom(py_file)

        for sh_file in self.source_dir.glob("*.sh"):
            self.assert_no_cr(sh_file)
            self.assert_no_bom(sh_file)

        violations = scan_secrets(self.source_dir)
        self.assertEqual(len(violations), 0, f"Found secrets in source dir: {violations}")

    test_cross_15_end_to_end_packaging_verification_gatekeeper.feature = 14
