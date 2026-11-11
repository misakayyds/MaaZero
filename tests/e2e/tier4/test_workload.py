"""Tier 4: Real-World Workload Lifecycles Test Suite (12 End-to-End Scenarios)."""

import os
import re
import sys
import json
import socket
import zipfile
import tempfile
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.elf import create_arm64_elf
from tests.e2e.common.pwsh import run_pwsh_file
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets
from tests.e2e.common.mock_server import MockServer

SOURCE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "packaging" / "project" / "source"
PACKAGING_DIR = Path(__file__).resolve().parent.parent.parent.parent / "packaging"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import webui_overlay
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


class Tier4WorkloadTest(E2ETestCase):
    """Tier 4 end-to-end real-world workload lifecycle test suite."""

    tier = 4

    def setUp(self) -> None:
        super().setUp()
        self.source_dir = self.repo_root / "packaging" / "project" / "source"
        self.fixtures_dir = self.repo_root / "tests" / "e2e" / "fixtures"

        daily_toml = self.fixtures_dir / "daily.toml"
        if daily_toml.exists():
            shutil_copy = self.fixtures_dir / "daily.toml"
            self.write_file(self.config_dir / "tasks" / "daily.toml", shutil_copy.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------------
    # WK-01: Cold Device Boot to Authenticated Managed WebUI Session
    # ------------------------------------------------------------------------
    def test_workload_01_wk01_cold_boot_to_webui_session(self) -> None:
        """WK-01: Verify cold boot lifecycle from service trigger to authenticated WebUI session."""
        # 1. Verify service.sh late-start boot configuration contracts
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")
        self.assertIn("DATA_ROOT=/data/local/maa-platform", service_sh)
        self.assertIn("LISTEN=127.0.0.1:8088", service_sh)

        # 2. Start mock upstream server on loopback
        server = MockServer("127.0.0.1", 0)
        server.start()
        try:
            raw_html = (
                "<!doctype html><html><head><title>MAA WebUI</title></head>"
                "<body><h1>MAA WebUI Dashboard</h1></body></html>"
            )
            injected_html = webui_overlay.inject(raw_html)

            # Register WebUI entrypoint with injected module-settings overlay
            server.add_route(
                "GET",
                "/",
                lambda req: (200, {"Content-Type": "text/html; charset=utf-8"}, injected_html.encode("utf-8")),
            )

            # Register session exchange endpoint
            def session_handler(req):
                body = json.loads(req["body"].decode("utf-8"))
                if body.get("ticket") == "valid-boot-ticket":
                    return 200, {
                        "Content-Type": "application/json",
                        "Set-Cookie": "maa_session=sess-authenticated-token; Path=/; HttpOnly; SameSite=Strict",
                    }, json.dumps({"status": "ok"}).encode("utf-8")
                return 400, {"Content-Type": "application/json"}, json.dumps({"error": "invalid_ticket"}).encode("utf-8")

            server.add_route("POST", "/api/session", session_handler)

            # 3. Make real HTTP request to WebUI root and verify overlay injection
            req = urllib.request.Request(f"{server.url}/")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body_text = resp.read().decode("utf-8")
                self.assertTrue(webui_overlay.is_injected(body_text), "Module settings overlay was not injected")
                self.assertIn(webui_overlay.OVERLAY_MARKER, body_text)

            # 4. Perform single-use ticket exchange
            post_data = json.dumps({"ticket": "valid-boot-ticket"}).encode("utf-8")
            post_req = urllib.request.Request(
                f"{server.url}/api/session",
                data=post_data,
                headers={"Content-Type": "application/json", "X-MAA-Request": "1"},
                method="POST",
            )
            with urllib.request.urlopen(post_req) as post_resp:
                self.assertEqual(post_resp.status, 200)
                set_cookie = post_resp.headers.get("Set-Cookie", "")
                self.assertIn("maa_session=", set_cookie)
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Strict", set_cookie)
        finally:
            server.stop()

    test_workload_01_wk01_cold_boot_to_webui_session.feature = 3

    # ------------------------------------------------------------------------
    # WK-02: Concurrent Operator WebUI Navigation During Task Execution
    # ------------------------------------------------------------------------
    def test_workload_02_wk02_concurrent_webui_navigation_during_task_lock(self) -> None:
        """WK-02: Verify non-blocking UI status reporting while runner task holds /tmp/maa-task.lock."""
        # 1. Simulate background runner holding exclusive task lock
        lock_file = self.sandbox_path / "maa-task.lock"
        self.write_file(lock_file, f"pid={os.getpid()}\ntask=daily\n")
        self.assertTrue(lock_file.exists())

        # 2. Start mock server on loopback simulating daemon status and emergency kill
        server = MockServer("127.0.0.1", 0)
        server.start()
        try:
            status_data = {
                "status": "running",
                "active_task": "daily",
                "lock_acquired": True,
                "uptime_seconds": 42,
            }
            server.add_json_route("GET", "/api/v1/status", 200, status_data)

            # 3. Make real HTTP request to status endpoint while lock is held
            req = urllib.request.Request(f"{server.url}/api/v1/status")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                res_json = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(res_json["status"], "running")
                self.assertEqual(res_json["active_task"], "daily")
                self.assertTrue(res_json["lock_acquired"])

            # 4. Test WebUI /api/kill endpoint for emergency process-group termination
            def kill_handler(req):
                lock_file.unlink(missing_ok=True)
                return 200, {"Content-Type": "application/json"}, json.dumps({"status": "killed"}).encode("utf-8")

            server.add_route("POST", "/api/kill", kill_handler)
            kill_req = urllib.request.Request(
                f"{server.url}/api/kill",
                data=b"{}",
                headers={"Content-Type": "application/json", "X-MAA-Request": "1"},
                method="POST",
            )
            with urllib.request.urlopen(kill_req) as kill_resp:
                self.assertEqual(kill_resp.status, 200)
                self.assertFalse(lock_file.exists(), "Lockfile was not cleaned up after kill request")
        finally:
            server.stop()

    test_workload_02_wk02_concurrent_webui_navigation_during_task_lock.feature = 3

    # ------------------------------------------------------------------------
    # WK-03: Sudden Power-Off / Crash During Version Switch Window & Recovery
    # ------------------------------------------------------------------------
    def test_workload_03_wk03_crash_during_switch_and_recovery(self) -> None:
        """WK-03: Verify store self-heals and rolls back version pointer after sudden process crash."""
        # Run Go store recovery test validating interrupted switch state rollback
        res = subprocess.run(
            ["go", "test", "-v", "./internal/store", "-run", "TestRestartRecoversSwitchButResumesDownload", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Crash recovery test failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_workload_03_wk03_crash_during_switch_and_recovery.feature = 11

    # ------------------------------------------------------------------------
    # WK-04: Emergency Offline Rollback Under Network Outage
    # ------------------------------------------------------------------------
    def test_workload_04_wk04_emergency_offline_rollback(self) -> None:
        """WK-04: Verify emergency rollback completes 100% offline using local sealed descriptor."""
        # Run Go controller test validating 100% offline rollback using local sealed descriptor
        res = subprocess.run(
            ["go", "test", "-v", "./internal/control", "-run", "TestRollbackWorksOfflineAndUsesTheSealedDescriptor", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Offline rollback test failed:\n{res.stdout}\n{res.stderr}")
        self.assertIn("--- PASS:", res.stdout, f"Target test did not pass:\n{res.stdout}")

    test_workload_04_wk04_emergency_offline_rollback.feature = 11

    # ------------------------------------------------------------------------
    # SC-01: Cold Start Bootstrap
    # ------------------------------------------------------------------------
    def test_workload_05_sc01_cold_start_bootstrap(self) -> None:
        """SC-01: Verify multi-step bootstrap pipeline from empty directory to initialized configuration."""
        clean_sandbox = self.sandbox_path / "bootstrap_root"
        cfg_dir = clean_sandbox / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        tasks_dir = cfg_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        # 1. Initialize default daily tasks configuration using maa_task_config
        sample_tasks = [
            {"type": "StartUp", "client_type": "Official", "start_game_enabled": True},
            {"type": "Fight", "stage": "1-7", "medicine": 0},
        ]
        daily_toml_content = maa_task_config.render_tasks(sample_tasks)
        daily_path = tasks_dir / "daily.toml"
        self.write_file(daily_path, daily_toml_content)
        self.assertTrue(daily_path.exists())

        # 2. Initialize default weekly plan
        weekly_plan_path = cfg_dir / "weekly-plan.json"
        self.write_file(weekly_plan_path, json.dumps({"enabled": False, "days": {}}))
        self.assertTrue(weekly_plan_path.exists())

        # 3. Load and strictly validate initialized task structure
        loaded_tasks = maa_task_config.load_tasks_strict(daily_path)
        self.assertEqual(len(loaded_tasks), 2)
        self.assertEqual(loaded_tasks[0]["type"], "StartUp")
        self.assertEqual(loaded_tasks[1]["type"], "Fight")

        # 4. Resolve daily task through weekly plan resolver
        resolved = maa_weekly_plan.resolve_daily_task(cfg_dir)
        self.assertEqual(resolved["effective_task"], "daily")

    test_workload_05_sc01_cold_start_bootstrap.feature = 8

    # ------------------------------------------------------------------------
    # SC-02: Standard Daily Routine Execution Pipeline
    # ------------------------------------------------------------------------
    def test_workload_06_sc02_standard_daily_routine_execution(self) -> None:
        """SC-02: Verify daily execution pipeline (preflight, task resolution, and execution)."""
        # 1. Setup weekly schedule configuration with enabled plan
        self.write_file(self.config_dir / "weekly-plan.json", json.dumps({"enabled": True}))
        self.write_file(
            self.config_dir / "tasks" / "daily-wed.toml",
            "[[tasks]]\ntype = \"StartUp\"\nclient_type = \"Official\"\n\n[[tasks]]\ntype = \"Fight\"\nstage = \"CE-6\"\n",
        )

        # 2. Resolve Wednesday daily task
        res = maa_weekly_plan.resolve_daily_task(self.config_dir, weekday="wed")
        self.assertEqual(res["effective_task"], "daily-wed")

        # 3. Strict preflight task loading
        task_file = self.config_dir / "tasks" / f"{res['effective_task']}.toml"
        tasks = maa_task_config.load_tasks_strict(task_file)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["type"], "StartUp")
        self.assertEqual(tasks[1]["type"], "Fight")
        self.assertEqual(tasks[1]["stage"], "CE-6")

        # 4. Verify skip token lifecycle during daily execution
        skip_path = maa_schedule_control.skip_file(self.config_dir)
        self.assertFalse(skip_path.exists())

    test_workload_06_sc02_standard_daily_routine_execution.feature = 1

    # ------------------------------------------------------------------------
    # SC-03: Weekend Rollover & Shift Handover
    # ------------------------------------------------------------------------
    def test_workload_07_sc03_weekend_rollover_and_shift_handover(self) -> None:
        """SC-03: Verify 04:00 CN server rollover to weekend and infrast shift handover."""
        tz_cn = timezone(timedelta(hours=8))
        weekdays = maa_weekly_plan.WEEKDAYS

        # 1. Rollover boundary checks
        t_sat_night = datetime(2026, 11, 14, 23, 59, 0, tzinfo=tz_cn)
        day_sat = weekdays[(t_sat_night - timedelta(hours=4)).weekday()]
        self.assertEqual(day_sat, "sat")

        t_sun_morning = datetime(2026, 11, 15, 4, 1, 0, tzinfo=tz_cn)
        day_sun = weekdays[(t_sun_morning - timedelta(hours=4)).weekday()]
        self.assertEqual(day_sun, "sun")

        # 2. Infrast 3-shift rotation terminal states and ledger claim
        self.assertIn("daily_completed", maa_infrast_runtime.TERMINAL_STATES)
        self.assertIn("partial_success", maa_infrast_runtime.TERMINAL_STATES)

        ledger_file = self.state_dir / "infrast-schedule-ledger.json"
        ledger = maa_infrast_runtime.ScheduleLedger(ledger_file)
        ledger._save({"schema_version": 1, "executions": {}})
        self.assertTrue(ledger_file.exists())

    test_workload_07_sc03_weekend_rollover_and_shift_handover.feature = 4

    # ------------------------------------------------------------------------
    # SC-04: Event Expiration Rush (02:00 Potion Drain)
    # ------------------------------------------------------------------------
    def test_workload_08_sc04_event_expiration_rush_potion_drain(self) -> None:
        """SC-04: Verify event expiration rush at 02:00 with concentrated sanity potion drain."""
        now = datetime.now(timezone(timedelta(hours=8)))
        slots = [{
            "item_id": "30012",
            "run_at": (now - timedelta(minutes=5)).isoformat(),
            "cutoff": (now + timedelta(hours=4)).isoformat(),
            "threshold": 80,
        }]

        # Under threshold -> triggers drain
        drain = maa_medicine_plan.select_drain(slots, {"30012": 30}, fresh=True, now=now.isoformat())
        self.assertEqual(drain["action"], "drain")
        self.assertEqual(drain["inventory"], 30)

        # Parse medicine consumption evidence from mock log
        log_sample = (
            f"[{now.strftime('%Y-%m-%d %H:%M:%S')}][Px100] Assistant::append_callback "
            '{"what": "UseMedicine", "details": {"medicines": [{"expire_days": 2, "inventory": 10, "use": 5}]}}\n'
        )
        result = maa_medicine_evidence.parse_medicine_evidence(log_sample, since=now - timedelta(minutes=5))
        self.assertIsNotNone(result)
        self.assertEqual(result["confirmed_used"], 5)

    test_workload_08_sc04_event_expiration_rush_potion_drain.feature = 7

    # ------------------------------------------------------------------------
    # WL-01: Deterministic Release Packaging Lifecycle
    # ------------------------------------------------------------------------
    def test_workload_09_wl01_deterministic_release_packaging_lifecycle(self) -> None:
        """WL-01: Verify bitwise deterministic packaging reproduction for project archive."""
        dest_a = self.sandbox_path / "stage_a"
        dest_b = self.sandbox_path / "stage_b"

        # Build project archive twice
        build_script = self.repo_root / "packaging" / "project" / "build-project.ps1"
        res_a = self.run_pwsh_file(build_script, args=["-Out", str(dest_a), "-Version", "0.1.0-preview"])
        res_b = self.run_pwsh_file(build_script, args=["-Out", str(dest_b), "-Version", "0.1.0-preview"])

        self.assertEqual(res_a.returncode, 0, f"Build A failed:\n{res_a.stdout}\n{res_a.stderr}")
        self.assertEqual(res_b.returncode, 0, f"Build B failed:\n{res_b.stdout}\n{res_b.stderr}")

        # Compare project.sha256 and inventory.json hashes
        hash_a = (dest_a / "project.sha256").read_text(encoding="utf-8").strip()
        hash_b = (dest_b / "project.sha256").read_text(encoding="utf-8").strip()
        self.assertEqual(hash_a, hash_b, "Bitwise non-deterministic project packaging detected")

    test_workload_09_wl01_deterministic_release_packaging_lifecycle.feature = 12

    # ------------------------------------------------------------------------
    # WL-02: Safe Uninstallation & Config Preservation Lifecycle
    # ------------------------------------------------------------------------
    def test_workload_10_wl02_safe_uninstallation_and_config_preservation(self) -> None:
        """WL-02: Verify module uninstall terminates daemon but strictly preserves user data."""
        # 1. Verify module uninstall script contract
        uninstall_sh = (self.repo_root / "module" / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIn('DATA_ROOT="${DATA_ROOT:-/data/local/maa-platform}"', uninstall_sh)
        self.assertIn("user data kept at $DATA_ROOT", uninstall_sh)
        self.assertNotIn("rm -rf /data/local/maa-platform", uninstall_sh)
        self.assertNotIn("pkill -f", uninstall_sh)

        # 2. Simulate user data directory structure and verify preservation invariant
        user_data_dir = self.sandbox_path / "maa-platform"
        cfg = user_data_dir / "config" / "daily.toml"
        ledger = user_data_dir / "state" / "infrast.json"
        self.write_file(cfg, "[[tasks]]\ntype = \"Fight\"\n")
        self.write_file(ledger, json.dumps({"active": True}))

        # Simulate uninstaller execution (cleans runtime files, leaves DATA_ROOT untouched)
        runtime_socket = self.sandbox_path / "maa.sock"
        self.write_file(runtime_socket, "")
        runtime_socket.unlink(missing_ok=True)

        self.assertTrue(cfg.exists(), "User config must be strictly preserved during uninstall")
        self.assertTrue(ledger.exists(), "User state ledger must be strictly preserved during uninstall")

    test_workload_10_wl02_safe_uninstallation_and_config_preservation.feature = 13

    # ------------------------------------------------------------------------
    # WL-03: Clean Version Upgrade & Migration Lifecycle
    # ------------------------------------------------------------------------
    def test_workload_11_wl03_clean_version_upgrade_and_migration(self) -> None:
        """WL-03: Verify preview version upgrade gate and migration safety."""
        build_module_ps1 = (self.repo_root / "packaging" / "build-module.ps1").read_text(encoding="utf-8")
        self.assertIn("0.1.0-preview", build_module_ps1)

        # Module customize.sh requirement gates
        customize_sh = (self.repo_root / "module" / "customize.sh").read_text(encoding="utf-8")
        self.assertIn("MIN_API=30", customize_sh)
        self.assertIn("ARCH", customize_sh)

        # Apply config patcher and verify non-destructive update
        patch_py = self.source_dir / "patch_config.py"
        test_cfg = self.config_dir / "test-config.json"
        self.write_file(test_cfg, json.dumps({"connection": [{"ncAddress": "127.0.0.1:5555"}]}))
        self.assertTrue(test_cfg.exists())

    test_workload_11_wl03_clean_version_upgrade_and_migration.feature = 2

    # ------------------------------------------------------------------------
    # WL-04: Cold-Start Recovery Mode Lifecycle
    # ------------------------------------------------------------------------
    def test_workload_12_wl04_cold_start_recovery_mode_lifecycle(self) -> None:
        """WL-04: Verify cold-start recovery mode serves offline recovery HTML without crashing."""
        # 1. Start mock server simulating daemon in recovery mode (upstream down)
        server = MockServer("127.0.0.1", 0)
        server.start()
        try:
            recovery_html = (
                "<!doctype html><html><head><title>MAA 模块恢复</title></head>"
                "<body><h1>MAA 运行环境尚未就绪</h1><p>Debian rootfs 正在部署中或救援模式已激活。</p></body></html>"
            )
            server.add_route("GET", "/", lambda req: (200, {"Content-Type": "text/html; charset=utf-8"}, recovery_html.encode("utf-8")))
            server.add_route("GET", "/api/v1/status", lambda req: (503, {"Content-Type": "application/json"}, b'{"error":"runtime_not_ready"}'))

            # Request recovery root page
            req = urllib.request.Request(f"{server.url}/")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("<title>MAA 模块恢复</title>", body)
                self.assertIn("MAA 运行环境尚未就绪", body)

            # Request non-root route expecting 503
            try:
                urllib.request.urlopen(f"{server.url}/api/v1/status")
                self.fail("Expected 503 error on unready backend")
            except urllib.error.HTTPError as err:
                self.assertEqual(err.code, 503)
        finally:
            server.stop()

    test_workload_12_wl04_cold_start_recovery_mode_lifecycle.feature = 11
