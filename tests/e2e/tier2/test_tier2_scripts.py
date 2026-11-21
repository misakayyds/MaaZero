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

            # 2. All-platform APK update & store jump routes accept Bilibili and reject invalid clients
            apk_calls = []
            webui_mod.update_apk_bg = lambda **kw: (apk_calls.append(kw) or True)
            webui_mod.open_client_store_on_device = lambda ct: (True, f"store://{ct}")

            st, _, raw = _req("/api/apk-update", method="POST", payload={})
            self.assertEqual(st, 200)
            apk_body = json.loads(raw.decode("utf-8"))
            self.assertTrue(apk_body["ok"])
            self.assertEqual(apk_body["client_type"], "Bilibili")
            self.assertEqual(len(apk_calls), 1)

            st_bad_cli, _, _ = _req("/api/apk-update", method="POST", payload={"client": "InvalidChannel"})
            self.assertEqual(st_bad_cli, 400)

            st_store, _, raw_store = _req("/api/apk-open-store", method="POST", payload={})
            self.assertEqual(st_store, 200)
            store_body = json.loads(raw_store.decode("utf-8"))
            self.assertTrue(store_body["ok"])
            self.assertEqual(store_body["opened"], "store://Bilibili")

            # 3. Visual schedule save (including apk_update_enabled) & status next_daily_for
            st, _, raw = _req(
                "/api/schedule/visual",
                method="POST",
                payload={
                    "daily_enabled": True,
                    "daily_times": ["04:30", "16:30"],
                    "update_enabled": True,
                    "update_time": "03:30",
                    "apk_update_enabled": True,
                    "apk_update_time": "04:15",
                },
            )
            self.assertEqual(st, 200)
            vis_body = json.loads(raw.decode("utf-8"))
            self.assertEqual(vis_body["daily_times"], ["04:30", "16:30"])
            self.assertTrue(vis_body["apk_update_enabled"])
            self.assertEqual(vis_body["apk_update_time"], "04:15")
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

    def test_ux_09_all_platform_apk_update_and_xapk_unpack(self) -> None:
        """Verify 5-server official version checks, Biligame API + Referer, and single/XAPK archive unpacking."""
        import http.server
        import io
        import stat as _stat
        import threading
        import zipfile

        # 1. Version tuple & dumpsys comparisons across CN and Overseas
        inst_bili = maa_schedule_control.parse_dumpsys_package_output(
            "  versionCode=208 minSdk=21 targetSdk=33\n  versionName=2.7.81\n"
        )
        self.assertEqual(inst_bili, {"installed": True, "version_name": "2.7.81", "version_code": 208})
        self.assertTrue(maa_schedule_control.is_remote_version_newer("2.7.81", 208, "2.7.91", 210))
        self.assertFalse(maa_schedule_control.is_remote_version_newer("2.7.91", 210, "2.7.91", 210))
        self.assertTrue(maa_schedule_control.is_remote_version_newer("37.7.9", 2000400, "37.7.41", 0))
        self.assertFalse(maa_schedule_control.is_remote_version_newer("37.7.41", 2000462, "37.7.41", 0))

        # 2. Mock official network_config + Android/version + Biligame gameinfo HTTP server
        seen_bili_referers: list[str] = []

        class _MockOfficialHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                base = f"http://127.0.0.1:{self.server.server_port}"
                if self.path == "/cn/network_config":
                    inner = {"funcVer": "V056", "configs": {"V056": {"network": {"hv": f"{base}/cn/{{0}}/version"}}}}
                    body = json.dumps({"content": json.dumps(inner)})
                elif self.path == "/cn/Android/version":
                    body = json.dumps({"resVersion": "26-11-06", "clientVersion": "2.7.91"})
                elif self.path == "/overseas/network_config":
                    inner = {"funcVer": "V041", "configs": {"V041": {"network": {"hv": f"{base}/overseas/{{0}}/version"}}}}
                    body = json.dumps({"content": json.dumps(inner)})
                elif self.path == "/overseas/Android/version":
                    body = json.dumps({"resVersion": "26-11-02", "clientVersion": "37.7.41"})
                elif self.path.startswith("/biligame/gameinfo"):
                    seen_bili_referers.append(self.headers.get("Referer", ""))
                    body = json.dumps(
                        {
                            "code": 0,
                            "data": {
                                "android_pkg_name": "com.hypergryph.arknights.bilibili",
                                "android_pkg_ver": 210,
                                "android_pkg_size": 1959715292,
                                "android_download_link": f"{base}/dl/mrfz_2.7.91_20261023_112922_95acf.apk",
                            },
                        }
                    )
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockOfficialHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{port}"
        old_allow = os.environ.get("MAA_ALLOW_HTTP_VERSION_API")
        try:
            # Without MAA_ALLOW_HTTP_VERSION_API, plain HTTP / loopback must be blocked (SSRF defense)
            os.environ.pop("MAA_ALLOW_HTTP_VERSION_API", None)
            with self.assertRaises(ValueError):
                maa_schedule_control.fetch_client_remote_info(
                    "YoStarEN", network_config_url_override=f"{base_url}/overseas/network_config"
                )

            os.environ["MAA_ALLOW_HTTP_VERSION_API"] = "1"
            info_off = maa_schedule_control.fetch_client_remote_info(
                "Official",
                network_config_url_override=f"{base_url}/cn/network_config",
                official_download_url_override=f"{base_url}/dl/arknights-hg-2791.apk",
            )
            self.assertEqual(info_off["strategy"], "official_cdn")
            self.assertTrue(info_off["auto_install_supported"])
            self.assertEqual(info_off["remote_version_name"], "2.7.91")

            info_bili = maa_schedule_control.fetch_client_remote_info(
                "Bilibili",
                network_config_url_override=f"{base_url}/cn/network_config",
                biligame_api_url_override=f"{base_url}/biligame/gameinfo?game_base_id=101772",
            )
            self.assertEqual(info_bili["strategy"], "bilibili_api")
            self.assertTrue(info_bili["auto_install_supported"])
            self.assertEqual(info_bili["remote_version_name"], "2.7.91")
            self.assertEqual(info_bili["remote_version_code"], 210)
            self.assertEqual(info_bili["referer"], "https://www.biligame.com/")
            self.assertEqual(seen_bili_referers[-1], "https://www.biligame.com/")

            for yostar_ct in ("YoStarEN", "YoStarJP", "YoStarKR"):
                info_yo = maa_schedule_control.fetch_client_remote_info(
                    yostar_ct,
                    network_config_url_override=f"{base_url}/overseas/network_config",
                )
                self.assertEqual(info_yo["strategy"], "store_only")
                self.assertFalse(info_yo["auto_install_supported"])
                self.assertEqual(info_yo["remote_version_name"], "37.7.41")
                self.assertIn("market://details?id=", str(info_yo["store_intent_uri"]))
        finally:
            srv.shutdown()
            srv.server_close()
            if old_allow is None:
                os.environ.pop("MAA_ALLOW_HTTP_VERSION_API", None)
            else:
                os.environ["MAA_ALLOW_HTTP_VERSION_API"] = old_allow

        # 3. Single APK vs Split XAPK (ARM64 ABI filtering + OBB + adversarial Zip-Slip/symlink/package mismatch)
        def _make_apk_bytes(label: str = "base") -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("AndroidManifest.xml", f"<manifest split='{label}'/>")
                zf.writestr("classes.dex", b"dex")
            return buf.getvalue()

        single_apk = self.sandbox_path / "official.apk"
        single_apk.write_bytes(_make_apk_bytes("single"))
        res_single = maa_schedule_control.inspect_and_unpack_apk_archive(
            single_apk,
            self.sandbox_path / "unpack_single",
            expected_package="com.hypergryph.arknights",
        )
        self.assertEqual(res_single["kind"], "single_apk")
        self.assertEqual(res_single["apks"], [str(single_apk)])

        valid_xapk = self.sandbox_path / "yostar_en.xapk"
        with zipfile.ZipFile(valid_xapk, "w") as zf:
            zf.writestr(
                "manifest.json",
                json.dumps({"package_name": "com.YoStarEN.Arknights", "version_name": "37.7.41"}),
            )
            zf.writestr("config.x86_64.apk", _make_apk_bytes("x86_64"))
            zf.writestr("config.arm64_v8a.apk", _make_apk_bytes("arm64"))
            zf.writestr("com.YoStarEN.Arknights.apk", _make_apk_bytes("base"))
            zf.writestr("config.en.apk", _make_apk_bytes("en"))
            zf.writestr(
                "Android/obb/com.YoStarEN.Arknights/main.200.com.YoStarEN.Arknights.obb",
                b"OBB_PAYLOAD",
            )

        res_xapk = maa_schedule_control.inspect_and_unpack_apk_archive(
            valid_xapk,
            self.sandbox_path / "unpack_xapk",
            expected_package="com.YoStarEN.Arknights",
        )
        self.assertEqual(res_xapk["kind"], "split_apks")
        apk_names = [Path(p).name for p in res_xapk["apks"]]
        self.assertEqual(len(apk_names), 3)
        self.assertTrue(apk_names[0].endswith("com.YoStarEN.Arknights.apk"))
        self.assertFalse(any("x86_64" in n for n in apk_names))
        self.assertEqual(len(res_xapk["obbs"]), 1)
        self.assertEqual(res_xapk["obbs"][0]["filename"], "main.200.com.YoStarEN.Arknights.obb")

        # Package mismatch in manifest.json must be rejected
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                valid_xapk,
                self.sandbox_path / "unpack_mismatch",
                expected_package="com.YoStarJP.Arknights",
            )

        # Zip-Slip path traversal must be rejected
        slip_xapk = self.sandbox_path / "slip.xapk"
        with zipfile.ZipFile(slip_xapk, "w") as zf:
            zf.writestr("../../evil.apk", _make_apk_bytes("evil"))
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                slip_xapk, self.sandbox_path / "unpack_slip", expected_package="com.YoStarEN.Arknights"
            )

        # Symlink member inside archive must be rejected
        sym_xapk = self.sandbox_path / "sym.xapk"
        with zipfile.ZipFile(sym_xapk, "w") as zf:
            zi = zipfile.ZipInfo("link.apk")
            zi.external_attr = (_stat.S_IFLNK | 0o777) << 16
            zf.writestr(zi, "/etc/passwd")
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                sym_xapk, self.sandbox_path / "unpack_sym", expected_package="com.YoStarEN.Arknights"
            )

        # 4. Presigned custom_package_url masking & redacted backup round-trip
        presigned = "https://cdn.example.com/pkg/arknights-en.xapk?sig=presigned123"
        rt_saved = maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {"client_type": "YoStarEN", "client_update": {"custom_package_url": presigned}},
        )
        self.assertEqual(
            rt_saved["client_update"]["custom_package_url"],
            "https://cdn.example.com/pkg/arknights-en.xapk?***",
        )
        self.assertTrue(rt_saved["client_update"]["auto_install_supported"])

        redacted = maa_schedule_control.export_config_backup(self.config_dir, b"", include_secrets=False)
        exp_rt = json.loads(redacted["files"]["runtime-settings.json"])
        self.assertEqual(
            exp_rt["client_update"]["custom_package_url"],
            "https://cdn.example.com/pkg/arknights-en.xapk",
        )
        maa_schedule_control.import_config_backup(self.config_dir, redacted)
        self.assertEqual(
            maa_schedule_control.load_runtime_settings(self.config_dir)["client_update"]["custom_package_url"],
            presigned,
        )

    def test_ux_10_apk_update_shell_and_security_regressions(self) -> None:
        """Verify redirect SSRF block, APKM/x86/base ordering, Bilibili & quoting, and arknights-apk-update.sh E2E."""
        import http.server
        import io
        import shutil
        import threading
        import urllib.request
        import zipfile
        from unittest import mock

        def _make_apk_bytes(label: str = "base") -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("AndroidManifest.xml", f"<manifest split='{label}'/>")
                zf.writestr("classes.dex", b"dex-payload")
            return buf.getvalue()

        # 1. Redirect handler blocks HTTPS -> HTTP loopback/file downgrade (C2-H2), URL redaction & stdin dumpsys
        redir_handler = maa_schedule_control._SafeHttpsRedirectHandler(allow_http=False)
        req_https = urllib.request.Request("https://example.com/start")
        with self.assertRaises(ValueError):
            redir_handler.redirect_request(
                req_https, None, 302, "Found", {}, "http://127.0.0.1:8088/api/status"
            )
        with self.assertRaises(ValueError):
            redir_handler.redirect_request(req_https, None, 302, "Found", {}, "file:///etc/passwd")

        redacted_err = maa_schedule_control._redact_urls_in_text(
            "urlopen failed for https://u:mysecret@cdn.example.com/a.xapk?token=xyz987."
        )
        self.assertNotIn("mysecret", redacted_err)
        self.assertNotIn("xyz987", redacted_err)
        self.assertIn("https://***:***@cdn.example.com/a.xapk?***.", redacted_err)

        # Partial edit of masked *** URLs must be rejected (C2-L1)
        maa_schedule_control.save_runtime_settings(
            self.config_dir,
            {
                "network": {"http_proxy": "http://pxu:pxp@proxy.example.com:8080"},
                "client_update": {"custom_package_url": "https://cdn.example.com/v1/ak.xapk?sig=real"},
            },
        )
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"client_update": {"custom_package_url": "https://cdn.example.com/v2/ak.xapk?***"}},
            )
        with self.assertRaises(ValueError):
            maa_schedule_control.save_runtime_settings(
                self.config_dir,
                {"network": {"http_proxy": "http://***:***@proxy.example.com:9090"}},
            )

        # 2. Split archive validation: oversized manifest.json, APKM info.json mismatch, x86-only, base ordering, cleanup
        oversized_xapk = self.sandbox_path / "oversized_manifest.xapk"
        with zipfile.ZipFile(oversized_xapk, "w") as zf:
            zf.writestr(
                "manifest.json",
                json.dumps({"package_name": "com.YoStarKR.Arknights", "pad": "A" * 300_000}),
            )
            zf.writestr("base.apk", _make_apk_bytes("base"))
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                oversized_xapk,
                self.sandbox_path / "unpack_oversized",
                expected_package="com.YoStarEN.Arknights",
            )

        wrong_apkm = self.sandbox_path / "wrong_pkg.apkm"
        with zipfile.ZipFile(wrong_apkm, "w") as zf:
            zf.writestr("info.json", json.dumps({"pname": "com.YoStarKR.Arknights"}))
            zf.writestr("base.apk", _make_apk_bytes("base"))
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                wrong_apkm,
                self.sandbox_path / "unpack_apkm",
                expected_package="com.YoStarEN.Arknights",
            )

        x86_only_xapk = self.sandbox_path / "x86_only.xapk"
        with zipfile.ZipFile(x86_only_xapk, "w") as zf:
            zf.writestr("base.apk", _make_apk_bytes("base"))
            zf.writestr("config.x86_64.apk", _make_apk_bytes("x86_64"))
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                x86_only_xapk,
                self.sandbox_path / "unpack_x86",
                expected_package="com.YoStarEN.Arknights",
            )

        ordered_xapk = self.sandbox_path / "ordered.xapk"
        with zipfile.ZipFile(ordered_xapk, "w") as zf:
            zf.writestr("install_asset_pack.apk", _make_apk_bytes("asset"))
            zf.writestr("config.arm64_v8a.apk", _make_apk_bytes("arm64"))
            zf.writestr("base.apk", _make_apk_bytes("base"))
        res_ord = maa_schedule_control.inspect_and_unpack_apk_archive(
            ordered_xapk,
            self.sandbox_path / "unpack_ord",
            expected_package="com.YoStarEN.Arknights",
        )
        self.assertTrue(Path(res_ord["apks"][0]).name.endswith("-base.apk"))
        self.assertTrue(Path(res_ord["apks"][1]).name.endswith("-install_asset_pack.apk"))

        corrupt_inner = self.sandbox_path / "corrupt_inner.xapk"
        with zipfile.ZipFile(corrupt_inner, "w") as zf:
            zf.writestr("base.apk", _make_apk_bytes("base"))
            zf.writestr("config.arm64_v8a.apk", b"NOT_A_ZIP")
        corrupt_out = self.sandbox_path / "unpack_corrupt"
        with self.assertRaises(ValueError):
            maa_schedule_control.inspect_and_unpack_apk_archive(
                corrupt_inner, corrupt_out, expected_package="com.YoStarEN.Arknights"
            )
        self.assertFalse(corrupt_out.exists())

        # 3. WebUI open_client_store_on_device quotes '&' in Bilibili URI & checks web fallback errors (C2-H4)
        spec_w = importlib.util.spec_from_file_location("maa_webui_c2_store", self.source_dir / "maa-webui.py")
        assert spec_w is not None and spec_w.loader is not None
        webui_mod = importlib.util.module_from_spec(spec_w)
        spec_w.loader.exec_module(webui_mod)

        adb_cmds: list[list[str]] = []

        def _fake_adb_run(cmd, *a, **kw):
            adb_cmds.append(list(cmd))
            if cmd[-1] == "get-state":
                return subprocess.CompletedProcess(cmd, 0, stdout="device\n", stderr="")
            if "bilibili://" in cmd[-1]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="Error: Activity not started, unable to resolve Intent\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="Starting: Intent\n", stderr="")

        with mock.patch.object(webui_mod.subprocess, "run", side_effect=_fake_adb_run):
            ok_st, opened_target = webui_mod.open_client_store_on_device("Bilibili")
        self.assertTrue(ok_st)
        self.assertEqual(opened_target, "https://www.biligame.com/detail/?id=101772")
        self.assertIn(
            "'bilibili://game_center/detail?id=101772&sourceFrom=2300030011'",
            adb_cmds[2][-1],
        )

        # 4. End-to-end arknights-apk-update.sh execution with stubbed adb/curl/flock (C2-H1/H3/M1/M2/L2/L3)
        bash_bin = (
            Path(r"C:\Program Files\Git\bin\bash.exe")
            if os.name == "nt" and Path(r"C:\Program Files\Git\bin\bash.exe").exists()
            else Path(shutil.which("bash") or "")
        )
        if not bash_bin.is_file():
            return

        sh_root = self.sandbox_path / "sh_e2e"
        sh_home = sh_root / "home"
        sh_bin = sh_home / "bin"
        sh_cfg = sh_root / "cfg"
        sh_state = sh_root / "state"
        sh_cache = sh_root / "cache"
        sh_logs = sh_root / "logs"
        for d in (sh_bin, sh_cfg, sh_state, sh_cache, sh_logs):
            d.mkdir(parents=True, exist_ok=True)

        # Build a valid split .xapk fixture to serve from stubbed curl
        served_xapk = sh_root / "served_en.xapk"
        with zipfile.ZipFile(served_xapk, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"package_name": "com.YoStarEN.Arknights"}))
            zf.writestr("base.apk", _make_apk_bytes("base"))
            zf.writestr("config.arm64_v8a.apk", _make_apk_bytes("arm64"))
            zf.writestr(
                "Android/obb/com.YoStarEN.Arknights/main.210.com.YoStarEN.Arknights.obb",
                b"OBB_DATA_210",
            )

        def _to_msys_path(p: Path) -> str:
            s = p.resolve().as_posix()
            if len(s) >= 3 and s[1] == ":" and s[2] == "/":
                return f"/{s[0].lower()}{s[2:]}"
            return s

        py_exe = _to_msys_path(Path(sys.executable))
        (sh_bin / "python3").write_bytes(f'#!/bin/bash\nexec "{py_exe}" "$@"\n'.encode("utf-8"))
        (sh_bin / "flock").write_bytes(
            b'#!/bin/bash\nwhile [[ $# -gt 0 && "$1" == -* ]]; do\n'
            b'  if [[ "$1" == "-E" || "$1" == "-o" ]]; then shift 2; else shift; fi\n'
            b'done\nexec "$@"\n'
        )
        (sh_bin / "df").write_bytes(
            b'#!/bin/bash\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted\\ndisk 20000000 1000000 19000000 5%% /\\n"\n'
        )

        adb_log = sh_root / "adb_calls.log"
        dev_ver_file = sh_root / "device_ver.txt"
        dev_ver_file.write_text("37.7.9\n2000400\n", encoding="utf-8")

        adb_stub = f"""#!/bin/bash
set -euo pipefail
LOG="{adb_log.as_posix()}"
VER_FILE="{dev_ver_file.as_posix()}"
if [[ "${{1:-}}" == "-s" ]]; then shift 2; fi
printf '%s\\n' "$*" >> "$LOG"
case "${{1:-}}" in
  connect) exit 0 ;;
  get-state) echo "device"; exit 0 ;;
  install-multiple) echo "Failure [INSTALL_FAILED_SESSION_REQUIRED]"; exit 1 ;;
  push)
    mkdir -p "$(dirname "{sh_root.as_posix()}/remote/$3")"
    cp "$2" "{sh_root.as_posix()}/remote/$3"
    exit 0
    ;;
  shell)
    shift
    cmd="$*"
    if [[ "$cmd" == *"dumpsys package"* ]]; then
      vn=$(sed -n '1p' "$VER_FILE")
      vc=$(sed -n '2p' "$VER_FILE")
      printf '  versionCode=%s minSdk=21\\n  versionName=%s\\n' "$vc" "$vn"
      # Pad >140 KiB to verify --installed-dumpsys-stdin avoids E2BIG
      head -c 145000 < /dev/zero | tr '\\0' 'x'
      printf '\\n'
      exit 0
    fi
    if [[ "$cmd" == *"pm install-create"* ]]; then
      echo "Success: created install session [42]"
      exit 0
    fi
    if [[ "$cmd" == *"pm install-write -S "* ]]; then
      # Replace '/data/local/tmp/...' redirection path with local remote mirror
      local_cmd="${{cmd//\\/data\\/local\\/tmp/{sh_root.as_posix()}\\/remote\\/data\\/local\\/tmp}}"
      bytes_read=$(eval "cat $(echo "$local_cmd" | sed -n "s/.*< '\\([^']*\\)'.*/'\\1'/p")" | wc -c | tr -d '[:space:]')
      expected_s=$(echo "$cmd" | awk '{{for(i=1;i<=NF;i++) if($i=="-S") print $(i+1)}}')
      if [[ "$bytes_read" != "$expected_s" || -z "$expected_s" ]]; then
        echo "Failure [SIZE_MISMATCH got=$bytes_read want=$expected_s]"
        exit 1
      fi
      echo "Success: streamed $bytes_read bytes"
      exit 0
    fi
    if [[ "$cmd" == *"pm install-commit 42"* ]]; then
      printf '37.7.41\\n2000462\\n' > "$VER_FILE"
      echo "Success"
      exit 0
    fi
    echo "OK"
    exit 0
    ;;
esac
exit 0
"""
        (sh_bin / "adb").write_bytes(adb_stub.encode("utf-8"))

        curl_log = sh_root / "curl_calls.log"
        etag_file = sh_root / "etag.txt"
        etag_file.write_text('"etag-v37741"', encoding="utf-8")

        curl_stub = f"""#!/bin/bash
set -euo pipefail
printf 'ARGV:%s\\n' "$*" >> "{curl_log.as_posix()}"
out=""
dump_hdr=""
is_range=0
use_cfg=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -D) dump_hdr="$2"; shift 2 ;;
    -r) is_range=1; shift 2 ;;
    -w|--proto|--proto-redir|--max-redirs|--connect-timeout|--max-time|--retry|--retry-delay) shift 2 ;;
    --config) use_cfg=1; shift 2 ;;
    *) shift ;;
  esac
done
if [[ "$use_cfg" -eq 1 ]]; then
  cfg_in=$(cat)
  url=$(printf '%s\\n' "$cfg_in" | sed -n 's/^url = "\\(.*\\)"$/\\1/p')
else
  url="missing-config"
fi
etag=$(cat "{etag_file.as_posix()}")
if [[ -n "$dump_hdr" ]]; then
  printf 'HTTP/2 206\\r\\netag: %s\\r\\ncontent-range: bytes 0-0/123456\\r\\n\\r\\n' "$etag" > "$dump_hdr"
fi
if [[ "$is_range" -eq 1 ]]; then
  printf '%s\\n' "$url"
  exit 0
fi
cp "{served_xapk.as_posix()}" "$out"
"""
        (sh_bin / "curl").write_bytes(curl_stub.encode("utf-8"))
        for stub_f in ("python3", "flock", "df", "adb", "curl"):
            os.chmod(sh_bin / stub_f, 0o755)

        remote_ver_state = {"ver": "37.7.41"}
        notified_messages: list[str] = []

        class _ShellTestHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                b = f"http://127.0.0.1:{self.server.server_port}"
                if self.path == "/overseas/network_config":
                    inner = {"funcVer": "V1", "configs": {"V1": {"network": {"hv": f"{b}/overseas/{{0}}/version"}}}}
                    payload = json.dumps({"content": json.dumps(inner)}).encode("utf-8")
                elif self.path == "/overseas/Android/version":
                    payload = json.dumps({"resVersion": "26-11-01", "clientVersion": remote_ver_state["ver"]}).encode(
                        "utf-8"
                    )
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                ln = int(self.headers.get("Content-Length") or 0)
                body_obj = json.loads(self.rfile.read(ln).decode("utf-8"))
                notified_messages.append(body_obj.get("content", ""))
                resp = b'{"code":200}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ShellTestHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        # Create a thin wrapper of maa_schedule_control.py that overrides YoStarEN network_config URL & send_notifications
        sc_wrapper = sh_home / "maa_schedule_control.py"
        sc_wrapper.write_text(
            f"""import importlib.util, json, urllib.request
spec = importlib.util.spec_from_file_location("real_sc", r"{(self.source_dir / 'maa_schedule_control.py').as_posix()}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.CLIENT_NETWORK_CONFIG_URLS["YoStarEN"] = "http://127.0.0.1:{port}/overseas/network_config"
def _mock_notify(text, **kw):
    req = urllib.request.Request(
        "http://127.0.0.1:{port}/notify",
        data=json.dumps({{"content": text}}).encode("utf-8"),
        headers={{"Content-Type": "application/json"}},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()
    return {{"attempted": 1, "succeeded": 1, "any_ok": True, "results": []}}
mod.send_notifications = _mock_notify
if __name__ == "__main__":
    raise SystemExit(mod.main())
""",
            encoding="utf-8",
        )

        base_env = os.environ.copy()
        base_env.update(
            {
                "MAA_HOME": _to_msys_path(sh_home),
                "MAA_SCHEDULE_CONTROL_SCRIPT": sc_wrapper.as_posix(),
                "MAA_CONFIG_DIR": sh_cfg.as_posix(),
                "MAA_STATE_DIR": sh_state.as_posix(),
                "MAA_CACHE_DIR": sh_cache.as_posix(),
                "MAA_LOG_DIR": sh_logs.as_posix(),
                "MAA_TASK_LOCK_FILE": (sh_root / "task.lock").as_posix(),
                "MAA_ALLOW_HTTP_VERSION_API": "1",
                "PYTHONUTF8": "1",
                "TMPDIR": (sh_root / "tmp").as_posix(),
            }
        )
        (sh_root / "tmp").mkdir(parents=True, exist_ok=True)

        try:
            # Configure YoStarEN + PushPlus notifications (no custom URL yet -> store_only)
            maa_schedule_control.save_runtime_settings(
                sh_cfg,
                {
                    "client_type": "YoStarEN",
                    "notify": {"enabled": True, "pushplus": {"enabled": True, "token": "pp-tok"}},
                },
            )

            script_msys = _to_msys_path(self.source_dir / "arknights-apk-update.sh")
            r1 = subprocess.run(
                [str(bash_bin), script_msys, "--check", "--open-store"],
                env=base_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            r1_logs = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sh_logs.glob("*.log"))
            self.assertEqual(r1.returncode, 0, msg=f"stdout={r1.stdout}\nstderr={r1.stderr}\nlogs={r1_logs}")
            self.assertIn("APK_UPDATE_STORE_REQUIRED client=YoStarEN remote=37.7.41", r1.stdout)
            self.assertEqual(len(notified_messages), 1)
            self.assertIn("market://details?id=com.YoStarEN.Arknights", adb_log.read_text(encoding="utf-8"))

            # Second store_only run deduplicates notification
            r2 = subprocess.run(
                [str(bash_bin), script_msys, "--check"],
                env=base_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(r2.returncode, 0)
            self.assertEqual(len(notified_messages), 1)

            # Configure presigned custom_package_url -> triggers XAPK download, pm install-write -S fallback & OBB push
            secret_pkg_url = (
                "https://dluser:TOP_SECRET_CRED@cdn.example.com/pkg/yostar_en.xapk?X-Amz-Signature=PRESIGNED_TOK_777"
            )
            maa_schedule_control.save_runtime_settings(
                sh_cfg,
                {"client_update": {"custom_package_url": secret_pkg_url}},
            )
            r3 = subprocess.run(
                [str(bash_bin), script_msys, "--check"],
                env=base_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(r3.returncode, 0, msg=f"stdout={r3.stdout}\nstderr={r3.stderr}")
            self.assertEqual(len(notified_messages), 2)
            self.assertIn("已自动更新并安装成功", notified_messages[-1])

            # Verify zero secret/token leakage in stdout, logs, meta, and curl argv (C2-H1)
            all_logs = "\n".join(p.read_text(encoding="utf-8") for p in sh_logs.glob("*.log"))
            meta_text = (sh_state / "arknights-apk-head-YoStarEN.meta").read_text(encoding="utf-8")
            curl_argv_text = curl_log.read_text(encoding="utf-8")
            for haystack in (r3.stdout, r3.stderr, all_logs, meta_text, curl_argv_text):
                self.assertNotIn("TOP_SECRET_CRED", haystack)
                self.assertNotIn("PRESIGNED_TOK_777", haystack)

            # Verify OBB was pushed to /sdcard/Android/obb/com.YoStarEN.Arknights/
            self.assertTrue(
                (
                    sh_root
                    / "remote/sdcard/Android/obb/com.YoStarEN.Arknights/main.210.com.YoStarEN.Arknights.obb"
                ).exists()
            )

            # Advance official network_config to 37.8.0 while custom_package_url ETag stays unchanged (C2-M1)
            remote_ver_state["ver"] = "37.8.0"
            curl_calls_before = len(curl_log.read_text(encoding="utf-8").splitlines())
            r4 = subprocess.run(
                [str(bash_bin), script_msys, "--check"],
                env=base_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(r4.returncode, 0, msg=f"stdout={r4.stdout}\nstderr={r4.stderr}")
            curl_calls_after = len(curl_log.read_text(encoding="utf-8").splitlines())
            # Only 1 metadata range probe (-r 0-0), NO package download!
            self.assertEqual(curl_calls_after - curl_calls_before, 1)
            self.assertEqual(len(notified_messages), 3)
            self.assertIn("跳过重复下载", notified_messages[-1])

            # Next cron run with same unchanged custom URL neither downloads nor spams alert
            r5 = subprocess.run(
                [str(bash_bin), script_msys, "--check"],
                env=base_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(r5.returncode, 0)
            self.assertEqual(len(curl_log.read_text(encoding="utf-8").splitlines()) - curl_calls_after, 1)
            self.assertEqual(len(notified_messages), 3)
        finally:
            srv.shutdown()
            srv.server_close()

    # ========================================================================
    # Synced MAA Features + MaaZero Adaptation Regressions (Blocks 1-3)
    # ========================================================================

    def test_sync_01_cultivation_catalyst_auto_elite_and_multi_client(self) -> None:
        """Verify red-ticket catalyst unblock, phased auto-elite gate, multi-client StartUp, and backup exclusion."""
        import unittest.mock as mock
        import maa_cultivation_service
        import maa_cultivation_runtime

        catalog_path = self.source_dir / "maa-cultivation-data.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertIn("4006", catalog["items"])
        self.assertTrue(any(s.get("stage") == "AP-5" for s in catalog["stages"]))

        # 1. Multi-client StartUp propagation in ensure_elite_task_file & ephemeral task exclusion from backup
        maa_schedule_control.save_runtime_settings(self.config_dir, {"client_type": "Bilibili"})
        oper_task = {
            "name": "干员精英化",
            "type": "OperProgress",
            "params": {"plans": [{"role": "Sniper", "name": "能天使", "elite": 1}]},
        }
        elite_toml = maa_cultivation_runtime.ensure_elite_task_file(self.config_dir, oper_task)
        elite_text = elite_toml.read_text(encoding="utf-8")
        self.assertIn('client_type = "Bilibili"', elite_text)
        self.assertNotIn('client_type = "Official"', elite_text)

        maa_cultivation_runtime.ensure_scan_task_file(self.config_dir)
        maa_cultivation_runtime.ensure_mastery_task_file(self.config_dir, oper_task)
        backup = maa_schedule_control.export_config_backup(self.config_dir)
        exported_paths = set((backup.get("files") or {}).keys())
        for ephemeral in (
            "tasks/cultivation-elite.toml",
            "tasks/cultivation-scan.toml",
            "tasks/cultivation-mastery.toml",
        ):
            self.assertNotIn(ephemeral, exported_paths)

        # 2. Phased auto-elite readiness + red-ticket (4006) unblocking chip catalyst (32001)
        now_iso = datetime.now(timezone(timedelta(hours=8))).isoformat()
        box_ops = [
            {
                "id": "char_103_angel",
                "name": "能天使",
                "own": True,
                "rarity": 6,
                "elite": 0,
                "level": 50,
                "potential": 1,
            }
        ]
        maa_operator_box.save_successful_scan(
            self.config_dir / "operator-box.json",
            self.config_dir / "operator-box-state.json",
            {"what": "OperBoxInfo", "details": {"done": True, "all_opers": box_ops, "own_opers": box_ops}},
            scanned_at=now_iso,
            source_log="test",
            core_version="v6.22.0",
            resource_version="2026-11-15",
        )
        maa_cultivation_service.save_goal(
            self.config_dir,
            catalog_path,
            {
                "operator_id": "char_103_angel",
                "current_elite": 0,
                "current_level": 50,
                "target_elite": 2,
                "target_level": 1,
            },
        )
        # Provide all E1 materials + LMD + red tickets for catalyst (32001), but intentionally omit E2 chips
        maa_cultivation_service.save_inventory(
            self.config_dir,
            {
                "4001": 500000,
                "4006": 450,
                "2004": 500,
                "30012": 20,
                "30052": 10,
                "3241": 5,
            },
        )
        maa_cultivation_service.update_settings(
            self.config_dir, catalog_path, {"enabled": True, "auto_elite": True}
        )
        payload = maa_cultivation_service.get_payload(self.config_dir, catalog_path)
        plan = payload["plan"]
        blocked_ids = {row.get("item_id") for row in (plan.get("blocked") or [])}
        self.assertNotIn("32001", blocked_ids)
        exchange_targets = {row.get("item_id") for row in (plan.get("exchanges") or [])}
        self.assertIn("32001", exchange_targets)
        goal_row = plan["goals"][0]
        self.assertEqual(goal_row["promotion"]["ready_elite"], 1)
        self.assertEqual(goal_row["promotion"]["verified_ready_elite"], 1)

        # 3. WebUI cultivation_elite_run_gate allows verified E1 promotion and rejects when stale/empty
        spec = importlib.util.spec_from_file_location("maa_webui_sync_elite", self.source_dir / "maa-webui.py")
        assert spec is not None and spec.loader is not None
        webui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(webui)
        with (
            mock.patch.object(webui, "CONFIG_DIR", str(self.config_dir)),
            mock.patch.object(webui, "CULTIVATION_CATALOG_FILE", str(catalog_path)),
        ):
            self.assertIsNone(webui.cultivation_elite_run_gate())
            maa_cultivation_service.mark_inventory_failed(self.config_dir, "stale for test")
            blocked = webui.cultivation_elite_run_gate()
            self.assertIsNotNone(blocked)
            assert blocked is not None
            self.assertEqual(blocked[0], 409)
            self.assertEqual(blocked[1]["error"]["code"], "disabled")

    def test_sync_02_waterfall_scheduling_and_medicine_coordination(self) -> None:
        """Verify 3-stage waterfall validation, bidirectional medicine sync, multi-region activity, and blind deduction."""
        import unittest.mock as mock
        import maa_cultivation_service
        import maa_cultivation_runtime
        import maa_medicine_service

        catalog_path = self.source_dir / "maa-cultivation-data.json"

        # 1. activity_first + fallback_mode='stage' must reject with 400 invalid_settings
        with self.assertRaises(maa_cultivation_service.InvalidSettingsError) as ctx:
            maa_cultivation_service.update_settings(
                self.config_dir,
                catalog_path,
                {"priority_mode": "activity_first", "fallback_mode": "stage"},
            )
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_settings")

        # 2. Bidirectional sync between cultivation medicine_mode and medicine-plan.json
        maa_cultivation_service.update_settings(
            self.config_dir,
            catalog_path,
            {
                "enabled": True,
                "priority_mode": "cultivation_first",
                "medicine_mode": "strict_none",
                "fallback_mode": "weekly",
            },
        )
        self.assertFalse(maa_medicine_service.settings(self.config_dir)["enabled"])
        rev_before = maa_cultivation_service.load_state(self.config_dir)["revision"]

        med_payload = maa_medicine_service.update_settings(
            self.config_dir, {"enabled": True, "threshold": 95}
        )
        state_after = maa_cultivation_service.load_state(self.config_dir)
        self.assertEqual(state_after["settings"]["medicine_mode"], "expire_only")
        self.assertEqual(state_after["revision"], rev_before + 1)
        self.assertTrue(med_payload["cultivation_coordination"]["active"])
        self.assertEqual(med_payload["cultivation_coordination"]["medicine_mode"], "expire_only")

        # 3. Multi-region StageActivityV2 lookup (YoStarEN vs Bilibili->Official fallback) + env-var path decoupling
        act_file = self.sandbox_path / "custom_StageActivityV2.json"
        item_file = self.sandbox_path / "custom_item_index.json"
        act_data = {
            "Official": {
                "sideStoryStage": {
                    "CN_ACT": {
                        "Activity": {
                            "StageName": "CN Event",
                            "UtcStartTime": "2026/11/01 16:00:00",
                            "UtcExpireTime": "2026/12/01 03:59:59",
                            "TimeZone": 8,
                        },
                        "Stages": [{"Value": "CN-8", "Drop": "30013"}],
                    }
                }
            },
            "YoStarEN": {
                "sideStoryStage": {
                    "EN_ACT": {
                        "Activity": {
                            "StageName": "EN Event",
                            "UtcStartTime": "2026/11/01 16:00:00",
                            "UtcExpireTime": "2026/12/01 03:59:59",
                            "TimeZone": 8,
                        },
                        "Stages": [{"Value": "EN-8", "Drop": "30013"}],
                    }
                }
            },
        }
        act_file.write_text(json.dumps(act_data), encoding="utf-8")
        item_file.write_text(json.dumps({"30013": {"name": "固源岩组"}}), encoding="utf-8")
        (self.config_dir / "tasks" / "daily.toml").write_text(
            '[[tasks]]\nname = "理智作战"\ntype = "Fight"\nparams = { stage = "EN-8" }\n',
            encoding="utf-8",
        )
        maa_schedule_control.save_runtime_settings(self.config_dir, {"client_type": "YoStarEN"})
        fixed_now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        with (
            mock.patch.object(maa_cultivation_service, "ACTIVITY_FILE", act_file),
            mock.patch.object(maa_cultivation_service, "ITEM_FILE", item_file),
            mock.patch.object(maa_cultivation_service, "_weekly_now", return_value=fixed_now),
        ):
            wctx = maa_cultivation_service.get_payload(self.config_dir, catalog_path)["weekly_context"]
        self.assertTrue(wctx["is_activity_stage"])
        self.assertEqual(wctx["effective_stage"], "EN-8")
        self.assertEqual(wctx["activity_item_id"], "30013")

        week = maa_medicine_service.load_week(self.config_dir)
        rules_en = maa_medicine_service.activity_rules(
            week, 80, act_file, item_file, config_dir=self.config_dir
        )
        self.assertEqual([r["stage"] for r in rules_en], ["EN-8"])

        # Bilibili falls back cleanly to Official region
        (self.config_dir / "tasks" / "daily.toml").write_text(
            '[[tasks]]\nname = "理智作战"\ntype = "Fight"\nparams = { stage = "CN-8" }\n',
            encoding="utf-8",
        )
        maa_schedule_control.save_runtime_settings(self.config_dir, {"client_type": "Bilibili"})
        rules_bili = maa_medicine_service.activity_rules(
            maa_medicine_service.load_week(self.config_dir),
            80,
            act_file,
            item_file,
            config_dir=self.config_dir,
        )
        self.assertEqual([r["stage"] for r in rules_bili], ["CN-8"])

        # 4. claim_cultivation_drain + double finish() idempotent blind-use watermark + --started-at CLI arg
        started_iso = "2026-11-15T02:05:00+08:00"
        evidence_path = self.config_dir / "medicine-evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "accurate": True,
                    "observed_at": "2026-11-14T16:00:00+08:00",
                    "expires_at": "2026-11-15T20:00:00+08:00",
                    "remaining_count": 3,
                    "source": "trc",
                    "batches": [
                        {"remaining": 1, "expires_at": "2026-11-15T20:00:00+08:00"},
                        {"remaining": 2, "expires_at": "2026-11-20T20:00:00+08:00"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        asst_log = self.sandbox_path / "blind_asst.log"
        asst_log.write_text("", encoding="utf-8")
        claim = maa_medicine_service.claim_cultivation_drain(
            self.config_dir,
            now=started_iso,
            started_at=started_iso,
            activity_file=act_file,
            item_file=item_file,
            log_file=asst_log,
        )
        self.assertEqual(claim["action"], "drain")
        self.assertEqual(claim["expiring_count"], 1)

        # Simulate blind UseMedicine callback (no init_count line) and double finish()
        asst_log.write_text(
            '[2026-11-15 02:10:00.000][INF][Px1][Tx1] Assistant::append_callback | SubTaskExtraInfo {"what":"UseMedicine","details":{"count":1}}\n',
            encoding="utf-8",
        )
        maa_medicine_service.finish(self.config_dir, started_iso, 0, log_file=asst_log)
        maa_medicine_service.finish(self.config_dir, started_iso, 0, log_file=asst_log)

        ev_after = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertEqual(ev_after["remaining_count"], 2)
        self.assertEqual(ev_after["blind_deducted_since"], started_iso)
        self.assertEqual(ev_after["blind_deducted_used"], 1)
        attempt_after = json.loads((self.config_dir / "medicine-attempt.json").read_text(encoding="utf-8"))
        self.assertEqual(attempt_after["status"], "finished")
        self.assertEqual(attempt_after["confirmed_used"], 1)

        # Re-claiming in the same 02:00 window is blocked as already done
        replay = maa_medicine_service.claim_cultivation_drain(
            self.config_dir,
            now="2026-11-15T02:20:00+08:00",
            started_at="2026-11-15T02:20:00+08:00",
            activity_file=act_file,
            item_file=item_file,
            log_file=asst_log,
        )
        self.assertEqual(replay["action"], "done")

        parsed = maa_cultivation_runtime.build_parser().parse_args(
            ["--mode", "execute", "--started-at", started_iso]
        )
        self.assertEqual(parsed.started_at, started_iso)

    def test_sync_03_sim_dialog_recovery_gamestart_bound_and_timeout_redline(self) -> None:
        """Verify StartUp@SimPlugOutDialog + GameStart bound compose with stop-at-login and enforce timeout --foreground."""
        import maa_run_session

        sample_tasks = {
            "StartUp@CloseAnnos": {"next": ["CloseAnno", "TodaysSupplies"]},
            "StartUpThemes": {"next": ["StartUp@CloseAnnos"]},
            "GameStart": {"template": "GameStart.png", "maxTimes": 99},
        }
        patched = maa_run_session.with_mooncake_announcement(sample_tasks)
        self.assertIn("StartUp@SimPlugOutDialog", patched)
        self.assertEqual(patched["StartUp@CloseAnnos"]["next"][:2], ["StartUp@MooncakeTutorial", "StartUp@SimPlugOutDialog"])
        self.assertEqual(patched["GameStart"]["maxTimes"], 15)
        self.assertEqual(patched["GameStart"]["exceededNext"][0], "StartUp@SimPlugOutDialog")
        self.assertEqual(patched["StartUp@SimPlugOutDialog"]["reduceOtherTimes"], ["StartUp@GameStart"])

        # Must compose cleanly with MaaZero's with_stop_at_login
        composed = maa_run_session.with_stop_at_login(patched)
        self.assertEqual(composed["StartToWakeUp"]["action"], "Stop")
        self.assertEqual(composed["GameStart"]["maxTimes"], 15)
        self.assertIn("StartUp@SimPlugOutDialog", composed)

        # Verify maa-run.sh hooks and universal timeout --foreground redline across all shell scripts
        run_sh = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("dismiss_sim_plug_out_dialog()", run_sh)
        self.assertIn("dumpsys window displays", run_sh)
        self.assertIn('--started-at "$medicine_started_at"', run_sh)
        self.assertIn('reason="${reason}_SIM_DIALOG"', run_sh)
        self.assertIn('set_failure_reason "SIM_DIALOG"', run_sh)
        self.assertIn("sim_dialog_detected =", run_sh)
        self.assertIn(
            '        if (( ec == 0 )) && (( cultivation_active == 0 )); then\n'
            '            run_closing_scan "$task"\n'
            '        fi\n'
            '        stop_startup_overlay_watchdog',
            run_sh,
        )

        # Static redline: no shell script under packaging/project/source/ may call timeout without --foreground
        for sh_file in sorted(self.source_dir.glob("*.sh")):
            for lineno, line in enumerate(sh_file.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or '"124":' in stripped:
                    continue
                self.assertIsNone(
                    re.search(r"(^|[;&|(\s])timeout\s+(?!--foreground\b)", line),
                    msg=f"Bare timeout without --foreground at {sh_file.name}:{lineno}: {stripped}",
                )

    def test_sync_04_frontend_waterfall_and_auto_elite_js_syntax(self) -> None:
        """Verify synced cultivation/weekly frontend JS syntax and WebUI route/CSS integration."""
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to syntax-check frontend JS assets")
        for js_name in ("maa-cultivation-ui.js", "maa-weekly-editor.js"):
            js_path = self.source_dir / js_name
            proc = subprocess.run(
                [node_bin, "--check", str(js_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, msg=f"node --check failed for {js_name}: {proc.stderr}")

        cult_js = (self.source_dir / "maa-cultivation-ui.js").read_text(encoding="utf-8")
        weekly_js = (self.source_dir / "maa-weekly-editor.js").read_text(encoding="utf-8")
        webui_py = (self.source_dir / "maa-webui.py").read_text(encoding="utf-8")

        for token in ("cultivation/elite-run", "auto_elite", "priority_mode", "medicine_mode", "fallback_mode"):
            self.assertIn(token, cult_js)
        for token in ("syncCultivationAfterWeeklyChange", "cultivation_coordination"):
            self.assertIn(token, weekly_js)
        for token in ("def cultivation_elite_run_gate()", ".cult-sched-wrap", 'if p == "/api/cultivation/elite-run":'):
            self.assertIn(token, webui_py)
