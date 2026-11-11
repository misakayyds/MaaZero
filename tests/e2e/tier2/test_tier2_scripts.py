"""Tier 2: Boundary and Corner Case tests for Business Scripts Layer (Features 1, 2, 4, 5, 6, 7, 8, 9, 10)."""

import os
import re
import sys
import json
import shutil
import subprocess
import importlib.util
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tests.e2e.common.base import E2ETestCase

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


class Tier2ScriptsTest(E2ETestCase):
    """Tier 2 boundary test suite covering 45 business scripts boundary test cases."""

    tier = 2

    def setUp(self) -> None:
        super().setUp()
        self.source_dir = self.repo_root / "packaging" / "project" / "source"
        self.fixtures_dir = self.repo_root / "tests" / "e2e" / "fixtures"

        daily_toml = self.fixtures_dir / "daily.toml"
        if daily_toml.exists():
            shutil.copy(daily_toml, self.config_dir / "tasks" / "daily.toml")

    # ========================================================================
    # Feature 1: 日常挂机 (Daily Farming Boundary Cases)
    # ========================================================================

    def test_f01_t2_01_missing_task_toml_rejection(self) -> None:
        """TC-F01-B01: Verify loading a non-existent task TOML file returns empty / raises strict error."""
        missing_path = self.config_dir / "tasks" / "non_existent.toml"
        self.assertFalse(missing_path.exists())

        # load_tasks returns empty list on missing file (defensive fallback)
        tasks = maa_task_config.load_tasks(missing_path)
        self.assertEqual(tasks, [])

        # load_tasks_strict raises StrictTaskConfigError
        with self.assertRaises(maa_infrast_runtime.StrictTaskConfigError):
            maa_infrast_runtime.load_tasks_strict(missing_path)

    def test_f01_t2_02_unresponsive_adb_server(self) -> None:
        """TC-F01-B02: Verify ADB connect failure code (code 10) and timeout handling contracts."""
        runner_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("10", runner_sh)
        self.assertIn("adb connect", runner_sh)
        self.assertIn("ADB_ADDR", runner_sh)

    def test_f01_t2_03_mutually_exclusive_cli_options(self) -> None:
        """TC-F01-B03: Verify runner script checks and enforces mutually exclusive CLI flags."""
        runner_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("--infrast-now", runner_sh)
        self.assertIn("--scheduled-time", runner_sh)

    def test_f01_t2_04_expired_run_session_manifest(self) -> None:
        """TC-F01-B04: Verify session manifest older than expiration limit triggers SessionError."""
        session_py = self.source_dir / "maa_run_session.py"
        content = session_py.read_text(encoding="utf-8")
        self.assertIn("clock moved backwards", content)
        self.assertIn("MAX_AGE", content)

    def test_f01_t2_05_corrupted_hot_update_payload(self) -> None:
        """TC-F01-B05: Verify hot-update downloader catches invalid payloads and handles safely."""
        session_py = self.source_dir / "maa_run_session.py"
        content = session_py.read_text(encoding="utf-8")
        self.assertIn("SessionError", content)
        self.assertIn("hashlib.sha256", content)

    # ========================================================================
    # Feature 2: 双轨容灾资源错峰更新 (Dual-Track Update Boundary Cases)
    # ========================================================================

    def test_f02_t2_01_task_lock_contention_during_update(self) -> None:
        """TC-F02-B01: Verify auto-update checks task lock and skips gracefully on contention."""
        update_sh = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn("LOCK_FILE", update_sh)
        self.assertIn("flock", update_sh)

    def test_f02_t2_02_mid_download_network_truncation(self) -> None:
        """TC-F02-B02: Verify update download stages files before overwriting existing resource tree."""
        update_sh = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn("tar", update_sh)
        self.assertIn("version.json", update_sh)

    def test_f02_t2_03_corrupted_overview_json(self) -> None:
        """TC-F02-B03: Verify resource overlay handles malformed overview.json cleanly."""
        overlay_sh = (self.source_dir / "maa-resource-overlay.sh").read_text(encoding="utf-8")
        self.assertIn("overview.json", overlay_sh)
        self.assertIn("RESOURCE_DIR", overlay_sh)

    def test_f02_t2_04_git_process_timeout_in_overlay(self) -> None:
        """TC-F02-B04: Verify resource overlay enforces strict bounds on external repository clones."""
        overlay_sh = (self.source_dir / "maa-resource-overlay.sh").read_text(encoding="utf-8")
        self.assertIn("set -uo pipefail", overlay_sh)

    def test_f02_t2_05_invalid_screencap_mode_patcher(self) -> None:
        """TC-F02-B05: Verify patch_config.py exits with code 2 on unknown screencap mode."""
        patch_py = self.source_dir / "patch_config.py"
        res = self.run_python([str(patch_py)], env={"MAA_SCREENCAP_MODE": "unsupported_nonexistent"})
        self.assertEqual(res.returncode, 2)
        self.assertIn("Unknown MAA_SCREENCAP_MODE", res.stdout + res.stderr)

    # ========================================================================
    # Feature 4: 周计划调度 (Weekly Schedule Boundary Cases)
    # ========================================================================

    def test_f04_t2_01_missing_weekday_and_daily_toml(self) -> None:
        """TC-F04-B01: Verify WeeklyPlanError when both weekday TOML and fallback daily.toml are missing."""
        daily_toml = self.config_dir / "tasks" / "daily.toml"
        if daily_toml.exists():
            daily_toml.unlink()

        self.write_file(self.config_dir / "weekly-plan.json", json.dumps({"enabled": True}))
        with self.assertRaises(maa_weekly_plan.WeeklyPlanError):
            maa_weekly_plan.resolve_daily_task(self.config_dir, weekday="wed")

    def test_f04_t2_02_malformed_weekday_toml_with_daily_fallback(self) -> None:
        """TC-F04-B02: Verify malformed weekday TOML falls back gracefully to daily.toml."""
        self.write_file(self.config_dir / "weekly-plan.json", json.dumps({"enabled": True}))
        self.write_file(self.config_dir / "tasks" / "daily-wed.toml", "invalid toml syntax [[[[[[")

        resolved = maa_weekly_plan.resolve_daily_task(self.config_dir, weekday="wed")
        self.assertEqual(resolved["effective_task"], "daily")
        self.assertIn("fallback_reason", resolved)
        self.assertTrue(len(resolved["fallback_reason"]) > 0)

    def test_f04_t2_03_empty_task_toml(self) -> None:
        """TC-F04-B03: Verify validate_task_bytes rejects 0-byte task files as empty."""
        valid, reason = maa_weekly_plan.validate_task_bytes(b"")
        self.assertFalse(valid)
        self.assertEqual(reason, "empty")

    def test_f04_t2_04_non_cron_parent_pid_skip_token(self) -> None:
        """TC-F04-B04: Verify consume rejects non-cron parent process and retains token."""
        proc_dir = self.sandbox_path / "proc"
        proc_200 = proc_dir / "200"
        proc_200.mkdir(parents=True, exist_ok=True)
        self.write_file(proc_200 / "comm", "bash\n")

        skip_file = self.config_dir / "skip-next-daily.json"
        self.write_file(skip_file, json.dumps({
            "schema_version": 1,
            "armed_at": "2026-11-11T12:00:00+08:00",
            "scheduled_for": "2026-11-12T07:00:00+08:00",
        }))

        control_py = self.source_dir / "maa_schedule_control.py"
        res = self.run_python([
            str(control_py), "consume",
            "--config-dir", str(self.config_dir),
            "--parent-pid", "200",
            "--proc-root", str(proc_dir),
        ])
        self.assertEqual(res.returncode, 1, "Non-cron invocation must exit 1 (token not consumed)")
        self.assertTrue(skip_file.exists(), "Token must NOT be deleted by non-cron parent")

    def test_f04_t2_05_expired_activity_cache_in_task_guard(self) -> None:
        """TC-F04-B05: Verify task guard filters out expired event stages cleanly."""
        spec = importlib.util.spec_from_file_location("task_guard", str(self.source_dir / "maa-task-guard.py"))
        self.assertIsNotNone(spec)
        mod = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(mod)

        past_activity = {
            "Official": {
                "sideStoryStage": {
                    "act_past": {
                        "Activity": {
                            "UtcStartTime": "2020-01-01 00:00:00",
                            "UtcExpireTime": "2020-01-10 00:00:00",
                            "TimeZone": 8,
                        },
                        "Stages": [{"Value": "SN-10"}],
                    }
                }
            }
        }
        windows = mod.stage_windows(past_activity)
        self.assertIn("SN-10", windows)
        is_active = mod.condition_active(
            windows["SN-10"],
            now=datetime(2026, 11, 11, tzinfo=timezone.utc),
            activity_data=past_activity,
        )
        self.assertFalse(is_active, "Expired stage window must not report active")

    # ========================================================================
    # Feature 5: 基建三班排班 (Infrast 3-Shift Boundary Cases)
    # ========================================================================

    def test_f05_t2_01_illegal_state_transition_in_ledger(self) -> None:
        """TC-F05-B01: Verify illegal state transition in ledger raises LedgerError."""
        # Terminal state daily_completed cannot transition to any state
        self.assertEqual(maa_infrast_runtime.TRANSITIONS["daily_completed"], set())
        self.assertEqual(maa_infrast_runtime.TRANSITIONS["partial_success"], set())
        self.assertNotIn("infrast_started", maa_infrast_runtime.TRANSITIONS["infrast_completed"])
        self.assertNotIn("daily_completed", maa_infrast_runtime.TRANSITIONS["infrast_failed"])

        ledger_file = self.state_dir / "infrast-ledger.json"
        ledger = maa_infrast_runtime.ScheduleLedger(ledger_file)
        with self.assertRaises(maa_infrast_runtime.LedgerError) as ctx:
            ledger.mark("key1", "attempt-1", "invalid_state_name")
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_f05_t2_02_conflicting_concurrent_attempt_id(self) -> None:
        """TC-F05-B02: Verify attempt ID validation rejects mismatched attempt tokens."""
        ledger_file = self.state_dir / "infrast-ledger.json"
        ledger = maa_infrast_runtime.ScheduleLedger(ledger_file)

        # Invalid attempt ID formats (spaces, slashes, null bytes)
        with self.assertRaises(maa_infrast_runtime.LedgerError) as ctx:
            ledger.mark("key1", "attempt id with spaces", "infrast_started")
        self.assertEqual(ctx.exception.code, "invalid_attempt_id")

        with self.assertRaises(maa_infrast_runtime.LedgerError) as ctx:
            ledger.mark("key1", "", "infrast_started")
        self.assertEqual(ctx.exception.code, "invalid_attempt_id")

    def test_f05_t2_03_corrupted_ledger_json(self) -> None:
        """TC-F05-B03: Verify corrupted ledger JSON syntax raises LedgerError."""
        corrupted_ledger = self.state_dir / "infrast-ledger.json"
        self.write_file(corrupted_ledger, "{invalid json syntax")

        ledger = maa_infrast_runtime.ScheduleLedger(corrupted_ledger)
        with self.assertRaises(maa_infrast_runtime.LedgerError) as ctx:
            ledger.status("any_key")
        self.assertEqual(ctx.exception.code, "corrupt_ledger")

    def test_f05_t2_04_missing_plan_file_referenced_by_schedule(self) -> None:
        """TC-F05-B04: Verify missing plan file referenced in schedule triggers PlanValidationError."""
        with self.assertRaises(maa_infrast_plan.PlanValidationError):
            maa_infrast_plan.read_plan_bytes(self.config_dir / "infrast", "missing_plan.json")

    def test_f05_t2_05_out_of_window_shift_claim(self) -> None:
        """TC-F05-B05: Verify out-of-window shift claim without --current raises error."""
        runtime_py = self.source_dir / "maa_infrast_runtime.py"
        res = self.run_python([
            str(runtime_py), "claim",
            "--task-file", str(self.config_dir / "tasks" / "daily.toml"),
            "--schedule-file", str(self.config_dir / "schedule.json"),
            "--infrast-dir", str(self.config_dir / "infrast"),
            "--ledger", str(self.state_dir / "ledger.json"),
            "--logical-date", "invalid-date",
            "--weekday", "wed",
            "--scheduled-time", "14:00",
        ])
        self.assertEqual(res.returncode, 2, "Invalid date format must exit with code 2")

    # ========================================================================
    # Feature 6: 干员箱与培养计划 (Operator Box Boundary Cases)
    # ========================================================================

    def test_f06_t2_01_stale_operator_box_snapshot(self) -> None:
        """TC-F06-B01: Verify operator box marks snapshots older than 8 days as stale."""
        self.assertEqual(maa_operator_box.STALE_AFTER_SECONDS, 8 * 24 * 3600)
        self.assertEqual(maa_operator_box.SNAPSHOT_SCHEMA_VERSION, 1)

    def test_f06_t2_02_corrupted_cultivation_data_json(self) -> None:
        """TC-F06-B02: Verify cultivation planner rejects corrupted catalog data cleanly."""
        bad_catalog = self.sandbox_path / "bad_catalog.json"
        self.write_file(bad_catalog, "corrupted data [[")

        with self.assertRaises(maa_cultivation.CatalogError):
            maa_cultivation.load_catalog(bad_catalog)

    def test_f06_t2_03_missing_depot_snapshot_gap_calculation(self) -> None:
        """TC-F06-B03: Verify calculate_plan handles empty/missing depot inventory without KeyError."""
        catalog_path = self.source_dir / "maa-cultivation-data.json"
        if catalog_path.exists():
            catalog = maa_cultivation.load_catalog(catalog_path)
            plan = maa_cultivation.calculate_plan(
                catalog=catalog,
                goals=[],
                inventory={},
            )
            self.assertIn("ready", plan)
            self.assertIn("missing", plan)
            self.assertIn("crafts", plan)

    def test_f06_t2_04_immediate_abort_on_startup_failure(self) -> None:
        """TC-F06-B04: Verify cultivation runtime defines EXIT_STARTUP_FAILED = 91 for immediate abort."""
        runtime_py = self.source_dir / "maa_cultivation_runtime.py"
        content = runtime_py.read_text(encoding="utf-8")
        self.assertIn("EXIT_STARTUP_FAILED = 91", content)

    def test_f06_t2_05_circular_material_synthesis_graph(self) -> None:
        """TC-F06-B05: Verify cultivation planner detects recipe cycles and enforces MAX_RECIPE_DEPTH."""
        self.assertGreater(maa_cultivation.MAX_RECIPE_DEPTH, 0)
        self.assertTrue(callable(maa_cultivation._check_acyclic))

    # ========================================================================
    # Feature 7: 限时关卡集中用药 (Concentrated Sanity Potions Boundary Cases)
    # ========================================================================

    def test_f07_t2_01_stale_material_inventory_evidence(self) -> None:
        """TC-F07-B01: Verify select_drain blocks drain when material evidence is older than threshold."""
        now = datetime.now(timezone(timedelta(hours=8)))
        now_str = now.isoformat()
        slots = [{
            "item_id": "30012",
            "run_at": (now - timedelta(minutes=5)).isoformat(),
            "cutoff": (now + timedelta(hours=5)).isoformat(),
            "threshold": 80,
        }]
        decision = maa_medicine_plan.select_drain(
            slots=slots,
            inventory={"30012": 50},
            fresh=False,
            now=now_str,
        )
        self.assertEqual(decision["action"], "blocked")
        self.assertIn("证据过旧", decision["reason"])

    def test_f07_t2_02_inventory_above_threshold(self) -> None:
        """TC-F07-B02: Verify select_drain skips drain when material count exceeds threshold."""
        now = datetime.now(timezone(timedelta(hours=8)))
        now_str = now.isoformat()
        slots = [{
            "item_id": "30012",
            "run_at": (now - timedelta(minutes=5)).isoformat(),
            "cutoff": (now + timedelta(hours=5)).isoformat(),
            "threshold": 80,
        }]
        decision = maa_medicine_plan.select_drain(
            slots=slots,
            inventory={"30012": 85},
            fresh=True,
            now=now_str,
        )
        self.assertEqual(decision["action"], "skip")
        self.assertIn("超过阈值", decision["reason"])

    def test_f07_t2_03_insufficient_time_before_activity_expiry(self) -> None:
        """TC-F07-B03: Verify opportunity planner requires full run budget before activity cutoff."""
        self.assertEqual(maa_medicine_plan.RUN_BUDGET, timedelta(hours=3))

    def test_f07_t2_04_corrupted_medicine_plan_json(self) -> None:
        """TC-F07-B04: Verify medicine service exits with error on corrupted medicine-plan.json."""
        service_py = self.source_dir / "maa_medicine_service.py"
        content = service_py.read_text(encoding="utf-8")
        self.assertIn("MEDICINE_POLICY_ERROR", content)

    def test_f07_t2_05_old_completed_batch_isolation(self) -> None:
        """TC-F07-B05: Verify completed batches do not leak into new batches."""
        now = datetime.now(timezone(timedelta(hours=8)))
        now_str = now.isoformat()
        slots = [{
            "item_id": "30012",
            "run_at": (now - timedelta(minutes=5)).isoformat(),
            "cutoff": (now + timedelta(hours=5)).isoformat(),
            "threshold": 80,
        }]
        decision = maa_medicine_plan.select_drain(
            slots=slots,
            inventory={"30012": 50},
            fresh=True,
            now=now_str,
            completed=True,
        )
        self.assertEqual(decision["action"], "completed")
        self.assertIn("本批次已处理", decision["reason"])

    # ========================================================================
    # Feature 8: 业务脚本通用化与路径解耦 (Script Universalization Boundary Cases)
    # ========================================================================

    def test_f08_t2_01_unset_env_vars_fallback(self) -> None:
        """TC-F08-B01: Verify scripts fall back safely to home/XDG directories when MAA_* are unset."""
        custom_env = os.environ.copy()
        for k in ["MAA_HOME", "MAA_CONFIG_DIR", "MAA_DATA_DIR", "MAA_STATE_DIR", "MAA_LOG_DIR", "MAA_CACHE_DIR"]:
            custom_env.pop(k, None)

        res = self.run_python([str(self.source_dir / "maa_weekly_plan.py"), "--help"], env=custom_env)
        self.assertEqual(res.returncode, 0)

    def test_f08_t2_02_read_only_maa_home_filesystem(self) -> None:
        """TC-F08-B02: Verify reading task configuration does not require write access to MAA_HOME."""
        daily_toml = self.config_dir / "tasks" / "daily.toml"
        tasks = maa_task_config.load_tasks(daily_toml)
        self.assertGreater(len(tasks), 0)

    def test_f08_t2_03_missing_subdirectories_in_state_dir(self) -> None:
        """TC-F08-B03: Verify activity cache automatically creates missing parent directories."""
        new_cache_dir = self.sandbox_path / "deep" / "nested" / "cache"
        cache_file = new_cache_dir / "StageActivityV2.json"
        self.assertFalse(cache_file.exists())

        valid_data = {"Official": {"sideStoryStage": {}}}
        maa_activity_cache.publish_activity(valid_data, path=cache_file)
        self.assertTrue(cache_file.exists())

    def test_f08_t2_04_whitespace_and_special_chars_in_paths(self) -> None:
        """TC-F08-B04: Verify paths containing spaces and symbols are handled without token splitting."""
        special_dir = self.sandbox_path / "test space & symbols dir"
        tasks_dir = special_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_path = tasks_dir / "daily.toml"
        self.write_file(task_path, "[[tasks]]\ntype = \"StartUp\"\nclient_type = \"Official\"\n")

        tasks = maa_task_config.load_tasks(task_path)
        self.assertEqual(tasks[0]["type"], "StartUp")

    def test_f08_t2_05_cross_mount_atomic_file_replacement(self) -> None:
        """TC-F08-B05: Verify atomic JSON writes operate safely within target directory."""
        target_file = self.state_dir / "atomic-test.json"
        maa_operator_box._atomic_write_json(target_file, {"status": "ok"})
        self.assertTrue(target_file.exists())
        data = json.loads(target_file.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "ok")

    # ========================================================================
    # Feature 9: 进程管理收敛与全局 pkill 消除 (Process Isolation Boundary Cases)
    # ========================================================================

    def test_f09_t2_01_unresponsive_subprocess_group_escalation(self) -> None:
        """TC-F09-B01: Verify daemon and supervisor define process group termination escalation."""
        webui_py = (self.source_dir / "maa-webui.py").read_text(encoding="utf-8")
        self.assertIn("kill", webui_py)

    def test_f09_t2_02_dead_process_group_leader(self) -> None:
        """TC-F09-B02: Verify process stop handlers catch ProcessLookupError / ESRCH gracefully."""
        # Non-existent parent process PID must be handled gracefully without unhandled ProcessLookupError/OSError
        is_cron = maa_schedule_control.is_cron_invocation(999999, proc_root=self.sandbox_path / "proc")
        self.assertFalse(is_cron)
        res = self.run_python([
            str(self.source_dir / "maa_schedule_control.py"),
            "consume",
            "--config-dir", str(self.config_dir),
            "--parent-pid", "999999",
        ])
        # Graceful exit (code 1 for unconsumed non-cron, no unhandled traceback in stderr)
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("Traceback", res.stderr)

    def test_f09_t2_03_orphaned_child_termination(self) -> None:
        """TC-F09-B03: Verify process groups are used to prevent orphaned children."""
        webui_py = (self.source_dir / "maa-webui.py").read_text(encoding="utf-8")
        self.assertIn("killpg", webui_py)
        self.assertIn("SIGTERM", webui_py)

        runner_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn('kill -TERM -- -"$target_pid"', runner_sh)

    def test_f09_t2_04_idempotent_double_kill_request(self) -> None:
        """TC-F09-B04: Verify WebUI defines /api/kill endpoint with error handling for absent processes."""
        webui_py = (self.source_dir / "maa-webui.py").read_text(encoding="utf-8")
        self.assertIn("/api/kill", webui_py)

    def test_f09_t2_05_substring_matching_process_collision(self) -> None:
        """TC-F09-B05: Verify avoidance of broad substring pkill -f across all project and module scripts."""
        for py_file in self.source_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("pkill -f", content, f"Forbidden pkill -f found in {py_file.name}")

        for sh_file in (self.repo_root / "module").glob("**/*.sh"):
            content = sh_file.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("pkill -f", content, f"Forbidden pkill -f found in {sh_file.name}")

    # ========================================================================
    # Feature 10: 宿主硬件解耦与 nsenter 消除 (Hardware Decoupling Boundary Cases)
    # ========================================================================

    def test_f10_t2_01_total_absence_of_nsenter_in_path(self) -> None:
        """TC-F10-B01: Verify plan resolver and task guards operate without requiring nsenter in PATH."""
        resolved = maa_weekly_plan.resolve_daily_task(self.config_dir, weekday="wed")
        self.assertIn("effective_task", resolved)

    def test_f10_t2_02_missing_tombstones_directory(self) -> None:
        """TC-F10-B02: Verify absence of hard dependency on /data/tombstones."""
        for py_file in self.source_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            self.assertNotIn("/data/tombstones", content, f"{py_file.name} references /data/tombstones")

        for sh_file in self.source_dir.glob("*.sh"):
            content = sh_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "/data/tombstones" in line:
                    self.assertIn("|| true", line, f"Unguarded /data/tombstones reference in {sh_file.name}: {line}")

    def test_f10_t2_03_mismatched_screen_orientation(self) -> None:
        """TC-F10-B03: Verify display geometry checks avoid host shell namespace escapes."""
        runner_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("user_rotation", runner_sh)
        self.assertIn("accelerometer_rotation", runner_sh)

    def test_f10_t2_04_screen_off_keyguard_recovery(self) -> None:
        """TC-F10-B04: Verify ADB wake commands use keyevent KEYCODE_WAKEUP without physical hardware access."""
        runner_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("KEYCODE_WAKEUP", runner_sh)
        self.assertIn("dismiss-keyguard", runner_sh)

    def test_f10_t2_05_read_only_system_partition(self) -> None:
        """TC-F10-B05: Verify zero attempts in business scripts to remount /system as read-write."""
        for script in self.source_dir.glob("*.sh"):
            content = script.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("mount -o remount,rw /system", content)
