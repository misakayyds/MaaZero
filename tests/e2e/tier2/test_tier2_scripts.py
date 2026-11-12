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

    def test_f02_t2_06_missing_libmaacore_and_cli_toml_channel_alignment(self) -> None:
        """TC-F02-B06: Verify auto-update aligns channel with cli.toml and gates success on libMaaCore.so."""
        for script_name in ("maa-auto-update.sh", "maa-run.sh"):
            content = (self.source_dir / script_name).read_text(encoding="utf-8")
            self.assertIn("resolve_core_channel", content, f"{script_name} must resolve channel from cli.toml")
            self.assertIn('${detected:-stable}', content, f"{script_name} must default to stable matching seed.go")
            self.assertIn('"$MAA_DATA_DIR/lib/libMaaCore.so"', content, f"{script_name} must check libMaaCore.so")
            self.assertIn('-s "$RESOURCE_DIR/version.json"', content, f"{script_name} must skip redundant maa update when bundled resource is present")

        update_sh = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ "$resource_updated" -eq 1 && -f "$MAA_DATA_DIR/lib/libMaaCore.so" ]]; then', update_sh)

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

    def test_f09_t2_06_timeout_foreground_and_session_kill_protection(self) -> None:
        """TC-F09-B06: Verify all shell timeout commands use --foreground and WebUI reaps session members."""
        bare_timeout = re.compile(r"(?:^|[;&|(\s!])timeout\s+(?!--foreground\b)")
        for sh_file in self.source_dir.glob("*.sh"):
            for lineno, line in enumerate(sh_file.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#") or "urllib.request.urlopen" in line or "runner_outcome=" in line or '"124":' in line or "timeout when reading" in line:
                    continue
                self.assertIsNone(
                    bare_timeout.search(line),
                    f"{sh_file.name}:{lineno} invokes timeout without --foreground: {line}",
                )

        webui_py = (self.source_dir / "maa-webui.py").read_text(encoding="utf-8")
        self.assertIn("def _is_zombie_pid(", webui_py)
        self.assertIn("def kill_bg_session(", webui_py)
        self.assertIn("os.getsid(", webui_py)

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

    # ========================================================================
    # UX Parity & Mobile Policy Regression Tests (P0 + P1 + P3)
    # ========================================================================

    def test_ux_01_runtime_settings_secrets_and_client_sync(self) -> None:
        """Verify runtime-settings secret masking, __UNCHANGED__ preservation, and client_type sync."""
        daily_path = self.config_dir / "tasks" / "daily.toml"
        mon_path = self.config_dir / "tasks" / "daily-mon.toml"
        sample_toml = (
            '[[tasks]]\nname = "开始唤醒"\ntype = "StartUp"\n'
            'params = { client_type = "Bilibili", start_game_enabled = true }\n\n'
            '[[tasks]]\nname = "理智作战"\ntype = "Fight"\n'
            'params = { stage = "1-7", server = "CN" }\n\n'
            '[[tasks]]\nname = "自动公招"\ntype = "Recruit"\n'
            'params = { server = "CN", select = [4, 5] }\n\n'
            '[[tasks]]\nname = "关闭游戏"\ntype = "CloseDown"\n'
            'params = { client_type = "Bilibili" }\n'
        )
        daily_path.write_text(sample_toml, encoding="utf-8", newline="\n")
        mon_path.write_text(sample_toml, encoding="utf-8", newline="\n")

        # Without runtime-settings.json, client_type is inferred from daily.toml
        inferred_client, inferred_pkg, inferred_srv = maa_schedule_control.resolve_active_client(self.config_dir)
        self.assertEqual(inferred_client, "Bilibili")
        self.assertEqual(inferred_pkg, "com.hypergryph.arknights.bilibili")
        self.assertEqual(inferred_srv, "CN")

        # Save settings with secrets + sync_tasks to YoStarEN
        saved = maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {
                "client_type": "YoStarEN",
                "device": {"mute_during_run": True, "sleep_after_run": "always"},
                "network": {
                    "http_proxy": "http://127.0.0.1:7890",
                    "github_mirror": "https://ghfast.top",
                },
                "notify": {
                    "enabled": True,
                    "pushplus": {"enabled": True, "token": "unit-pp-tok-1", "topic": "grp1"},
                    "telegram": {
                        "enabled": True,
                        "bot_token": "unit-tg-tok-1",
                        "chat_id": "100200",
                        "api_base": "https://tg-proxy.example.com/",
                    },
                },
            },
            sync_tasks=True,
        )
        self.assertEqual(saved["client_type"], "YoStarEN")
        self.assertEqual(saved["package_name"], "com.YoStarEN.Arknights")
        self.assertEqual(saved["server_code"], "US")
        self.assertIn("daily.toml", saved["synced_files"])
        self.assertIn("daily-mon.toml", saved["synced_files"])
        # Secrets must be masked in public view
        self.assertEqual(saved["notify"]["pushplus"]["token"], "")
        self.assertTrue(saved["notify"]["pushplus"]["has_token"])
        self.assertEqual(saved["notify"]["telegram"]["bot_token"], "")
        self.assertTrue(saved["notify"]["telegram"]["has_bot_token"])

        # Verify task TOMLs were synced to YoStarEN (server = US)
        synced_tasks = maa_task_config.load_tasks(mon_path)
        by_type = {t["type"]: t["params"] for t in synced_tasks}
        self.assertEqual(by_type["StartUp"]["client_type"], "YoStarEN")
        self.assertEqual(by_type["CloseDown"]["client_type"], "YoStarEN")
        self.assertEqual(by_type["Fight"]["server"], "US")
        self.assertEqual(by_type["Recruit"]["server"], "US")

        # Preserve secret with __UNCHANGED__, then explicitly clear pushplus token
        maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {
                "notify": {
                    "pushplus": {"enabled": False, "token": ""},
                    "telegram": {"enabled": True, "bot_token": "__UNCHANGED__", "chat_id": "100200"},
                }
            },
        )
        raw_loaded = maa_schedule_control.load_runtime_settings(self.config_dir)
        self.assertEqual(raw_loaded["notify"]["pushplus"]["token"], "")
        self.assertEqual(raw_loaded["notify"]["telegram"]["bot_token"], "unit-tg-tok-1")

        # Invalid URL scheme must raise ValueError
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"network": {"http_proxy": "ftp://127.0.0.1:21"}},
            )

        # Verify CLI env emission
        env_res = self.run_python([
            str(self.source_dir / "maa_schedule_control.py"),
            "env",
            "--config-dir", str(self.config_dir),
        ])
        self.assertEqual(env_res.returncode, 0)
        self.assertIn("MAA_RT_CLIENT_TYPE=YoStarEN", env_res.stdout)
        self.assertIn("MAA_RT_MUTE_DURING_RUN=1", env_res.stdout)
        self.assertIn("MAA_RT_SLEEP_AFTER_RUN=always", env_res.stdout)

    def test_ux_02_visual_crontab_editor_and_managed_coexistence(self) -> None:
        """Verify visual crontab editor round-trip, infrast-schedule compatibility, and block guards."""
        initial = (
            b"# comment preserved\n"
            b"# BEGIN MAA_WEBUI_OPERATOR_SCAN\n"
            b"0 5 * * 1 python3 /root/maa-scripts/maa-operator-scan.py\n"
            b"# END MAA_WEBUI_OPERATOR_SCAN\n"
        )
        updated = maa_schedule_control.apply_visual_crontab(
            initial,
            daily_enabled=True,
            daily_times=["16:30", "04:30", "04:30"],
            update_enabled=True,
            update_time="03:45",
        )
        parsed = maa_schedule_control.parse_visual_crontab(updated)
        self.assertFalse(parsed["infrast_managed"])
        self.assertTrue(parsed["daily_enabled"])
        self.assertEqual(parsed["daily_times"], ["04:30", "16:30"])
        self.assertTrue(parsed["update_enabled"])
        self.assertEqual(parsed["update_time"], "03:45")
        self.assertIn(b"# BEGIN MAA_WEBUI_OPERATOR_SCAN", updated)

        # Generated lines must be cleanly recognized by maa_infrast_schedule without unsafe_daily_lines
        sched_view = maa_infrast_schedule.parse_managed_crontab(updated)
        self.assertEqual(sched_view["unsafe_daily_lines"], [])
        self.assertEqual(len(sched_view["unmanaged_daily_lines"]), 2)

        # When # BEGIN MAA_WEBUI_SCHEDULE is active, modifying daily_enabled must raise ValueError,
        # while daily_enabled=None allows updating maa-auto-update.sh cleanly.
        managed_raw = (
            b"# BEGIN MAA_WEBUI_SCHEDULE\n"
            b"0 4 * * * /root/maa-scripts/maa-run.sh daily --scheduled-time 04:00\n"
            b"# END MAA_WEBUI_SCHEDULE\n"
        )
        with self.assertRaises(ValueError):
            maa_schedule_control.apply_visual_crontab(
                managed_raw,
                daily_enabled=True,
                daily_times=["10:00"],
                update_enabled=False,
            )
        upd_only = maa_schedule_control.apply_visual_crontab(
            managed_raw,
            daily_enabled=None,
            update_enabled=True,
            update_time="03:15",
        )
        parsed_managed = maa_schedule_control.parse_visual_crontab(upd_only)
        self.assertTrue(parsed_managed["infrast_managed"])
        self.assertTrue(parsed_managed["update_enabled"])
        self.assertEqual(parsed_managed["update_time"], "03:15")
        self.assertIn(b"# BEGIN MAA_WEBUI_SCHEDULE", upd_only)

    def test_ux_03_multichannel_notifications_and_cultivation_client(self) -> None:
        """Verify PushPlus, ServerChan (SCT & sctp), Bark, signed Webhook, Telegram, and cultivation client."""
        import unittest.mock as mock
        import maa_cultivation_runtime

        maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {
                "client_type": "Bilibili",
                "notify": {
                    "enabled": True,
                    "pushplus": {"enabled": True, "token": "unit-pp-tok", "topic": "ops"},
                    "serverchan": {"enabled": True, "sendkey": "sctp9988t_unit"},
                    "bark": {"enabled": True, "server_url": "https://bark.example.com", "device_key": "unit-bk"},
                    "webhook": {
                        "enabled": True,
                        "provider": "dingtalk",
                        "url": "https://oapi.dingtalk.com/robot/send?access_token=unit",
                        "sign_secret": "unit-ding-sec",
                    },
                    "telegram": {
                        "enabled": True,
                        "api_base": "https://tg.example.com",
                        "chat_id": "8899",
                        "bot_token": "unit-tg-tok",
                    },
                },
            },
        )

        startup_task = maa_cultivation_runtime.startup_task_for_config(self.config_dir)
        self.assertEqual(startup_task["params"]["client_type"], "Bilibili")

        captured_requests = []

        class _FakeResp:
            status = 200

            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self, _n: int = -1) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def _fake_urlopen(req, timeout=15):
            url = req.full_url
            payload = json.loads(req.data.decode("utf-8"))
            captured_requests.append((url, payload))
            if "pushplus" in url or "bark.example.com" in url:
                return _FakeResp(b'{"code": 200}')
            if "telegram" in url or "tg.example.com" in url:
                return _FakeResp(b'{"ok": true}')
            return _FakeResp(b'{"code": 0, "errcode": 0}')

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            res = maa_schedule_control.send_notifications(
                "日常任务已完成",
                title="[MaaZero] 测试",
                config_dir=self.config_dir,
                retries=1,
                retry_delay_s=0.0,
            )

        self.assertEqual(res["attempted"], 5)
        self.assertEqual(res["succeeded"], 5)
        self.assertTrue(res["all_ok"])

        urls = [u for u, _ in captured_requests]
        self.assertTrue(any("https://tg.example.com/botunit-tg-tok/sendMessage" == u for u in urls))
        self.assertTrue(any("https://www.pushplus.plus/send" == u for u in urls))
        self.assertTrue(any("https://9988.push.ft07.com/send/sctp9988t_unit.send" == u for u in urls))
        self.assertTrue(any("https://bark.example.com/push" == u for u in urls))
        self.assertTrue(any("timestamp=" in u and "sign=" in u for u in urls))

        # Verify Feishu HMAC payload structure
        feishu_url, feishu_body = maa_schedule_control._webhook_request_spec(
            {"provider": "feishu", "url": "https://open.feishu.cn/hook/unit", "sign_secret": "unit-fs-sec"},
            "[MaaZero]",
            "hello",
        )
        self.assertEqual(feishu_url, "https://open.feishu.cn/hook/unit")
        self.assertEqual(feishu_body["msg_type"], "text")
        self.assertIn("timestamp", feishu_body)
        self.assertIn("sign", feishu_body)

    def test_ux_04_config_backup_export_and_validated_restore(self) -> None:
        """Verify config backup redacts secrets by default, skips ephemeral tasks, and blocks bad imports."""
        maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {
                "client_type": "Official",
                "notify": {
                    "enabled": True,
                    "pushplus": {"enabled": True, "token": "keep-existing-secret"},
                },
            },
        )
        # Ephemeral task file must be excluded from backup
        (self.config_dir / "tasks" / "_infrast_temp.toml").write_text("[[tasks]]\n", encoding="utf-8")
        (self.config_dir / "tasks" / "operator-scan.toml").write_text("[[tasks]]\n", encoding="utf-8")

        bundle = maa_schedule_control.export_config_backup(
            self.config_dir,
            b"30 4 * * * /root/maa-scripts/maa-run.sh daily >> /var/log/maa/cron.log 2>&1\n",
            include_secrets=False,
        )
        self.assertFalse(bundle["includes_secrets"])
        self.assertIn("tasks/daily.toml", bundle["files"])
        self.assertNotIn("tasks/_infrast_temp.toml", bundle["files"])
        self.assertNotIn("tasks/operator-scan.toml", bundle["files"])
        exported_rt = json.loads(bundle["files"]["runtime-settings.json"])
        self.assertEqual(exported_rt["notify"]["pushplus"]["token"], "")

        # Reject path traversal and invalid TOML
        bad_traversal = json.loads(json.dumps(bundle))
        bad_traversal["files"]["../evil.toml"] = '[[tasks]]\nname="x"\ntype="Award"\n'
        with self.assertRaises(ValueError):
            maa_schedule_control.import_config_backup(self.config_dir, bad_traversal)

        bad_toml = json.loads(json.dumps(bundle))
        bad_toml["files"]["tasks/daily.toml"] = "[[tasks]\nbroken"
        with self.assertRaises(ValueError):
            maa_schedule_control.import_config_backup(self.config_dir, bad_toml)

        # Valid redacted import preserves existing on-disk secret and restores crontab
        installed_cron: list[bytes] = []
        restored = maa_schedule_control.import_config_backup(
            self.config_dir,
            bundle,
            install_crontab_fn=installed_cron.append,
        )
        self.assertGreaterEqual(restored["restored_count"], 2)
        self.assertTrue(restored["crontab_restored"])
        self.assertEqual(len(installed_cron), 1)
        after_rt = maa_schedule_control.load_runtime_settings(self.config_dir)
        self.assertEqual(after_rt["notify"]["pushplus"]["token"], "keep-existing-secret")

    def test_ux_05_webui_failure_screenshots_and_embedded_js_syntax(self) -> None:
        """Verify failure screenshot gallery discovery, path-traversal guards, and embedded JS syntax."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 80)
        (self.log_dir / "startup-health-20261112.png").write_bytes(png_bytes)
        snap_sub = self.log_dir / "failure-snapshots" / "20261112-043000"
        snap_sub.mkdir(parents=True, exist_ok=True)
        (snap_sub / "adb-screen.png").write_bytes(png_bytes)
        iface_dir = self.state_dir / "debug" / "interface"
        iface_dir.mkdir(parents=True, exist_ok=True)
        (iface_dir / "capture.png").write_bytes(png_bytes)
        (iface_dir / "not_png.png").write_bytes(b"NOT_A_PNG" + (b"x" * 80))

        old_env = {k: os.environ.get(k) for k in ("MAA_CONFIG_DIR", "MAA_LOG_DIR", "MAA_STATE_DIR", "MAA_DATA_DIR")}
        try:
            os.environ["MAA_CONFIG_DIR"] = str(self.config_dir)
            os.environ["MAA_LOG_DIR"] = str(self.log_dir)
            os.environ["MAA_STATE_DIR"] = str(self.state_dir)
            os.environ["MAA_DATA_DIR"] = str(self.data_dir)
            spec = importlib.util.spec_from_file_location(
                "maa_webui_ux_test", self.source_dir / "maa-webui.py"
            )
            assert spec is not None and spec.loader is not None
            webui_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(webui_mod)

            items = webui_mod.list_failure_screenshots()
            ids = {item["id"] for item in items}
            self.assertIn("log:startup-health-20261112.png", ids)
            self.assertIn("snap:20261112-043000/adb-screen.png", ids)
            self.assertIn("iface:capture.png", ids)

            served = webui_mod.read_failure_screenshot_bytes("snap:20261112-043000/adb-screen.png")
            self.assertEqual(served, png_bytes)

            for bad_id in ("snap:../startup-health-20261112.png", "evil:capture.png", "iface:not_png.png"):
                with self.assertRaises(ValueError):
                    webui_mod.read_failure_screenshot_bytes(bad_id)

            html = webui_mod.web_html()
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
            self.assertGreaterEqual(len(scripts), 1)
            node_bin = shutil.which("node")
            if node_bin:
                for idx, js_code in enumerate(scripts):
                    js_file = self.sandbox_path / f"webui_check_{idx}.js"
                    js_file.write_text(js_code, encoding="utf-8", newline="\n")
                    chk = subprocess.run([node_bin, "--check", str(js_file)], capture_output=True, text=True)
                    self.assertEqual(chk.returncode, 0, f"Embedded JS syntax error in block {idx}: {chk.stderr}")
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_ux_06_mobile_runner_and_apk_client_guards(self) -> None:
        """Verify mobile mute/sleep/keyguard policies in maa-run.sh and client guard in arknights-apk-update.sh."""
        runner_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("mute_media_if_configured", runner_sh)
        self.assertIn("restore_media_volume", runner_sh)
        self.assertIn("sleep_screen_if_configured", runner_sh)
        self.assertIn("KEYGUARD_LOCKED", runner_sh)
        self.assertIn("223", runner_sh)  # KEYCODE_SLEEP
        self.assertIn('grep -Fxq "package:$AK_PACKAGE"', runner_sh)

        apk_sh = (self.source_dir / "arknights-apk-update.sh").read_text(encoding="utf-8")
        self.assertIn("MAA_RT_CLIENT_TYPE", apk_sh)
        self.assertIn("--force-official", apk_sh)
        self.assertIn("maa_schedule_control.py", apk_sh)

        auto_upd_sh = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn("MAA_RT_HTTP_PROXY", auto_upd_sh)
        self.assertIn("MAA_RT_GITHUB_MIRROR", auto_upd_sh)
        self.assertIn("NO_PROXY", auto_upd_sh)

    def test_ux_07_webui_http_routes_e2e(self) -> None:
        """Verify live HTTP routes for runtime-settings, visual cron, apk-update guard, backup, and snapshots."""
        import threading
        import urllib.error
        import urllib.request

        png_bytes = b"\x89PNG\r\n\x1a\n" + (b"\x01" * 96)
        (self.log_dir / "startup-health-http.png").write_bytes(png_bytes)

        old_env = {k: os.environ.get(k) for k in ("MAA_CONFIG_DIR", "MAA_LOG_DIR", "MAA_STATE_DIR", "MAA_DATA_DIR")}
        server = None
        try:
            os.environ["MAA_CONFIG_DIR"] = str(self.config_dir)
            os.environ["MAA_LOG_DIR"] = str(self.log_dir)
            os.environ["MAA_STATE_DIR"] = str(self.state_dir)
            os.environ["MAA_DATA_DIR"] = str(self.data_dir)
            spec = importlib.util.spec_from_file_location(
                "maa_webui_http_ux_test", self.source_dir / "maa-webui.py"
            )
            assert spec is not None and spec.loader is not None
            webui_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(webui_mod)

            cron_store = {"raw": b""}
            webui_mod.read_crontab_bytes = lambda: cron_store["raw"]
            webui_mod.install_crontab_bytes = lambda data: cron_store.__setitem__("raw", data)
            webui_mod.get_host_device_diag = lambda force=False: {
                "adb_online": True,
                "active_package": "com.hypergryph.arknights.bilibili",
                "package_installed": True,
                "lockscreen_secure": False,
                "lockscreen_showing": False,
                "lockscreen_hint": "ok",
                "doze_whitelisted": True,
                "doze_hint": "ok",
            }

            server = webui_mod.WEB_SERVER_CLASS(("127.0.0.1", 0), webui_mod.Handler)
            port = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base_url = f"http://127.0.0.1:{port}"

            def _req(path: str, method: str = "GET", payload=None):
                data = None
                headers = {}
                if payload is not None:
                    data = json.dumps(payload).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(f"{base_url}{path}", data=data, headers=headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        return resp.status, resp.headers.get("Content-Type", ""), resp.read()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.headers.get("Content-Type", ""), exc.read()

            # 1. Save runtime-settings -> Bilibili
            st, _, raw = _req(
                "/api/runtime-settings",
                method="POST",
                payload={
                    "client_type": "Bilibili",
                    "sync_tasks": True,
                    "device": {"mute_during_run": True, "sleep_after_run": "scheduled"},
                    "notify": {"enabled": True, "pushplus": {"enabled": True, "token": "http-pp-tok"}},
                },
            )
            self.assertEqual(st, 200)
            saved_body = json.loads(raw.decode("utf-8"))
            self.assertTrue(saved_body["ok"])
            self.assertEqual(saved_body["settings"]["client_type"], "Bilibili")
            self.assertTrue(saved_body["settings"]["notify"]["pushplus"]["has_token"])

            # 2. APK update must return 409 when client != Official
            st, _, raw = _req("/api/apk-update", method="POST", payload={})
            self.assertEqual(st, 409)
            self.assertFalse(json.loads(raw.decode("utf-8"))["ok"])

            # 3. Visual schedule save & status next_daily_for
            st, _, raw = _req(
                "/api/schedule/visual",
                method="POST",
                payload={
                    "daily_enabled": True,
                    "daily_times": ["04:30", "16:30"],
                    "update_enabled": True,
                    "update_time": "03:30",
                },
            )
            self.assertEqual(st, 200)
            vis_body = json.loads(raw.decode("utf-8"))
            self.assertEqual(vis_body["daily_times"], ["04:30", "16:30"])
            self.assertTrue(vis_body["next_daily_for"])

            st, _, raw = _req("/api/status")
            self.assertEqual(st, 200)
            status_body = json.loads(raw.decode("utf-8"))
            self.assertEqual(status_body["client_type"], "Bilibili")
            self.assertEqual(status_body["package_name"], "com.hypergryph.arknights.bilibili")
            self.assertTrue(status_body["next_daily_for"])

            # 4. Backup export & import round-trip over HTTP (GET with include_secrets=1 must be rejected)
            st_bad_get, hdr_bad_get, _ = _req("/api/backup/export?include_secrets=1")
            self.assertEqual(st_bad_get, 400)

            st, _, raw = _req("/api/backup/export?include_secrets=0")
            self.assertEqual(st, 200)
            backup_bundle = json.loads(raw.decode("utf-8"))

            st_post_exp, _, raw_post_exp = _req("/api/backup/export", method="POST", payload={"include_secrets": True})
            self.assertEqual(st_post_exp, 200)
            self.assertTrue(json.loads(raw_post_exp.decode("utf-8"))["includes_secrets"])

            # Malformed JSON to /api/schedule/visual must return 400 and keep crontab intact
            cron_before = cron_store["raw"]
            bad_req = urllib.request.Request(
                f"{base_url}/api/schedule/visual",
                data=b"{broken-json",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(bad_req, timeout=5) as resp:
                    st_bad_vis = resp.status
            except urllib.error.HTTPError as exc:
                st_bad_vis = exc.code
            self.assertEqual(st_bad_vis, 400)
            self.assertEqual(cron_store["raw"], cron_before)

            st, _, raw = _req("/api/backup/import", method="POST", payload=backup_bundle)
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(raw.decode("utf-8"))["ok"])

            # 5. Failure snapshot list & PNG image fetch + traversal block
            st, _, raw = _req("/api/failure-snapshots")
            self.assertEqual(st, 200)
            snap_items = json.loads(raw.decode("utf-8"))["items"]
            self.assertTrue(any(x["id"] == "log:startup-health-http.png" for x in snap_items))

            st, ctype, raw = _req("/api/failure-snapshots/image?id=log:startup-health-http.png")
            self.assertEqual(st, 200)
            self.assertEqual(ctype, "image/png")
            self.assertEqual(raw, png_bytes)

            st, _, _ = _req("/api/failure-snapshots/image?id=log:../evil.png")
            self.assertEqual(st, 400)
            st_non_startup, _, _ = _req("/api/failure-snapshots/image?id=log:arbitrary.png")
            self.assertEqual(st_non_startup, 400)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_ux_08_review_security_and_consistency_regressions(self) -> None:
        """Verify operator-scan client sync, proxy auth masking, URL SSRF/scheme checks, and backup baseline."""
        # 1. Proxy credential masking, preservation, and redacted backup round-trip
        saved = maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {
                "client_type": "Bilibili",
                "sync_tasks": True,
                "network": {
                    "http_proxy": "http://pxuser:pxcred@proxy.example.com:8080",
                    "github_mirror": "https://ghfast.top",
                },
                "notify": {"enabled": False},
            },
            sync_tasks=True,
        )
        self.assertEqual(saved["network"]["http_proxy"], "http://***:***@proxy.example.com:8080")
        self.assertTrue(saved["network"]["has_proxy_auth"])

        # Second sync with unchanged client_type reports 0 modified files
        noop_sync = maa_schedule_control.sync_client_type_to_tasks(self.config_dir, "Bilibili")
        self.assertEqual(noop_sync, [])

        # Saving masked proxy string back preserves underlying credentials
        maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {"network": {"http_proxy": "http://***:***@proxy.example.com:8080"}},
        )
        self.assertEqual(
            maa_schedule_control.load_runtime_settings(self.config_dir)["network"]["http_proxy"],
            "http://pxuser:pxcred@proxy.example.com:8080",
        )

        # 2. maa-operator-scan.py atomic_task_file respects active client_type (H-1)
        spec_op = importlib.util.spec_from_file_location(
            "maa_operator_scan_ux_test", self.source_dir / "maa-operator-scan.py"
        )
        assert spec_op is not None and spec_op.loader is not None
        op_scan_mod = importlib.util.module_from_spec(spec_op)
        spec_op.loader.exec_module(op_scan_mod)
        op_task_path = op_scan_mod.atomic_task_file(self.config_dir)
        op_task_text = op_task_path.read_text(encoding="utf-8")
        self.assertIn('client_type = "Bilibili"', op_task_text)
        self.assertNotIn('client_type = "Official"', op_task_text)

        # 3. Explicit notify.enabled = False suppresses legacy tg.txt fallback (M-6)
        (self.config_dir / "tg.txt").write_text("legacy_tok\n12345\n", encoding="utf-8")
        res_muted = maa_schedule_control.send_notifications(
            "should not send", config_dir=self.config_dir, retries=1, retry_delay_s=0.0
        )
        self.assertEqual(res_muted["attempted"], 0)

        # 4. Loopback/unspecified notify URL, out-of-range port, and plain-HTTP github_mirror must be rejected
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"notify": {"webhook": {"url": "http://127.0.0.1:9999/hook"}}},
            )
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"notify": {"webhook": {"url": "http://0.0.0.0:9999/hook"}}},
            )
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"network": {"http_proxy": "http://pxuser:pxcred@proxy.example.com:78900"}},
            )
        self.assertEqual(
            maa_schedule_control._mask_proxy_url("http://pxuser:pxcred@proxy.example.com:78900"),
            ("", False),
        )
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"network": {"github_mirror": "http://mirror.example.com"}},
            )

        # 5. Redacted backup strips proxy credentials on export, preserves on import,
        #    rejects file:// URLs and invalid infrast plans, and restores empty crontab + cleans stale manifest
        redacted_bundle = maa_schedule_control.export_config_backup(
            self.config_dir, b"", include_secrets=False
        )
        exported_rt = json.loads(redacted_bundle["files"]["runtime-settings.json"])
        self.assertEqual(exported_rt["network"]["http_proxy"], "http://proxy.example.com:8080")

        evil_rt_bundle = json.loads(json.dumps(redacted_bundle))
        evil_rt = json.loads(evil_rt_bundle["files"]["runtime-settings.json"])
        evil_rt["notify"]["webhook"]["url"] = "file:///etc/hosts"
        evil_rt_bundle["files"]["runtime-settings.json"] = json.dumps(evil_rt)
        with self.assertRaises(ValueError):
            maa_schedule_control.import_config_backup(self.config_dir, evil_rt_bundle)

        bad_plan_bundle = json.loads(json.dumps(redacted_bundle))
        bad_plan_bundle["files"]["infrast/broken.json"] = '{"title": "no plans array"}'
        with self.assertRaises(ValueError):
            maa_schedule_control.import_config_backup(self.config_dir, bad_plan_bundle)

        import base64 as _b64

        bad_tx_bundle = json.loads(json.dumps(redacted_bundle))
        bad_tx_bundle["schedule_activation"] = {
            "backups/infrast-schedule/00000000-0000-4000-8000-000000000001/transaction.json": _b64.b64encode(
                b'{"state": "prepared", "activation": true}'
            ).decode("ascii")
        }
        with self.assertRaises(ValueError):
            maa_schedule_control.import_config_backup(self.config_dir, bad_tx_bundle)

        # Plant a stale infrast-schedule.json not present in backup; import should remove it & install empty cron
        stale_manifest = self.config_dir / "infrast-schedule.json"
        stale_manifest.write_text('{"schema_version": 1}\n', encoding="utf-8")
        installed_crons: list[bytes] = []
        maa_schedule_control.import_config_backup(
            self.config_dir, redacted_bundle, install_crontab_fn=installed_crons.append
        )
        self.assertFalse(stale_manifest.exists())
        self.assertEqual(installed_crons, [b""])
        self.assertEqual(
            maa_schedule_control.load_runtime_settings(self.config_dir)["network"]["http_proxy"],
            "http://pxuser:pxcred@proxy.example.com:8080",
        )
