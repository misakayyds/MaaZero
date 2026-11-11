#!/usr/bin/env python3
"""Unified CLI Test Runner for MAA Phone Manager E2E Test Suite.

Supports:
    python -B tests/e2e/runner.py
    python -B tests/e2e/runner.py --tier 1
    python -B tests/e2e/runner.py --tier 1,2
    python -B tests/e2e/runner.py --feature 1,2,3
    python -B tests/e2e/runner.py -v
    python -B tests/e2e/runner.py --json-report test-report.json
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

METHOD_PATTERN = re.compile(r"^test_f(\d{2})_t(\d+)_")

TIER_NAMES = {
    1: "Tier 1 (Feature Coverage)",
    2: "Tier 2 (Boundary & Corner)",
    3: "Tier 3 (Cross-Feature)",
    4: "Tier 4 (Workloads)",
    5: "Tier 5 (Adversarial Hardening)",
}

TIER3_FEATURE_MAP: Dict[str, int] = {
    "test_cross_01_weekly_plan_and_concentrated_sanity_potion": 4,
    "test_cross_02_infrast_3shift_and_task_guard": 5,
    "test_cross_03_operator_box_scan_and_cultivation_goal": 6,
    "test_cross_04_cultivation_takeover_and_sanity_arbitration": 7,
    "test_cross_05_dual_track_update_and_script_universalization": 2,
    "test_cross_06_reverse_proxy_webui_settings_auth_csrf": 3,
    "test_cross_07_chroot_task_lock_staged_download_switch_atomicity": 11,
    "test_cross_08_degraded_network_offline_rollback_sealed_descriptor": 11,
    "test_cross_09_reverse_proxy_credential_stripping_leak_prevention": 3,
    "test_cross_10_compliance_crlf_secret_violation_aborts_update": 11,
    "test_cross_11_build_project_inventory_contract_loopback_anchor": 8,
    "test_cross_12_build_project_archive_manifest_creation_signing_verify": 12,
    "test_cross_13_elf_fixture_build_module_zip_verify_unix_modes": 13,
    "test_cross_14_module_packaging_magisk_install_service_socket_contract": 13,
    "test_cross_15_end_to_end_packaging_verification_gatekeeper": 14,
}

TIER4_FEATURE_MAP: Dict[str, int] = {
    "test_workload_01_wk01_cold_boot_to_webui_session": 3,
    "test_workload_02_wk02_concurrent_webui_navigation_during_task_lock": 3,
    "test_workload_03_wk03_crash_during_switch_and_recovery": 11,
    "test_workload_04_wk04_emergency_offline_rollback": 11,
    "test_workload_05_sc01_cold_start_bootstrap": 8,
    "test_workload_06_sc02_standard_daily_routine_execution": 1,
    "test_workload_07_sc03_weekend_rollover_and_shift_handover": 4,
    "test_workload_08_sc04_event_expiration_rush_potion_drain": 7,
    "test_workload_09_wl01_deterministic_release_packaging_lifecycle": 12,
    "test_workload_10_wl02_safe_uninstallation_and_config_preservation": 13,
    "test_workload_11_wl03_clean_version_upgrade_and_migration": 2,
    "test_workload_12_wl04_cold_start_recovery_mode_lifecycle": 11,
}


def get_test_metadata(test: unittest.TestCase, default_tier: Optional[int] = None) -> Tuple[Optional[int], int]:
    """Extract feature number and tier number for any test case instance."""
    method_name = getattr(test, "_testMethodName", str(test))
    m = METHOD_PATTERN.match(method_name)
    if m:
        return int(m.group(1)), int(m.group(2))

    t_num = getattr(test, "tier", default_tier or 1)
    method = getattr(test, method_name, None)
    f_num = getattr(method, "feature", None) if method else None
    if f_num is None:
        f_num = getattr(test, "feature", None)

    if f_num is None:
        if t_num == 3 and method_name in TIER3_FEATURE_MAP:
            f_num = TIER3_FEATURE_MAP[method_name]
        elif t_num == 4 and method_name in TIER4_FEATURE_MAP:
            f_num = TIER4_FEATURE_MAP[method_name]

    return f_num, t_num


class E2ETestResult(unittest.TextTestResult):
    """Custom TestResult that captures per-test execution records and durations."""

    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.records: List[Dict[str, any]] = []
        self._start_times: Dict[str, float] = {}

    def startTest(self, test: unittest.TestCase):
        super().startTest(test)
        self._start_times[test.id()] = time.perf_counter()

    def addSuccess(self, test: unittest.TestCase):
        super().addSuccess(test)
        duration = time.perf_counter() - self._start_times.get(test.id(), time.perf_counter())
        method_name = getattr(test, "_testMethodName", str(test))
        feat, tier = get_test_metadata(test)
        self.records.append({
            "id": test.id(),
            "name": method_name,
            "feature": feat,
            "tier": tier,
            "status": "passed",
            "duration_seconds": round(duration, 4),
            "error": None,
        })

    def addFailure(self, test: unittest.TestCase, err):
        super().addFailure(test, err)
        duration = time.perf_counter() - self._start_times.get(test.id(), time.perf_counter())
        method_name = getattr(test, "_testMethodName", str(test))
        feat, tier = get_test_metadata(test)
        self.records.append({
            "id": test.id(),
            "name": method_name,
            "feature": feat,
            "tier": tier,
            "status": "failed",
            "duration_seconds": round(duration, 4),
            "error": self._exc_info_to_string(err, test),
        })

    def addError(self, test: unittest.TestCase, err):
        super().addError(test, err)
        duration = time.perf_counter() - self._start_times.get(test.id(), time.perf_counter())
        method_name = getattr(test, "_testMethodName", str(test))
        feat, tier = get_test_metadata(test)
        self.records.append({
            "id": test.id(),
            "name": method_name,
            "feature": feat,
            "tier": tier,
            "status": "error",
            "duration_seconds": round(duration, 4),
            "error": self._exc_info_to_string(err, test),
        })

    def addSkip(self, test: unittest.TestCase, reason: str):
        super().addSkip(test, reason)
        duration = time.perf_counter() - self._start_times.get(test.id(), time.perf_counter())
        method_name = getattr(test, "_testMethodName", str(test))
        feat, tier = get_test_metadata(test)
        self.records.append({
            "id": test.id(),
            "name": method_name,
            "feature": feat,
            "tier": tier,
            "status": "skipped",
            "duration_seconds": round(duration, 4),
            "error": reason,
        })


def extract_test_cases(suite: unittest.TestSuite):
    """Recursively yield all TestCase instances from a TestSuite."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from extract_test_cases(item)
        elif isinstance(item, unittest.TestCase):
            yield item


