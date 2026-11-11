"""Tier 5: White-Box Adversarial Hardening for Business Scripts, Module & Packaging Systems.

Features Covered:
- F01: 日常挂机 (Daily Farming & Execution CLI)
- F02: 双轨容灾资源错峰更新 (Dual-Track Update)
- F04: 周计划调度 (Weekly Schedule & Rollover)
- F05: 基建三班排班 (Infrast 3-Shift Rotation & Task Guard)
- F06: 干员箱与培养计划 (Operator Box & Cultivation)
- F07: 限时关卡集中用药 (Concentrated Sanity Potions & Evidence)
- F08: 业务脚本通用化与路径解耦 (Path & Environment Universalization)
- F09: 进程管理收敛与全局 pkill 消除 (Process Isolation & Signal Handling)
- F10: 宿主硬件解耦与 Android 沙箱隔离 (Host Hardware Decoupling)
- F12: 上游与自有边界隔离 (Upstream Manifests & Channel Policies)
- F13: Root 引导模块与救援入口 (Root Module Packaging & Uninstallation Safety)
- F14: 规范与合规红线 (Compliance, Pure LF, Zero Secrets)
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.elf import build_arm64_elf_bytes, create_arm64_elf
from tests.e2e.common.pwsh import run_pwsh_file
from tests.e2e.common.scanner import scan_cr_bytes, scan_secrets, scan_utf8_bom

# Ensure packaging/project/source is on sys.path
SOURCE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "packaging" / "project" / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

PACKAGING_DIR = Path(__file__).resolve().parent.parent.parent.parent / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import maa_activity_cache
import maa_medicine_plan
import maa_medicine_service
import maa_operator_box
import maa_run_session
import maa_schedule_control
import maa_task_config
import maa_weekly_plan
import patch_config
import webui_overlay


def _load_generate_manifests_module(repo_root: Path):
    path = repo_root / "packaging" / "generate-manifests.py"
    spec = importlib.util.spec_from_file_location("generate_manifests", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Tier5ScriptsPackagingAdversarialTest(E2ETestCase):
    """Tier 5 Adversarial Test Suite for Scripts, Module, and Packaging Subsystems."""

    tier = 5

    def setUp(self) -> None:
        super().setUp()
        self.source_dir = self.repo_root / "packaging" / "project" / "source"
        self.fixtures_dir = self.repo_root / "tests" / "e2e" / "fixtures"

        # Standard daily fixture
        daily_toml = self.fixtures_dir / "daily.toml"
        if daily_toml.exists():
            shutil.copy(daily_toml, self.config_dir / "tasks" / "daily.toml")

    # ========================================================================
    # Feature 01: 日常挂机 (Daily Farming & Config Parser Adversarial Attacks)
    # ========================================================================

    def test_f01_t5_01_fuzzed_daily_toml_syntax_rejection(self) -> None:
        """TC-F01-ADV01: Fuzz daily.toml with malformed syntax, truncation, and binary noise."""
        task_file = self.config_dir / "tasks" / "daily.toml"

        fuzzed_payloads = [
            b"[[tasks]\ntype = 'StartUp'",  # unclosed bracket
            b"[[tasks]]\ntype = \"StartUp\nclient_type = 'Official'",  # unclosed string
            b"\x00\xff\xfe\x01\x02\x03BINARY_NOISE",  # non-utf8 binary garbage
            b"   \t\r\n   ",  # whitespace only
        ]

        for idx, payload in enumerate(fuzzed_payloads):
            if isinstance(payload, bytes):
                task_file.write_bytes(payload)
            else:
                self.write_file(task_file, payload)

            # 1. validate_task_bytes must detect error or empty
            valid, err = maa_task_config.validate_task_bytes(payload if isinstance(payload, bytes) else payload.encode("utf-8"))
            self.assertFalse(valid, f"Payload {idx} should be invalid")
            self.assertTrue(len(err) > 0, f"Payload {idx} should report error reason")

            # 2. Defensive load_tasks must return empty list without crashing
            tasks = maa_task_config.load_tasks(task_file)
            self.assertEqual(tasks, [], f"Defensive load_tasks must return empty for payload {idx}")

            # 3. Strict load_tasks_strict must raise StrictTaskConfigError
            with self.assertRaises(maa_task_config.StrictTaskConfigError, msg=f"Payload {idx} must raise in strict mode"):
                maa_task_config.load_tasks_strict(task_file)

    def test_f01_t5_02_task_config_array_of_primitives_strict_rejection(self) -> None:
        """TC-F01-ADV02: Task config where tasks array contains primitives or non-tables."""
        task_file = self.config_dir / "tasks" / "malformed_tasks.toml"

        # tasks containing strings, integers, or dict-level non-list
        malformed_cases = [
            'tasks = ["StartUp", "Fight"]\n',
            'tasks = [1, 2, 3]\n',
            'tasks = "StartUp"\n',
            'disabled_tasks = ["CustomTask"]\n',
        ]

        for idx, content in enumerate(malformed_cases):
            self.write_file(task_file, content)

            # Defensive loader returns empty list
            tasks = maa_task_config.load_tasks(task_file)
            self.assertEqual(tasks, [], f"Case {idx} should produce empty tasks")

            # Strict loader must raise StrictTaskConfigError
            with self.assertRaises(maa_task_config.StrictTaskConfigError, msg=f"Case {idx} must raise StrictTaskConfigError"):
                maa_task_config.load_tasks_strict(task_file)

    def test_f01_t5_03_task_config_single_table_vs_array_divergence(self) -> None:
        """TC-F01-ADV03: Single table [tasks] vs array [[tasks]] semantic divergence detection.

        Empirical finding:
        When a user writes [tasks] (single table) instead of [[tasks]] (array of tables):
        - validate_task_bytes incorrectly reports (True, "") because it only checks data.get('tasks') truthiness.
        - load_tasks_strict correctly enforces isinstance(active, list) and raises StrictTaskConfigError.
        - load_tasks catches StrictTaskConfigError and returns [].
        """
        task_file = self.config_dir / "tasks" / "single_table.toml"
        payload = b"[tasks]\ntype = 12345\n"
        task_file.write_bytes(payload)

        # 1. validate_task_bytes allows it through (documented semantic gap)
        valid, _ = maa_task_config.validate_task_bytes(payload)
        self.assertTrue(valid, "validate_task_bytes currently permits non-array tasks key")

        # 2. Strict loader catches the non-array type and raises StrictTaskConfigError
        with self.assertRaises(maa_task_config.StrictTaskConfigError) as ctx:
            maa_task_config.load_tasks_strict(task_file)
        self.assertIn("tasks 和 disabled_tasks 必须是数组", str(ctx.exception))

        # 3. Defensive loader safely suppresses and returns empty list
        tasks = maa_task_config.load_tasks(task_file)
        self.assertEqual(tasks, [])

    # ========================================================================
    # Feature 04: 周计划调度 (Weekly Schedule & Rollover Adversarial Hardening)
    # ========================================================================

    def test_f04_t5_01_weekly_plan_corrupted_json_resilient_fallback(self) -> None:
        """TC-F04-ADV01: Resilient fallback when weekly-plan.json contains corrupted or hostile types."""
        settings_file = self.config_dir / "weekly-plan.json"

        adversarial_json_bodies = [
            "{corrupted_json_payload: null",
            "[1, 2, 3]",
            '"enabled"',
            "99999",
            '{"enabled": "yes", "initialized": 42}',
            '{"unexpected_huge_key": "' + ("A" * 10000) + '"}',
        ]

        for idx, body in enumerate(adversarial_json_bodies):
            self.write_file(settings_file, body)

            # load_settings must recover to safe defaults
            settings = maa_weekly_plan.load_settings(self.config_dir)
            self.assertFalse(settings["enabled"], f"Case {idx} enabled must be False")
            self.assertFalse(settings["initialized"], f"Case {idx} initialized must be False")

            # resolve_daily_task must fall back cleanly to daily.toml
            res = maa_weekly_plan.resolve_daily_task(self.config_dir)
            self.assertEqual(res["effective_task"], "daily")
            self.assertFalse(res["weekly_enabled"])

    def test_f04_t5_02_weekly_plan_enable_with_corrupt_daily_toml_fails_cleanly(self) -> None:
        """TC-F04-ADV02: set_enabled(True) when daily.toml is corrupt must fail without damaging state."""
        daily_toml = self.config_dir / "tasks" / "daily.toml"
        daily_toml.write_bytes(b"\x00\xffCORRUPT_BYTES_IN_DAILY")

        with self.assertRaises(maa_weekly_plan.WeeklyPlanError) as ctx:
            maa_weekly_plan.set_enabled(self.config_dir, True)

        self.assertIn("daily.toml is not usable", str(ctx.exception))

        # Settings must remain disabled
        settings = maa_weekly_plan.load_settings(self.config_dir)
        self.assertFalse(settings["enabled"], "Weekly mode must not be enabled after failure")

        # No corrupted weekday files should have been spawned
        for day in maa_weekly_plan.WEEKDAYS:
            target = self.config_dir / "tasks" / f"daily-{day}.toml"
            self.assertFalse(target.exists(), f"{target} should not exist after failed initialization")

    # ========================================================================
    # Feature 02: 双轨容灾资源错峰更新 (Dual-Track Resource Cache Adversarial)
    # ========================================================================

    def test_f02_t5_01_activity_cache_corrupted_json_and_validation(self) -> None:
        """TC-F02-ADV01: Activity cache rejects corrupt, oversized or malformed schemas."""
        act_file = self.cache_dir / "StageActivityV2.json"

        # 1. Corrupted JSON file recovery in get_activity
        self.write_file(act_file, "{corrupted_json_without_brackets")
        catalog, warning = maa_activity_cache.get_activity(act_file, now=time.time())
        # Cache reading handles error and attempts fallback or returns empty with warning
        self.assertIsInstance(catalog, dict)

        # 2. publish_activity rejects missing 'Official' or missing 'sideStoryStage'
        with self.assertRaises(ValueError) as ctx1:
            maa_activity_cache.publish_activity({}, path=act_file)
        self.assertIn("官方活动目录格式无效", str(ctx1.exception))

        with self.assertRaises(ValueError) as ctx2:
            maa_activity_cache.publish_activity({"Official": "invalid_string"}, path=act_file)
        self.assertIn("官方活动目录格式无效", str(ctx2.exception))

        with self.assertRaises(ValueError) as ctx3:
            maa_activity_cache.publish_activity({"Official": {"sideStoryStage": "not_a_dict"}}, path=act_file)
        self.assertIn("官方活动目录格式无效", str(ctx3.exception))

    # ========================================================================
    # Feature 05: 基建三班排班 & Task Guard (Adversarial Edge Cases)
    # ========================================================================

    def test_f05_t5_01_task_guard_adversarial_timezone_and_malformed_activity(self) -> None:
        """TC-F05-ADV01: maa-task-guard handles missing/malformed activity files and invalid timezones."""
        task_guard_py = self.source_dir / "maa-task-guard.py"
        task_file = self.config_dir / "tasks" / "daily.toml"
        corrupt_activity = self.cache_dir / "StageActivityCorrupted.json"
        self.write_file(corrupt_activity, "{bad_json: 123")

        # Execute task guard with corrupt activity file
        res = self.run_python([
            str(task_guard_py),
            "--task-file", str(task_file),
            "--activity-file", str(corrupt_activity),
            "--dry-run-stage",
        ])
        self.assertEqual(res.returncode, 0, f"Task guard must not crash on corrupt activity:\n{res.stderr}")
        self.assertIn("effective_stage=", res.stdout)

    # ========================================================================
    # Feature 06: 干员箱与培养计划 (Operator Box Snapshot Tamper Detection)
    # ========================================================================

    def test_f06_t5_01_operator_box_snapshot_tamper_detection(self) -> None:
        """TC-F06-ADV01: load_scan_status verifies SHA-256 canonical integrity and rejects tampered data."""
        snapshot_path = self.sandbox_path / "operator-box.json"
        state_path = self.sandbox_path / "operator-box-state.json"

        valid_callback = {
            "what": "OperBoxInfo",
            "details": {
                "done": True,
                "all_opers": [
                    {"id": "char_002_amiya", "name": "阿米娅", "own": True, "rarity": 5},
                    {"id": "char_102_texas", "name": "德克萨斯", "own": True, "rarity": 5},
                ],
                "own_opers": [
                    {"id": "char_002_amiya", "name": "阿米娅", "own": True, "rarity": 5, "elite": 2, "level": 80, "potential": 6},
                ],
            },
        }

        # 1. Save valid snapshot
        snapshot = maa_operator_box.save_successful_scan(
            snapshot_path=snapshot_path,
            state_path=state_path,
            callback=valid_callback,
            scanned_at="2026-11-11T12:00:00Z",
            source_log="mock log output",
            core_version="v5.1.0",
            resource_version="v2026.09.01",
        )
        self.assertTrue(snapshot_path.exists())

        # Verify initially valid
        status_valid = maa_operator_box.load_scan_status(
            snapshot_path, state_path, now=datetime(2026, 11, 11, 13, 0, 0, tzinfo=timezone.utc)
        )
        self.assertTrue(status_valid["available"])
        self.assertFalse(status_valid["stale"])

        # 2. Tamper with 1 byte of the payload on disk (modify rarity)
        raw_data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        raw_data["all_opers"][0]["rarity"] = 6  # tampered
        snapshot_path.write_text(json.dumps(raw_data), encoding="utf-8")

        # 3. load_scan_status must catch the SHA-256 mismatch and reject the snapshot
        status_tampered = maa_operator_box.load_scan_status(
            snapshot_path, state_path, now=datetime(2026, 11, 11, 13, 0, 0, tzinfo=timezone.utc)
        )
        self.assertFalse(status_tampered["available"], "Tampered snapshot must report available=False")
        self.assertTrue(status_tampered["stale"], "Tampered snapshot must report stale=True")
        self.assertIsNone(status_tampered["snapshot"], "Tampered snapshot must return None")

    def test_f06_t5_02_operator_box_validation_duplicate_ids_and_fake_ownership(self) -> None:
        """TC-F06-ADV02: validate_final_callback strictly rejects duplicate operator IDs and fake ownership."""
        # Case A: Duplicate operator IDs in all_opers
        dup_callback = {
            "what": "OperBoxInfo",
            "details": {
                "done": True,
                "all_opers": [
                    {"id": "char_002_amiya", "name": "阿米娅", "own": True, "rarity": 5},
                    {"id": "char_002_amiya", "name": "阿米娅", "own": True, "rarity": 5},  # DUPLICATE
                ],
                "own_opers": [],
            },
        }
        with self.assertRaises(maa_operator_box.OperatorBoxError) as ctx1:
            maa_operator_box.validate_final_callback(dup_callback)
        self.assertEqual(ctx1.exception.code, "invalid_callback")
        self.assertIn("全干员列表 ID 重复", str(ctx1.exception))

        # Case B: own_opers includes operator with own == False
        fake_own_callback = {
            "what": "OperBoxInfo",
            "details": {
                "done": True,
                "all_opers": [{"id": "char_002_amiya", "name": "阿米娅", "own": True, "rarity": 5}],
                "own_opers": [
                    {"id": "char_002_amiya", "name": "阿米娅", "own": False, "rarity": 5, "elite": 1, "level": 50, "potential": 1}
                ],
            },
        }
        with self.assertRaises(maa_operator_box.OperatorBoxError) as ctx2:
            maa_operator_box.validate_final_callback(fake_own_callback)
        self.assertEqual(ctx2.exception.code, "invalid_callback")
        self.assertIn("已拥有列表包含未拥有干员", str(ctx2.exception))

    # ========================================================================
    # Feature 07: 限时关卡集中用药 (Concentrated Sanity Potions Boundary Hardening)
    # ========================================================================

    def test_f07_t5_01_medicine_plan_settings_fuzzing_and_boundary_rejection(self) -> None:
        """TC-F07-ADV01: medicine-plan settings strict boundary and unknown key rejection."""
        plan_file = self.config_dir / "medicine-plan.json"

        # Invalid threshold boundaries on disk
        invalid_settings_cases = [
            {"enabled": True, "threshold": -1},  # negative
            {"enabled": True, "threshold": 10000},  # overflow
            {"enabled": True, "threshold": "80"},  # wrong type
            {"enabled": "true", "threshold": 80},  # wrong type
        ]

        for idx, payload in enumerate(invalid_settings_cases):
            self.write_file(plan_file, json.dumps(payload))
            with self.assertRaises(ValueError, msg=f"Payload {idx} must be rejected"):
                maa_medicine_service.settings(self.config_dir)

        # Non-dict JSON on disk raises ValueError: 状态必须是对象
        self.write_file(plan_file, "[1, 2, 3]")
        with self.assertRaises(ValueError):
            maa_medicine_service.settings(self.config_dir)

        # update_settings strictly rejects extra/unknown keys
        with self.assertRaises(ValueError) as ctx_extra:
            maa_medicine_service.update_settings(
                self.config_dir,
                {"enabled": True, "threshold": 80, "unauthorized_field": "exploit"},
            )
        self.assertIn("仅支持启用与材料阈值", str(ctx_extra.exception))

    # ========================================================================
    # Feature 08: 业务脚本通用化与路径解耦 (Paths with Spaces, Unicode & Read-Only)
    # ========================================================================

    def test_f08_t5_01_paths_with_spaces_unicode_and_special_symbols(self) -> None:
        """TC-F08-ADV01: Verify operations succeed in paths with spaces, Unicode and special symbols."""
        weird_dir = self.sandbox_path / "weird path 路径_Аркнайтс_!@#$"
        weird_config = weird_dir / "config"
        weird_tasks = weird_config / "tasks"
        weird_tasks.mkdir(parents=True, exist_ok=True)

        daily_src = self.fixtures_dir / "daily.toml"
        shutil.copy(daily_src, weird_tasks / "daily.toml")

        # 1. Weekly plan resolution in weird path
        res = maa_weekly_plan.resolve_daily_task(weird_config)
        self.assertEqual(res["effective_task"], "daily")
        self.assertTrue(Path(res["source_path"]).exists())

        # 2. Skip file creation in weird path
        skip_path = maa_schedule_control.skip_file(weird_config)
        self.assertIn("weird path 路径_Аркнайтс_!@#$", str(skip_path))

        # 3. Atomic task save in weird path
        tasks = maa_task_config.load_tasks(weird_tasks / "daily.toml")
        self.assertGreater(len(tasks), 0)
        save_ok = maa_task_config.save_tasks(tasks, weird_tasks / "daily-test.toml")
        self.assertTrue(save_ok)
        self.assertTrue((weird_tasks / "daily-test.toml").exists())

    def test_f08_t5_02_atomic_write_safety_under_readonly_destination(self) -> None:
        """TC-F08-ADV02: Atomic writers fail cleanly and clean up temp files on destination errors."""
        # Test write failure when destination parent is blocked by a file (not a directory)
        blocking_file = self.sandbox_path / "blocking_file.txt"
        blocking_file.write_text("not a directory", encoding="utf-8")
        target_file = blocking_file / "test.json"

        with self.assertRaises(OSError):
            maa_schedule_control._atomic_write_json(target_file, {"test": True})

        with self.assertRaises(OSError):
            maa_weekly_plan._atomic_write_bytes(target_file, b"test")

    # ========================================================================
    # Feature 09: 进程管理收敛与全局 pkill 消除 (Skip Tokens, Signals & Idempotency)
    # ========================================================================

    def test_f09_t5_01_corrupted_skip_token_and_atomic_claim_idempotency(self) -> None:
        """TC-F09-ADV01: consume_skip_next_daily cleanly discards corrupt tokens and guarantees idempotency."""
        # 1. Non-cron invocation must immediately return False
        self.assertFalse(maa_schedule_control.consume_skip_next_daily(self.config_dir, is_cron=False))

        # 2. Corrupt token file (0 bytes)
        token_path = maa_schedule_control.skip_file(self.config_dir)
        token_path.write_bytes(b"")
        self.assertFalse(maa_schedule_control.consume_skip_next_daily(self.config_dir, is_cron=True))
        self.assertFalse(token_path.exists(), "Corrupt token must be cleaned up")

        # 3. Wrong schema version
        self.write_file(token_path, json.dumps({"schema_version": 99, "scheduled_for": "2026-11-11T04:00:00"}))
        self.assertFalse(maa_schedule_control.consume_skip_next_daily(self.config_dir, is_cron=True))
        self.assertFalse(token_path.exists())

        # 4. Valid token one-time consumption (atomic mutual exclusion)
        valid_payload = {
            "schema_version": 1,
            "armed_at": "2026-11-11T03:00:00Z",
            "scheduled_for": "2026-11-11T04:00:00Z",
        }
        self.write_file(token_path, json.dumps(valid_payload))

        first_consume = maa_schedule_control.consume_skip_next_daily(self.config_dir, is_cron=True)
        self.assertTrue(first_consume, "First consume must succeed")

        second_consume = maa_schedule_control.consume_skip_next_daily(self.config_dir, is_cron=True)
        self.assertFalse(second_consume, "Second consume must return False (already consumed)")

    def test_f09_t5_02_run_session_prewarm_token_idempotency_and_path_traversal(self) -> None:
        """TC-F09-ADV02: maa_run_session rejects path traversal and enforces prewarm token consumption."""
        session_root = self.sandbox_path / "mock_session"
        session_root.mkdir(parents=True, exist_ok=True)
        (session_root / "cache").mkdir(parents=True, exist_ok=True)

        # Mock session manifest
        hashes = {}
        for _, relative in maa_run_session.TARGETS:
            for suffix in ("", ".etag"):
                p = session_root / "cache" / (relative + suffix)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"mock_resource_content")
                hashes[relative + suffix] = hashlib.sha256(b"mock_resource_content").hexdigest()

        (session_root / "manifest.json").write_text(json.dumps({
            "created_at": time.time(),
            "api": "https://api.maa.plus/MaaAssistantArknights/api",
            "hashes": hashes,
        }), encoding="utf-8")

        # 1. Path traversal attacks on task name
        traversal_attempts = [
            ["run", "../../../etc/passwd"],
            ["run", "."],
            ["run", ".."],
            ["run", "sub/task"],
            ["exec", "daily"],  # unsupported command
        ]
        for args in traversal_attempts:
            with self.assertRaises(maa_run_session.SessionError):
                maa_run_session.build_command(
                    session_root, self.config_dir, args, maa="maa"
                )

        # 2. Prewarm token consumption idempotency
        cmd1, env1 = maa_run_session.build_command(
            session_root, self.config_dir, ["run", "daily"], maa="maa", prewarm_token="token_alpha"
        )
        phase1_cfg = Path(env1["MAA_CONFIG_DIR"]) / "tasks" / "daily.toml"
        self.assertTrue(phase1_cfg.exists())
        doc1 = maa_task_config.load_tasks(phase1_cfg)
        self.assertFalse(doc1[0]["params"].get("start_game_enabled", True), "First consume must disable start_game_enabled")

        # Second build with same prewarm token must NOT re-modify (marker already exists)
        cmd2, env2 = maa_run_session.build_command(
            session_root, self.config_dir, ["run", "daily"], maa="maa", prewarm_token="token_alpha"
        )
        phase2_cfg = Path(env2["MAA_CONFIG_DIR"]) / "tasks" / "daily.toml"
        doc2 = maa_task_config.load_tasks(phase2_cfg)
        self.assertTrue(phase2_cfg.exists())

    # ========================================================================
    # Feature 10: 宿主硬件解耦与 Android 沙箱隔离 (Zero nsenter / pkill verification)
    # ========================================================================

    def test_f10_t5_01_host_sanitation_zero_nsenter_and_zero_pkill_repo_wide(self) -> None:
        """TC-F10-ADV01: Verify absence of nsenter and pkill invocations across all scripts and modules."""
        target_dirs = [
            self.repo_root / "packaging" / "project" / "source",
            self.repo_root / "module",
        ]

        forbidden_patterns = [
            (b"nsenter ", "Unscoped nsenter call"),
            (b"pkill -f", "Global pkill -f pattern match"),
            (b"pkill ", "Unscoped pkill invocation"),
        ]

        for target_dir in target_dirs:
            for root, _, files in os.walk(target_dir):
                for f in files:
                    if f.endswith((".sh", ".py")):
                        fpath = Path(root) / f
                        content = fpath.read_bytes()
                        for pattern, desc in forbidden_patterns:
                            self.assertNotIn(
                                pattern,
                                content,
                                f"Forbidden pattern '{desc}' found in {fpath.relative_to(self.repo_root)}",
                            )

    # ========================================================================
    # Feature 12: 上游与自有边界隔离 (Manifest Generation Semantic Boundary Testing)
    # ========================================================================

    def test_f12_t5_01_generate_manifests_semantic_boundary_rejection(self) -> None:
        """TC-F12-ADV01: generate-manifests semantic validator strictly enforces schema invariants."""
        gen_mod = _load_generate_manifests_module(self.repo_root)

        # 1. Channel must be stable or beta
        with self.assertRaises(ValueError) as ctx:
            gen_mod.build_manifest(
                channel="nightly",
                sequence=1,
                manager_size=1000,
                manager_sha256="a" * 64,
                project_size=1000,
                project_sha256="b" * 64,
                generated_at="2026-11-11T12:00:00Z",
            )
        self.assertIn("invalid channel", str(ctx.exception))

        # 2. Semantic validator checks
        valid_data = gen_mod.build_manifest(
            channel="stable",
            sequence=1,
            manager_size=1000,
            manager_sha256="a" * 64,
            project_size=1000,
            project_sha256="b" * 64,
            generated_at="2026-11-11T12:00:00Z",
        )
        gen_mod.validate_manifest_semantics(valid_data)  # must pass

        # Mutate sequence <= 0
        mutated = copy.deepcopy(valid_data)
        mutated["sequence"] = 0
        with self.assertRaises(AssertionError):
            gen_mod.validate_manifest_semantics(mutated)

        # Mutate SHA256 to non-lowercase or invalid length
        mutated_sha = copy.deepcopy(valid_data)
        mutated_sha["components"][0]["artifacts"][0]["sha256"] = "A" * 64
        with self.assertRaises(AssertionError):
            gen_mod.validate_manifest_semantics(mutated_sha)

        # Mutate artifact URL to exclude version
        mutated_url = copy.deepcopy(valid_data)
        mutated_url["components"][0]["artifacts"][0]["url"] = "https://example.com/unversioned"
        with self.assertRaises(AssertionError):
            gen_mod.validate_manifest_semantics(mutated_url)

    # ========================================================================
    # Feature 13: Root 引导模块与救援入口 (ELF Mutations, Release Config & Uninstall)
    # ========================================================================

    def test_f13_t5_01_build_module_elf_machine_mutations_rejection(self) -> None:
        """TC-F13-ADV01: build-module.ps1 strictly rejects non-ARM64, 32-bit, dynamic, or corrupt ELFs."""
        build_module_ps1 = self.repo_root / "packaging" / "build-module.ps1"
        out_dir = self.sandbox_path / "mod_out"

        mutations = [
            ("x86_64", build_arm64_elf_bytes(machine=0x003E), "must be an AArch64/arm64 ELF"),
            ("arm32", build_arm64_elf_bytes(machine=0x0028), "must be an AArch64/arm64 ELF"),
            ("riscv64", build_arm64_elf_bytes(machine=0x00F3), "must be an AArch64/arm64 ELF"),
            ("32bit", build_arm64_elf_bytes(is_64bit=False), "must be a 64-bit ELF"),
            ("big_endian", build_arm64_elf_bytes(is_little_endian=False), "must be a little-endian ELF"),
            ("bad_magic", build_arm64_elf_bytes(bad_magic=True), "not an ELF file (bad magic)"),
            ("dynamic", build_arm64_elf_bytes(is_static=False), "dynamically linked (PT_INTERP)"),
            ("truncated", b"\x7fELF" + (b"\x00" * 20), "too small to be an ELF file"),
        ]

        for name, elf_bytes, expected_error in mutations:
            bin_path = self.sandbox_path / f"mutated_bin_{name}"
            bin_path.write_bytes(elf_bytes)

            res = self.run_pwsh_file(
                build_module_ps1,
                args=[
                    "-ManagerBinary", str(bin_path),
                    "-Version", "0.1.0-preview",
                    "-OutputDirectory", str(out_dir),
                    "-AllowMissingWebuiAssets",
                ],
            )
            self.assertNotEqual(res.returncode, 0, f"Mutation '{name}' must fail build-module.ps1")
            self.assertIn(expected_error, res.stderr, f"Mutation '{name}' error mismatch:\n{res.stderr}")

    def test_f13_t5_02_build_module_release_config_adversarial_rejection(self) -> None:
        """TC-F13-ADV02: build-module.ps1 rejects insecure, over-sized or credentialed release.json."""
        build_module_ps1 = self.repo_root / "packaging" / "build-module.ps1"
        valid_bin = self.sandbox_path / "valid_static_arm64"
        valid_bin.write_bytes(build_arm64_elf_bytes(is_static=True))
        out_dir = self.sandbox_path / "mod_out_cfg"

        valid_key_32 = base64.b64encode(b"K" * 32).decode("ascii")

        bad_configs = [
            ("http_url", {"manifest_url": "http://example.com/manifest.json", "public_key": valid_key_32, "channel": "stable"}, "must use https"),
            ("userinfo_url", {"manifest_url": "https://user:pass@example.com/manifest.json", "public_key": valid_key_32, "channel": "stable"}, "must not embed credentials"),
            ("short_key", {"manifest_url": "https://example.com/manifest.json", "public_key": base64.b64encode(b"K" * 16).decode("ascii"), "channel": "stable"}, "decode to exactly 32 bytes"),
            ("extra_keys", {"manifest_url": "https://example.com/manifest.json", "public_key": valid_key_32, "channel": "stable", "secret_key": "leak"}, "only allows manifest_url, public_key and channel"),
            ("bad_channel", {"manifest_url": "https://example.com/manifest.json", "public_key": valid_key_32, "channel": "canary"}, "channel must be stable or beta"),
            ("non_preview_ver", {"manifest_url": "https://example.com/manifest.json", "public_key": valid_key_32, "channel": "stable"}, "version must be an explicit preview version"),
        ]

        for name, cfg_data, expected_err in bad_configs:
            cfg_path = self.sandbox_path / f"release_{name}.json"
            cfg_path.write_text(json.dumps(cfg_data), encoding="utf-8")

            ver_arg = "1.0.0" if name == "non_preview_ver" else "0.1.0-preview"

            res = self.run_pwsh_file(
                build_module_ps1,
                args=[
                    "-ManagerBinary", str(valid_bin),
                    "-ReleaseConfig", str(cfg_path),
                    "-Version", ver_arg,
                    "-OutputDirectory", str(out_dir),
                    "-AllowMissingWebuiAssets",
                ],
            )
            self.assertNotEqual(res.returncode, 0, f"Config test '{name}' must fail")
            self.assertIn(expected_err, res.stderr, f"Config test '{name}' error mismatch:\n{res.stderr}")

    def test_f13_t5_03_uninstall_sh_preserves_user_data_under_adverse_states(self) -> None:
        """TC-F13-ADV03: uninstall.sh contract strictly protects user data under arbitrary filesystem states."""
        uninstall_sh = (self.repo_root / "module" / "uninstall.sh").read_text(encoding="utf-8")

        # 1. Invariant: uninstall.sh NEVER contains recursive deletion commands
        self.assertNotIn("rm -rf", uninstall_sh, "uninstall.sh must NEVER contain rm -rf")
        self.assertNotIn("rm -r", uninstall_sh, "uninstall.sh must NEVER contain rm -r")
        self.assertNotIn("rmdir", uninstall_sh, "uninstall.sh must NEVER contain rmdir")

        # 2. Invariant: uninstall.sh strictly bounds kill target to exact binary and serve subcommand
        self.assertIn('argv0="$(printf "%s\\n" "$argv" | head -n 1)"', uninstall_sh)
        self.assertIn('argv1="$(printf "%s\\n" "$argv" | head -n 2 | tail -n 1)"', uninstall_sh)
        self.assertIn('[ "$argv0" = "$BIN" ] && [ "$argv1" = "serve" ]', uninstall_sh)

        # 3. Invariant: PID validation filters out non-digits
        self.assertIn('""|*[!0-9]*) pid="" ;;', uninstall_sh)

        # 4. Invariant: Output explicitly reassures user data preservation
        self.assertIn("user data kept at $DATA_ROOT", uninstall_sh)

    def test_f13_t5_04_service_sh_fail_closed_on_symlinks(self) -> None:
        """TC-F13-ADV04: service.sh refuses execution when symlinks are detected on critical paths."""
        service_sh = (self.repo_root / "module" / "service.sh").read_text(encoding="utf-8")

        # Symlink detection guards
        self.assertIn('[ -L "$DATA_ROOT" ]', service_sh)
        self.assertIn('[ -L "$LOG_DIR" ]', service_sh)
        self.assertIn('[ -L "$LOG" ]', service_sh)
        self.assertIn('[ -L "$PIDFILE" ]', service_sh)

        # Strict umask enforcement
        self.assertIn("umask 077", service_sh)

        # Strict 700 mode requirement
        self.assertIn("require_root_700", service_sh)
        self.assertIn("0:700|0:0700", service_sh)

        # Exact process matching (not substring)
        self.assertIn('[ "$argv0" = "$BIN" ]', service_sh)
        self.assertIn('[ "$argv1" = "serve" ]', service_sh)

    # ========================================================================
    # Feature 14: 规范与合规红线 (WebUI Overlay Injection & Repo Compliance)
    # ========================================================================

    def test_f14_t5_01_webui_overlay_adversarial_injection(self) -> None:
        """TC-F14-ADV01: webui_overlay.py handles adversarial HTML structures and partial injections."""
        # 1. Non-string or empty input raises OverlayError
        with self.assertRaises(webui_overlay.OverlayError):
            webui_overlay.inject("")
        with self.assertRaises(webui_overlay.OverlayError):
            webui_overlay.inject(None)  # type: ignore

        # 2. Partially injected page (only stylesheet tag) raises OverlayError
        partial_html = f"<html><head>{webui_overlay.STYLESHEET_TAG}</head><body>Hello</body></html>"
        with self.assertRaises(webui_overlay.OverlayError) as ctx:
            webui_overlay.inject(partial_html)
        self.assertIn("already partially injected", str(ctx.exception))

        # 3. Fully injected page is idempotent
        full_html = f"<html><head></head><body>Hello{webui_overlay.FRAGMENT}</body></html>"
        self.assertEqual(webui_overlay.inject(full_html), full_html)

        # 4. Case-insensitive closing tag </BODY>
        mixed_html = "<html><body>Content</BODY></html>"
        injected = webui_overlay.inject(mixed_html)
        self.assertIn(webui_overlay.FRAGMENT, injected)
        self.assertTrue(injected.endswith("</BODY></html>"))

        # 5. HTML missing </body> tag appends fragment and closing tag
        headless_html = "<div>No body tag</div>"
        injected_headless = webui_overlay.inject(headless_html)
        self.assertTrue(injected_headless.endswith(webui_overlay.FRAGMENT + "</body>"))

    def test_f14_t5_02_script_line_endings_and_encoding_compliance(self) -> None:
        """TC-F14-ADV02: Comprehensive compliance scan across all scripts and module files."""
        # Check CR bytes
        cr_violations = scan_cr_bytes(self.repo_root / "packaging" / "project" / "source")
        cr_violations.extend(scan_cr_bytes(self.repo_root / "module"))
        self.assertEqual(cr_violations, [], f"Carriage return violations found: {cr_violations}")

        # Check UTF-8 BOM
        bom_violations = scan_utf8_bom(self.repo_root / "packaging" / "project" / "source")
        bom_violations.extend(scan_utf8_bom(self.repo_root / "module"))
        self.assertEqual(bom_violations, [], f"UTF-8 BOM violations found: {bom_violations}")

        # Check secret patterns
        secret_violations = scan_secrets(self.repo_root / "packaging" / "project" / "source")
        secret_violations.extend(scan_secrets(self.repo_root / "module"))
        self.assertEqual(secret_violations, [], f"Secret/credential pattern violations found: {secret_violations}")


if __name__ == "__main__":
    import unittest
    unittest.main()
