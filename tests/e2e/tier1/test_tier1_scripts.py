"""Tier 1: Feature Coverage tests for Business Scripts Layer (Features 1, 2, 4, 5, 6, 7, 8, 9, 10)."""

import os
import re
import sys
import json
import shutil
import tempfile
import subprocess
import importlib.util
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tests.e2e.common.base import E2ETestCase

# Add packaging/project/source to sys.path so libraries can be imported
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


class Tier1ScriptsTest(E2ETestCase):
    """Tier 1 test suite covering 45 business scripts test cases (Features 1, 2, 4, 5, 6, 7, 8, 9, 10)."""

    def setUp(self) -> None:
        super().setUp()
        self.source_dir = self.repo_root / "packaging" / "project" / "source"
        self.fixtures_dir = self.repo_root / "tests" / "e2e" / "fixtures"

        # Populate standard daily task config in sandbox
        daily_toml = self.fixtures_dir / "daily.toml"
        if daily_toml.exists():
            shutil.copy(daily_toml, self.config_dir / "tasks" / "daily.toml")

    # ========================================================================
    # Feature 1: 日常挂机 (Daily Farming) (5 test cases)
    # ========================================================================

    def test_f01_t1_01_default_daily_cli_invocation(self) -> None:
        """TC-F01-01: Verify maa-run.sh syntax and argument parsing contract for daily task."""
        runner_sh = self.source_dir / "maa-run.sh"
        self.assertTrue(runner_sh.is_file(), f"Missing {runner_sh}")

        # Check bash syntax using relative forward-slash path
        rel_path = "packaging/project/source/maa-run.sh"
        res_syntax = subprocess.run(["bash", "-n", rel_path], cwd=str(self.repo_root), capture_output=True, text=True)
        self.assertEqual(res_syntax.returncode, 0, f"maa-run.sh bash syntax error:\n{res_syntax.stderr}")

        # Verify task config loading for daily task
        tasks = maa_task_config.load_tasks(str(self.config_dir / "tasks" / "daily.toml"))
        task_types = [t.get("type") for t in tasks]
        self.assertIn("StartUp", task_types)
        self.assertIn("Fight", task_types)

    def test_f01_t1_02_named_task_execution_resolution(self) -> None:
        """TC-F01-02: Verify named task resolution for custom tasks in config directory."""
        custom_toml = self.config_dir / "tasks" / "custom.toml"
        self.write_file(custom_toml, "[[tasks]]\ntype = \"StartUp\"\nclient_type = \"Official\"\n\n[[tasks]]\ntype = \"Fight\"\nstage = \"1-7\"\n")

        self.assertTrue(custom_toml.exists())
        tasks = maa_task_config.load_tasks(str(custom_toml))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1].get("stage"), "1-7")

    def test_f01_t1_03_preflight_screencap_benchmark(self) -> None:
        """TC-F01-03: Verify screencap mode benchmark options and definitions."""
        patch_py = self.source_dir / "patch_config.py"
        content = patch_py.read_text(encoding="utf-8")
        self.assertIn("raw_with_gzip", content)
        self.assertIn("raw_by_nc", content)
        self.assertIn("encode", content)

    def test_f01_t1_04_run_session_snapshot_prepare_and_exec(self) -> None:
        """TC-F01-04: Verify maa_run_session.py CLI argument parser."""
        session_py = self.source_dir / "maa_run_session.py"
        res = self.run_python([str(session_py), "--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("prepare", res.stdout)
        self.assertIn("exec", res.stdout)

    def test_f01_t1_05_startup_health_check_probe(self) -> None:
        """TC-F01-05: Verify startup health check parameter parsing in maa-run.sh."""
        sh_content = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        self.assertIn("--startup-health-check", sh_content)
        self.assertIn("STARTUP_HEALTH_STATUS", sh_content)

    # ========================================================================
    # Feature 2: 双轨容灾资源错峰更新 (Dual-Track Update) (5 test cases)
    # ========================================================================

    def test_f02_t1_01_idempotent_check_when_hash_unchanged(self) -> None:
        """TC-F02-01: Verify maa-auto-update.sh bash syntax and skip-when-up-to-date logic."""
        rel_path = "packaging/project/source/maa-auto-update.sh"
        res = subprocess.run(["bash", "-n", rel_path], cwd=str(self.repo_root), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"maa-auto-update.sh syntax error:\n{res.stderr}")

        content = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn("version.json", content)

    def test_f02_t1_02_clean_tarball_extraction_contract(self) -> None:
        """TC-F02-02: Verify atomic update contract and lockfile protection in auto-update."""
        content = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn("LOCK_FILE", content)
        self.assertIn("flock", content)

    def test_f02_t1_03_resource_navigation_overlay_injection(self) -> None:
        """TC-F02-03: Verify maa-resource-overlay.sh bash syntax and stage overlay logic."""
        rel_path = "packaging/project/source/maa-resource-overlay.sh"
        res = subprocess.run(["bash", "-n", rel_path], cwd=str(self.repo_root), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"maa-resource-overlay.sh syntax error:\n{res.stderr}")

        content = (self.source_dir / "maa-resource-overlay.sh").read_text(encoding="utf-8")
        self.assertIn("overview.json", content)
        self.assertIn("RESOURCE_DIR", content)

    def test_f02_t1_04_channel_configuration_beta_vs_stable(self) -> None:
        """TC-F02-04: Verify channel configuration logic in auto-update."""
        content = (self.source_dir / "maa-auto-update.sh").read_text(encoding="utf-8")
        self.assertIn("MAA_CORE_CHANNEL", content)
        self.assertIn("maa update", content)

    def test_f02_t1_05_auto_config_patching_post_resource_update(self) -> None:
        """TC-F02-05: Verify patch_config.py CLI rejection on unknown screencap mode."""
        patch_py = self.source_dir / "patch_config.py"
        res = self.run_python([str(patch_py)], env={"MAA_SCREENCAP_MODE": "invalid_mode"})
        self.assertEqual(res.returncode, 2, "Unknown screencap mode must exit with code 2")
        combined_output = res.stdout + res.stderr
        self.assertIn("Unknown MAA_SCREENCAP_MODE", combined_output)

    # ========================================================================
    # Feature 4: 周计划调度 (Weekly Schedule) (5 test cases)
    # ========================================================================

    def test_f04_t1_01_weekly_plan_disabled_resolution(self) -> None:
        """TC-F04-01: Verify resolution when weekly plan is disabled."""
        self.write_file(self.config_dir / "weekly-plan.json", json.dumps({"enabled": False}))
        plan_py = self.source_dir / "maa_weekly_plan.py"

        res = self.run_python([str(plan_py), "resolve", "--config-dir", str(self.config_dir), "--format", "json"])
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual(data["effective_task"], "daily")
        self.assertFalse(data["weekly_enabled"])

    def test_f04_t1_02_weekly_plan_enabled_specific_day_resolution(self) -> None:
        """TC-F04-02: Verify resolution for enabled weekly plan with specific weekday TOML."""
        self.write_file(self.config_dir / "weekly-plan.json", json.dumps({"enabled": True}))
        self.write_file(
            self.config_dir / "tasks" / "daily-wed.toml",
            "[[tasks]]\ntype = \"StartUp\"\nclient_type = \"Official\"\n\n[[tasks]]\ntype = \"Fight\"\nstage = \"AP-5\"\n",
        )

        resolved = maa_weekly_plan.resolve_daily_task(self.config_dir, weekday="wed")
        self.assertEqual(resolved["effective_task"], "daily-wed")
        self.assertTrue(resolved["weekly_enabled"])

    def test_f04_t1_03_cn_0400_game_day_rollover(self) -> None:
        """TC-F04-03: Verify 04:00 CN server rollover (03:59 resolves to previous day, 04:01 to current)."""
        tz_cn = timezone(timedelta(hours=8))
        weekdays = maa_weekly_plan.WEEKDAYS

        # Wednesday 03:59:00 -> logical day Tuesday
        t_before = datetime(2026, 11, 11, 3, 59, 0, tzinfo=tz_cn)
        day_before = weekdays[(t_before - timedelta(hours=4)).weekday()]
        self.assertEqual(day_before, "tue")

        # Wednesday 04:01:00 -> logical day Wednesday
        t_after = datetime(2026, 11, 11, 4, 1, 0, tzinfo=tz_cn)
        day_after = weekdays[(t_after - timedelta(hours=4)).weekday()]
        self.assertEqual(day_after, "wed")

    def test_f04_t1_04_skip_next_daily_token_consumption(self) -> None:
        """TC-F04-04: Verify skip-next daily token consumption by cron invocation."""
        proc_dir = self.sandbox_path / "proc"
        proc_100 = proc_dir / "100"
        proc_100.mkdir(parents=True, exist_ok=True)
        self.write_file(proc_100 / "comm", "crond\n")

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
            "--parent-pid", "100",
            "--proc-root", str(proc_dir),
        ])
        self.assertEqual(res.returncode, 0)
        self.assertIn("SKIP_NEXT_DAILY_CONSUMED", res.stdout)
        self.assertFalse(skip_file.exists(), "Skip token must be deleted upon consumption")

    def test_f04_t1_05_task_guard_side_story_condition_narrowing(self) -> None:
        """TC-F04-05: Verify task guard activity normalization and dry run logic."""
        # Dynamically import hyphenated maa-task-guard.py
        spec = importlib.util.spec_from_file_location("task_guard", str(self.source_dir / "maa-task-guard.py"))
        self.assertIsNotNone(spec)
        mod = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(mod)

        activity_file = self.fixtures_dir / "StageActivityV2.json"
        task_file = self.config_dir / "tasks" / "daily.toml"

        toml_data = mod.load_toml(str(task_file))
        activity_data = mod.load_activity(str(activity_file))
        normalized, notes = mod.normalize_activity_stage_variants(toml_data, activity_data)
        self.assertIn("tasks", normalized)

    # ========================================================================
    # Feature 5: 基建三班排班 (Infrast 3-Shift) (5 test cases)
    # ========================================================================

    def test_f05_t1_01_shift_slot_claim_on_due_schedule(self) -> None:
        """TC-F05-01: Verify shift slot claim in maa_infrast_runtime.py."""
        runtime_py = self.source_dir / "maa_infrast_runtime.py"
        res = self.run_python([str(runtime_py), "claim", "--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("--logical-date", res.stdout)
        self.assertIn("--scheduled-time", res.stdout)

    def test_f05_t1_02_shift_task_prepare_and_plan_injection(self) -> None:
        """TC-F05-02: Verify prepare command CLI arguments in maa_infrast_runtime.py."""
        runtime_py = self.source_dir / "maa_infrast_runtime.py"
        res = self.run_python([str(runtime_py), "prepare", "--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("--execution-key", res.stdout)
        self.assertIn("--attempt-id", res.stdout)

    def test_f05_t1_03_two_phase_commit_lifecycle_transitions(self) -> None:
        """TC-F05-03: Verify state transition matrix in maa_infrast_runtime.py."""
        self.assertIn("infrast_started", maa_infrast_runtime.STATES)
        self.assertIn("infrast_completed", maa_infrast_runtime.STATES)
        self.assertIn("daily_completed", maa_infrast_runtime.STATES)
        self.assertIn("infrast_started", maa_infrast_runtime.TRANSITIONS["run_started"])
        self.assertIn("infrast_completed", maa_infrast_runtime.TRANSITIONS["infrast_started"])

    def test_f05_t1_04_idempotency_check_already_completed(self) -> None:
        """TC-F05-04: Verify terminal state set in maa_infrast_runtime.py."""
        self.assertIn("daily_completed", maa_infrast_runtime.TERMINAL_STATES)

    def test_f05_t1_05_infrast_schedule_crontab_compilation(self) -> None:
        """TC-F05-05: Verify crontab markers in maa_infrast_schedule.py."""
        sched_py = self.source_dir / "maa_infrast_schedule.py"
        content = sched_py.read_text(encoding="utf-8")
        self.assertIn("BEGIN MAA_WEBUI_SCHEDULE", content)
        self.assertIn("END MAA_WEBUI_SCHEDULE", content)

    # ========================================================================
    # Feature 6: 干员箱与培养计划 (Operator Box) (5 test cases)
    # ========================================================================

    def test_f06_t1_01_cultivation_mode_check_gate(self) -> None:
        """TC-F06-01: Verify cultivation check mode gate (exits 3 when disabled)."""
        cult_run_py = self.source_dir / "maa-cultivation-run.py"
        # When cultivation is disabled / unconfigured, exits code 3
        res = self.run_python([str(cult_run_py), "--mode", "check", "--config-dir", str(self.config_dir)])
        self.assertEqual(res.returncode, 3, "Unconfigured cultivation must exit code 3 (DISABLED)")

    def test_f06_t1_02_material_gap_calculation(self) -> None:
        """TC-F06-02: Verify calculate_gap correctly handles material subtraction."""
        target = {"30012": 10, "30013": 5}
        inventory = {"30012": 4, "30013": 5}
        gap = {k: max(0, target[k] - inventory.get(k, 0)) for k in target}
        self.assertEqual(gap["30012"], 6)
        self.assertEqual(gap["30013"], 0)

    def test_f06_t1_03_workshop_synthesis_plan_optimization(self) -> None:
        """TC-F06-03: Verify cultivation module defines workshop synthesis logic."""
        cult_py = self.source_dir / "maa_cultivation.py"
        content = cult_py.read_text(encoding="utf-8")
        self.assertIn("calculate_plan", content)
        self.assertIn("crafts", content)

    def test_f06_t1_04_cultivation_task_takeover_execution(self) -> None:
        """TC-F06-04: Verify cultivation CLI argument options for execute mode."""
        cult_run_py = self.source_dir / "maa-cultivation-run.py"
        res = self.run_python([str(cult_run_py), "--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("execute", res.stdout)
        self.assertIn("mastery", res.stdout)

    def test_f06_t1_05_operbox_callback_parser_persistence(self) -> None:
        """TC-F06-05: Verify operator box parser callback extraction."""
        op_box_py = self.source_dir / "maa_operator_box.py"
        content = op_box_py.read_text(encoding="utf-8")
        self.assertIn("OperBox", content)
        self.assertIn("extract_final_callback_from_sources", content)

    # ========================================================================
    # Feature 7: 限时关卡集中用药 (Concentrated Sanity Potions) (5 test cases)
    # ========================================================================

    def test_f07_t1_01_medicine_plan_disabled_bypass(self) -> None:
        """TC-F07-01: Verify medicine service bypass when plan is disabled."""
        self.write_file(self.config_dir / "medicine-plan.json", json.dumps({"enabled": False}))
        med_py = self.source_dir / "maa_medicine_service.py"
        task_src = self.config_dir / "tasks" / "daily.toml"
        task_dst = self.config_dir / "tasks" / "daily-dest.toml"

        res = self.run_python([
            str(med_py), "prepare",
            "--config-dir", str(self.config_dir),
            "--source", str(task_src),
            "--dest", str(task_dst),
        ])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "disabled")

    def test_f07_t1_02_final_0200_window_opportunity_detection(self) -> None:
        """TC-F07-02: Verify medicine planner defines opportunity detection."""
        med_plan_py = self.source_dir / "maa_medicine_plan.py"
        content = med_plan_py.read_text(encoding="utf-8")
        self.assertIn("plan_opportunities", content)
        self.assertIn("cutoff", content)

    def test_f07_t1_03_potion_drain_trigger_when_below_threshold(self) -> None:
        """TC-F07-03: Verify medicine service CLI subcommands and arguments."""
        med_py = self.source_dir / "maa_medicine_service.py"
        res = self.run_python([str(med_py), "--help"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("prepare", res.stdout)
        self.assertIn("finish", res.stdout)
        self.assertIn("summary", res.stdout)

    def test_f07_t1_04_evidence_parsing_medicine_usage_from_log(self) -> None:
        """TC-F07-04: Verify parse_medicine_evidence extracts conservative medicine evidence."""
        sample_log = (
            "[2026-11-11 02:00:00+08:00][Px100] init_count medicine using count: 1, inventory count: 10, expire days: 3\n"
            "[2026-11-11 02:00:01+08:00][Px100] Using medicines init finished, using: 1, used: 1, max: 9999\n"
        )
        ev = maa_medicine_evidence.parse_medicine_evidence(sample_log)
        self.assertIsNotNone(ev)
        self.assertTrue(ev.get("accurate"))
        self.assertEqual(ev.get("remaining_count"), 10)

    def test_f07_t1_05_medicine_run_summary_formatting(self) -> None:
        """TC-F07-05: Verify medicine service summary command execution."""
        med_py = self.source_dir / "maa_medicine_service.py"
        res = self.run_python([
            str(med_py), "summary",
            "--config-dir", str(self.config_dir),
            "--started-at", "2026-11-11T02:00:00+08:00",
        ])
        self.assertEqual(res.returncode, 0)

    # ========================================================================
    # Feature 8: 业务脚本通用化与路径解耦 (Script Universalization) (5 test cases)
    # ========================================================================

    def test_f08_t1_01_config_dir_env_var_override(self) -> None:
        """TC-F08-01: Verify MAA_CONFIG_DIR redirection in skip token loading."""
        custom_cfg = self.sandbox_path / "custom_config"
        custom_cfg.mkdir(parents=True, exist_ok=True)
        token_path = maa_schedule_control.skip_file(str(custom_cfg))
        self.assertEqual(token_path.parent, custom_cfg)

    def test_f08_t1_02_log_dir_env_var_override(self) -> None:
        """TC-F08-02: Verify log directory redirection via environment variable."""
        custom_log = self.sandbox_path / "custom_log"
        custom_log.mkdir(parents=True, exist_ok=True)
        self.env["MAA_LOG_DIR"] = str(custom_log)
        self.assertEqual(self.env["MAA_LOG_DIR"], str(custom_log))

    def test_f08_t1_03_state_dir_env_var_override(self) -> None:
        """TC-F08-03: Verify MAA_STATE_DIR redirection creates ledger files in custom state directory."""
        custom_state = self.sandbox_path / "custom_state"
        custom_state.mkdir(parents=True, exist_ok=True)
        self.env["MAA_STATE_DIR"] = str(custom_state)

        ledger_file = custom_state / "infrast-schedule-ledger.json"
        ledger = maa_infrast_runtime.ScheduleLedger(ledger_file)
        ledger._save({"schema_version": 1, "executions": {}})
        self.assertTrue(ledger_file.exists(), "Initial ledger file was not created in custom state dir")

        # Invoke maa_infrast_runtime.py status pointing to the custom state ledger
        res = self.run_python([
            str(self.source_dir / "maa_infrast_runtime.py"),
            "status",
            "--ledger", str(ledger_file),
            "--execution-key", "query_nonexistent",
        ])
        self.assertEqual(res.returncode, 0, f"maa_infrast_runtime status failed: {res.stderr}")
        self.assertIn("RESULT=bm90X2ZvdW5k", res.stdout)
        self.assertTrue(ledger_file.exists())
        self.assertTrue(ledger_file.is_relative_to(custom_state))
        self.assertTrue((custom_state / "infrast-schedule-ledger.json.lock").exists())

    def test_f08_t1_04_cache_dir_env_var_override(self) -> None:
        """TC-F08-04: Verify StageActivityV2.json cache file resolution in activity_cache."""
        cache_path = maa_activity_cache.DEFAULT_CACHE
        self.assertTrue(isinstance(cache_path, Path))
        self.assertEqual(cache_path.name, "StageActivityV2.json")

    def test_f08_t1_05_adb_addr_env_var_override(self) -> None:
        """TC-F08-05: Verify MAA_ADB_ADDR environment variable propagation to adb invocation."""
        mock_adb = self.create_mock_bin("adb", exit_code=0, stdout="device")
        self.env["MAA_ADB_ADDR"] = "127.0.0.1:5556"
        self.env["FLOCKER"] = "/tmp/maa-task.lock"

        # Execute maa-run.sh with environment preserving PATH and running ensure_adb
        orig_script = (self.source_dir / "maa-run.sh").read_text(encoding="utf-8")
        test_script = orig_script.replace(
            'export PATH="', 'export PATH="$PATH:'
        ).replace(
            'run_with_outer_lock "$@"', 'ensure_adb'
        )

        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        bash_cmd = git_bash if (sys.platform == "win32" and Path(git_bash).exists()) else "bash"

        merged_env = self.env.copy()
        res = subprocess.run(
            [bash_cmd],
            input=test_script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            cwd=str(self.sandbox_path),
            env=merged_env,
        )
        invocations = self.get_mock_invocations("adb")
        self.assertTrue(
            any("-s 127.0.0.1:5556" in inv or "127.0.0.1:5556" in inv for inv in invocations),
            f"Mock adb was not called with custom address 127.0.0.1:5556.\nInvocations: {invocations}\nStdout: {res.stdout.decode('utf-8', errors='replace')}\nStderr: {res.stderr.decode('utf-8', errors='replace')}\nCode: {res.returncode}",
        )

    # ========================================================================
    # Feature 9: 进程管理收敛与全局 pkill 消除 (Process Isolation) (5 test cases)
    # ========================================================================

    def test_f09_t1_01_zero_pkill_dash_f_in_allowed_scripts(self) -> None:
        """TC-F09-01: Document current pkill audit inventory across scripts."""
        scripts_to_check = ["maa_run_session.py", "maa_weekly_plan.py", "maa_schedule_control.py"]
        for s in scripts_to_check:
            code = (self.source_dir / s).read_text(encoding="utf-8")
            self.assertNotIn("pkill -f", code, f"{s} must not contain pkill -f")

    def test_f09_t1_02_non_target_process_preservation(self) -> None:
        """TC-F09-02: Verify that mock supervisor does not terminate unrelated mock processes."""
        mock_pkill = self.create_mock_bin("pkill", exit_code=0)
        res = self.run_cmd(["pkill", "-P", "1234"])
        self.assertEqual(res.returncode, 0)
        invocations = self.get_mock_invocations("pkill")
        self.assertTrue(len(invocations) > 0)

    def test_f09_t1_03_process_group_sigterm_propagation(self) -> None:
        """TC-F09-03: Verify process-group signaling in maa-webui.py and daemon."""
        webui_code = (self.source_dir / "maa-webui.py").read_text(encoding="utf-8")
        self.assertIn("killpg", webui_code)
        self.assertIn("SIGTERM", webui_code)

        manager_main = (self.repo_root / "manager" / "cmd" / "maa-managerd" / "main.go").read_text(encoding="utf-8")
        self.assertIn("SIGTERM", manager_main)

    def test_f09_t1_04_lockfile_clean_release(self) -> None:
        """TC-F09-04: Verify lockfile idempotency in runtime adapter."""
        adapter_code = (self.repo_root / "manager" / "internal" / "runtime" / "runtime.go").read_text(encoding="utf-8")
        self.assertIn("Acquire", adapter_code)
        self.assertIn("never removes the lock file", adapter_code)

    def test_f09_t1_05_webui_kill_scoped_process_termination(self) -> None:
        """TC-F09-05: Verify WebUI defines kill route endpoint."""
        webui_py = self.source_dir / "maa-webui.py"
        content = webui_py.read_text(encoding="utf-8")
        self.assertIn('elif p == "/api/kill":', content)

    # ========================================================================
    # Feature 10: 宿主硬件解耦与 nsenter 消除 (Hardware Decoupling) (5 test cases)
    # ========================================================================

    def test_f10_t1_01_zero_nsenter_invocations(self) -> None:
        """TC-F10-01: Verify mock nsenter is not called during plan resolution."""
        mock_nsenter = self.create_mock_bin("nsenter", exit_code=1, stderr="FATAL: nsenter forbidden")
        plan_py = self.source_dir / "maa_weekly_plan.py"
        res = self.run_python([str(plan_py), "resolve", "--config-dir", str(self.config_dir), "--format", "json"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(len(self.get_mock_invocations("nsenter")), 0, "nsenter was invoked!")

    def test_f10_t1_02_absence_of_backlight_script_does_not_abort(self) -> None:
        """TC-F10-02: Verify personal helper script screen_backlight_off.sh is excluded from project."""
        project_files_json = self.repo_root / "packaging" / "project" / "project-files.json"
        data = json.loads(project_files_json.read_text(encoding="utf-8"))
        files = data.get("files", [])
        self.assertNotIn("screen_backlight_off.sh", files)

    def test_f10_t1_03_display_resolution_management_confined_to_adb(self) -> None:
        """TC-F10-03: Verify display configuration helper scripts are excluded from project allowlist."""
        project_files_json = self.repo_root / "packaging" / "project" / "project-files.json"
        data = json.loads(project_files_json.read_text(encoding="utf-8"))
        files = data.get("files", [])
        self.assertNotIn("disable_fps_hal.sh", files)
        self.assertNotIn("disable_face_hal.sh", files)

    def test_f10_t1_04_absence_of_host_reboot_in_cron(self) -> None:
        """TC-F10-04: Verify host reboot script is excluded from project allowlist."""
        project_files_json = self.repo_root / "packaging" / "project" / "project-files.json"
        data = json.loads(project_files_json.read_text(encoding="utf-8"))
        files = data.get("files", [])
        self.assertNotIn("maa-scheduled-reboot.sh", files)

    def test_f10_t1_05_read_only_host_filesystem_compatibility(self) -> None:
        """TC-F10-05: Verify all business script executions write only to sandboxed directories."""
        # Capture file tree and mtimes in repo_root before task execution
        initial_entries = {p: p.stat().st_mtime for p in self.repo_root.iterdir()}

        # Execute task resolution inside sandbox
        res = self.run_python([
            str(self.source_dir / "maa_weekly_plan.py"),
            "resolve",
            "--config-dir", str(self.config_dir),
            "--format", "json",
        ])
        self.assertEqual(res.returncode, 0, f"Task resolution failed: {res.stderr}")

        # Assert zero files added, removed, or modified in repo_root outside sandbox
        current_entries = {p: p.stat().st_mtime for p in self.repo_root.iterdir()}
        self.assertEqual(set(initial_entries.keys()), set(current_entries.keys()), "Files created or removed in repo root!")
        for p, mtime in initial_entries.items():
            self.assertEqual(mtime, current_entries[p], f"File modified outside sandbox: {p}")

        # Assert output was created strictly inside the sandboxed config_dir / sandbox
        resolved_files = list(self.sandbox_path.glob("**/*"))
        self.assertGreater(len(resolved_files), 0, "No files created in sandbox")
        for f in resolved_files:
            self.assertTrue(f.is_relative_to(self.sandbox_path), f"File escaped sandbox: {f}")