def discover_tier_tests(
    tier: int,
    requested_features: Optional[Set[int]],
    requested_tiers: Optional[Set[int]],
) -> List[unittest.TestCase]:
    """Discover and filter tests for a given tier."""
    tier_dir = REPO_ROOT / "tests" / "e2e" / f"tier{tier}"
    if not tier_dir.is_dir():
        return []

    loader = unittest.defaultTestLoader
    discovered_suite = loader.discover(
        start_dir=str(tier_dir),
        pattern="test_*.py",
        top_level_dir=str(REPO_ROOT),
    )

    matched_tests = []
    for test in extract_test_cases(discovered_suite):
        f_num, t_num = get_test_metadata(test, default_tier=tier)

        if requested_tiers is not None and t_num is not None and t_num not in requested_tiers:
            continue
        if requested_features is not None:
            if f_num is None or f_num not in requested_features:
                continue

        matched_tests.append(test)

    return matched_tests


def parse_tier_arg(val: str) -> Optional[Set[int]]:
    if not val or val.strip().lower() == "all":
        return None
    tiers = set()
    for part in val.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            num = int(p)
            if num < 1 or num > 5:
                raise ValueError(f"Tier number must be between 1 and 5, got {num}")
            tiers.add(num)
        except ValueError as ex:
            raise argparse.ArgumentTypeError(str(ex))
    return tiers


def parse_feature_arg(val: str) -> Optional[Set[int]]:
    if not val or val.strip().lower() == "all":
        return None
    features = set()
    for part in val.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            num = int(p)
            if num < 1 or num > 14:
                raise ValueError(f"Feature number must be between 1 and 14, got {num}")
            features.add(num)
        except ValueError as ex:
            raise argparse.ArgumentTypeError(str(ex))
    return features


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MAA Phone Manager Unified E2E Test Suite Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tier",
        "-t",
        type=parse_tier_arg,
        default=None,
        help="Comma-separated list of tiers to run (e.g. 1, 1,2, or all). Default: all",
    )
    parser.add_argument(
        "--feature",
        "-f",
        type=parse_feature_arg,
        default=None,
        help="Comma-separated list of features to run (e.g. 1, 12,13,14, or all). Default: all",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose test output (prints each test name and outcome).",
    )
    parser.add_argument(
        "--failfast",
        action="store_true",
        help="Stop on first failure or error.",
    )
    parser.add_argument(
        "--json-report",
        type=str,
        default=None,
        help="File path to write a JSON test execution summary.",
    )
    parser.add_argument(
        "--pwsh-path",
        type=str,
        default=None,
        help="Explicit path to pwsh (PowerShell 7) executable.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.pwsh_path:
        os.environ["PWSH_PATH"] = args.pwsh_path

    available_tiers = [1, 2, 3, 4]
    if (REPO_ROOT / "tests" / "e2e" / "tier5").is_dir():
        available_tiers.append(5)
    tiers_to_scan = available_tiers
    if args.tier is not None:
        tiers_to_scan = [t for t in [1, 2, 3, 4, 5] if t in args.tier]

    all_tier_tests: Dict[int, List[unittest.TestCase]] = {}
    total_tests_discovered = 0
    for t in tiers_to_scan:
        tests = discover_tier_tests(
            tier=t,
            requested_features=args.feature,
            requested_tiers=args.tier,
        )
        if tests:
            all_tier_tests[t] = tests
            total_tests_discovered += len(tests)

    if total_tests_discovered == 0:
        print("No tests found matching the specified filters.")
        return 0

    overall_start_time = time.perf_counter()
    tier_summaries: Dict[int, Dict[str, any]] = {}
    all_records: List[Dict[str, any]] = []
    any_failures = False

    for t, tests in all_tier_tests.items():
        tier_suite = unittest.TestSuite(tests)
        tier_start = time.perf_counter()

        runner = unittest.TextTestRunner(
            verbosity=2 if args.verbose else 1,
            failfast=args.failfast,
            resultclass=E2ETestResult,
        )
        result: E2ETestResult = runner.run(tier_suite)
        tier_duration = time.perf_counter() - tier_start

        passed_count = len([r for r in result.records if r["status"] == "passed"])
        failed_count = len(result.failures)
        errors_count = len(result.errors)
        skipped_count = len(result.skipped)

        tier_summaries[t] = {
            "name": TIER_NAMES.get(t, f"Tier {t}"),
            "total": result.testsRun,
            "passed": passed_count,
            "failed": failed_count,
            "errors": errors_count,
            "skipped": skipped_count,
            "duration_seconds": round(tier_duration, 4),
        }
        all_records.extend(result.records)

        if not result.wasSuccessful():
            any_failures = True
            if args.failfast:
                break

    total_duration = time.perf_counter() - overall_start_time

    # Compute overall totals
    total_passed = sum(s["passed"] for s in tier_summaries.values())
    total_failed = sum(s["failed"] for s in tier_summaries.values())
    total_errors = sum(s["errors"] for s in tier_summaries.values())
    total_skipped = sum(s["skipped"] for s in tier_summaries.values())
    total_run = total_passed + total_failed + total_errors + total_skipped

    # Print Formatted Execution Summary
    print()
    print("=" * 70)
    print("MAA Phone Manager E2E Test Suite Execution Summary")
    print("=" * 70)
    for t, summary in tier_summaries.items():
        label = f"{summary['name']}:".ljust(30)
        counts = f"{summary['passed']} passed, {summary['failed'] + summary['errors']} failed, {summary['skipped']} skipped"
        timing = f"[{summary['duration_seconds']:.2f}s]"
        print(f"{label} {counts.ljust(30)} {timing}")

    print("-" * 70)
    total_label = "Total Test Cases:".ljust(30)
    total_counts = f"{total_passed} passed, {total_failed + total_errors} failed, {total_skipped} skipped"
    print(f"{total_label} {total_counts}")
    print(f"{'Total Duration:'.ljust(30)} {total_duration:.2f}s")
    if not any_failures:
        print(f"{'Result:'.ljust(30)} ALL TESTS PASSED (Exit Code: 0)")
    else:
        print(f"{'Result:'.ljust(30)} TEST SUITE FAILED (Exit Code: 1)")
    print("=" * 70)

    # Write JSON report if requested
    if args.json_report:
        report_data = {
            "status": "passed" if not any_failures else "failed",
            "total": total_run,
            "passed": total_passed,
            "failed": total_failed,
            "errors": total_errors,
            "skipped": total_skipped,
            "duration_seconds": round(total_duration, 4),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tiers": {
                f"tier{t}": summary for t, summary in tier_summaries.items()
            },
            "tests": all_records,
        }
        report_path = Path(args.json_report).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report_data, f, indent=2)
        print(f"\nSaved JSON execution report to: {report_path}")

    return 1 if any_failures else 0


if __name__ == "__main__":
    sys.exit(main())
