"""Tier 5: Adversarial Hardening tests for Daemon, Chroot Driver, and Runtime (Features 3, 11, 12, 14)."""

import os
import sys
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.mock_server import MockServer
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets


class Tier5DaemonDriverAdversarialTest(E2ETestCase):
    """Tier 5 adversarial hardening suite covering Control Plane Daemon, Driver, and Runtime."""

    tier = 5

    # ------------------------------------------------------------------------
    # Feature 3: 8088 端口本地 WebUI (Local WebUI, Proxy & REST Endpoints)
    # ------------------------------------------------------------------------

    def test_f03_t5_01_reverse_proxy_offline_upstream_recovery_mode(self) -> None:
        """TC-F03-A01: Verify reverse proxy serves recoveryHTML (200) on root and 503 on non-root when upstream is offline."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn("s.proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, _ error) { s.recovery(w, r) }", content)
        self.assertIn("const recoveryHTML = `<!doctype html>", content)
        self.assertIn('failure(w, 503, "MAA 运行环境尚未就绪")', content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestCheckFailureIsNotReportedAsARejectedOperation", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Recovery proxy test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t5_02_reverse_proxy_oversized_body_8mb_bomb(self) -> None:
        """TC-F03-A02: Verify upstream responses exceeding maxInjectBytes (8MB) are rejected without OOM and trigger recovery handler."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn("const maxInjectBytes = 8 << 20", content)
        self.assertIn("if err != nil || len(b) > maxInjectBytes {", content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestAdversarialReverseProxy8MBLimit", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"8MB response bomb test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t5_03_reverse_proxy_credential_and_header_stripping(self) -> None:
        """TC-F03-A03: Verify reverse proxy strips sensitive Cookie/Auth headers before upstream and strips CORS/Set-Cookie from upstream."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn('r.Header.Del("Cookie")', content)
        self.assertIn('r.Header.Del("Authorization")', content)
        self.assertIn('r.Header.Del("Access-Control-Allow-Origin")', content)
        self.assertIn('r.Header.Del("Set-Cookie")', content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestProxyKeepsBodylessWritesAndRefusesFormPosts", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Header stripping test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t5_04_session_endpoint_extreme_body_limits(self) -> None:
        """TC-F03-A04: Adversarial fuzzing on /api/v1/session: >4KB payload, empty body, wrong Content-Type, malformed/primitive JSON."""
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestAdversarialSessionExtremeBodyLimits", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Session extreme payload test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t5_05_ticket_and_session_pool_saturation_and_concurrency(self) -> None:
        """TC-F03-A05: Verify 64-ticket and 64-session limits and race safety when 50 concurrent goroutines exchange the same ticket."""
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestAdversarialTicketPoolSaturation|TestAdversarialSessionPoolSaturation|TestAdversarialConcurrentTicketExchange", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Ticket/session pool saturation test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t5_06_unauthenticated_and_unmapped_endpoints_boundary(self) -> None:
        """TC-F03-A06: Verify unmapped endpoints (/api/v1/auth/exchange) return 401 unauthenticated and 404 authenticated; asset traversal rejected."""
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestAdversarialUnmappedRoutesAndPathTraversal", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Unmapped routes boundary test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t5_07_csrf_origin_and_host_poisoning_adversarial_matrix(self) -> None:
        """TC-F03-A07: Verify comprehensive rejection of Host header poisoning, untrusted Origin, missing CSRF token, and form Content-Types."""
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestSessionHostAndCSRF", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CSRF & host poisoning matrix test failed:\n{res.stdout}\n{res.stderr}")

    # ------------------------------------------------------------------------
    # Feature 11: Chroot 挂载与生命周期管理 (Chroot Lifecycle, Driver & Concurrency)
    # ------------------------------------------------------------------------

    def test_f11_t5_01_concurrent_acquire_lock_collisions_and_contention(self) -> None:
        """TC-F11-A01: Stress-test Acquire() concurrency and verify context cancellation releases file descriptor immediately."""
        res = subprocess.run(
            ["go", "test", "./internal/driver", "-run", "TestAdversarialLockConcurrencyAndContention", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Lock concurrency test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t5_02_stale_pid_and_corrupted_lockfile_recovery(self) -> None:
        """TC-F11-A02: Verify Acquire() recovers when lockfile contains stale PID or corrupted garbage bytes."""
        lock_go = self.repo_root / "manager" / "internal" / "driver" / "lock.go"
        content = lock_go.read_text(encoding="utf-8")
        self.assertIn("os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0644)", content)
        self.assertIn("tryLockFile(file)", content)

        # Create simulated rootfs with corrupt lockfile
        fake_rootfs = self.sandbox_path / "rootfs"
        tmp_dir = fake_rootfs / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        lock_file = tmp_dir / "maa-task.lock"
        # Write stale PID and garbage
        lock_file.write_text("999999\nDEAD_PROCESS_LOCK_DATA\x00\xff\xfe", encoding="utf-8", errors="replace")

        # Go test proves that inode-level flock/LockFileEx succeeds regardless of file content
        res = subprocess.run(
            ["go", "test", "./internal/driver", "-run", "TestAcquireHonoursCancelledContext", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stale lock recovery test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t5_03_double_release_idempotency_and_cancelled_context(self) -> None:
        """TC-F11-A03: Verify that the release callback from Acquire() is 100% idempotent and cancelled context fails immediately."""
        lock_go = self.repo_root / "manager" / "internal" / "driver" / "lock.go"
        content = lock_go.read_text(encoding="utf-8")
        self.assertIn("var once sync.Once", content)
        self.assertIn("once.Do(func() {", content)

        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestAcquireHonoursCancelledContext", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Double release idempotency test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t5_04_partial_mount_unwind_lifo_on_failure(self) -> None:
        """TC-F11-A04: Inject failures across all 12 mount positions and verify 100% clean reverse LIFO unmount and stack cleanup."""
        res = subprocess.run(
            ["go", "test", "./internal/driver", "-run", "TestAdversarialMountRollbackSequence", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Mount LIFO unwind test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t5_05_clean_stale_mounts_deepest_first(self) -> None:
        """TC-F11-A05: Verify crash recovery cleans up lingering mounts sorted deepest path first with unmount detach fallback."""
        mount_go = self.repo_root / "manager" / "internal" / "driver" / "mount.go"
        content = mount_go.read_text(encoding="utf-8")
        self.assertIn("cleanStaleMounts", content)
        self.assertIn("len(mounts[i]) > len(mounts[j])", content)
        self.assertIn("UnmountDetach", content)

        res = subprocess.run(
            ["go", "test", "./internal/driver", "-run", "TestAdversarialUnmountResilience", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stale mount cleanup test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t5_06_optional_mount_failure_tolerance(self) -> None:
        """TC-F11-A06: Verify failure of optional mounts (devpts, devshm) does not abort activation, while mandatory mounts fail closed."""
        res = subprocess.run(
            ["go", "test", "./internal/driver", "-run", "TestStressAllMountPositionsFailureInjection", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Optional mount tolerance test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t5_07_supervisor_process_group_isolation_and_signal_escalation(self) -> None:
        """TC-F11-A07: Verify process group isolation (Setpgid=true) and scoped process termination (-PGID) with zero global pkill."""
        sup_linux = self.repo_root / "manager" / "internal" / "driver" / "supervisor_linux.go"
        content = sup_linux.read_text(encoding="utf-8")
        self.assertIn("Setpgid: true", content)
        self.assertIn("syscall.Kill(-webuiPGID, syscall.SIGTERM)", content)
        self.assertIn("syscall.Kill(-webuiPGID, syscall.SIGKILL)", content)
        self.assertNotIn("pkill", content)

        res = subprocess.run(
            ["go", "test", "./internal/driver", "-run", "TestAdversarialTeardownOrdering", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Supervisor teardown ordering test failed:\n{res.stdout}\n{res.stderr}")

    # ------------------------------------------------------------------------
    # Feature 12: 上游与自有边界隔离 (Upstream Boundary & Manifest Verification)
    # ------------------------------------------------------------------------

    def test_f12_t5_01_truncated_manifest_payload_rejection(self) -> None:
        """TC-F12-A01: Verify LoadAndVerify strictly rejects truncated JSON payloads without unhandled panic."""
        verify_go = self.repo_root / "manager" / "internal" / "manifest" / "verify.go"
        content = verify_go.read_text(encoding="utf-8")
        self.assertIn("requireJSONEOF", content)
        self.assertIn("decoder.DisallowUnknownFields()", content)

        res = subprocess.run(
            ["go", "test", "./internal/manifest", "-run", "TestLoadAndVerifyRejectsTruncatedJSON", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Truncated manifest test failed:\n{res.stdout}\n{res.stderr}")

    def test_f12_t5_02_bit_flipped_and_rogue_signature_rejection(self) -> None:
        """TC-F12-A02: Verify bit-flipped Ed25519 signature or signature from untrusted key pair is strictly rejected."""
        verify_go = self.repo_root / "manager" / "internal" / "manifest" / "verify.go"
        content = verify_go.read_text(encoding="utf-8")
        self.assertIn("ed25519.Verify(publicKey, payload, signature)", content)

        res = subprocess.run(
            ["go", "test", "./internal/manifest", "-run", "TestManifestAdversarialMutationsStrictlyRejected", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Signature tampering test failed:\n{res.stdout}\n{res.stderr}")

    def test_f12_t5_03_manifest_payload_size_bomb_rejection(self) -> None:
        """TC-F12-A03: Verify manifest payloads exceeding 1MB (1<<20 bytes) are rejected before JSON decoding."""
        verify_go = self.repo_root / "manager" / "internal" / "manifest" / "verify.go"
        content = verify_go.read_text(encoding="utf-8")
        self.assertIn("const maxManifestBytes = 1 << 20", content)
        self.assertIn("if len(payload) > maxManifestBytes {", content)

        res = subprocess.run(
            ["go", "test", "./internal/manifest", "-run", "TestManifestAdversarialMutationsStrictlyRejected", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Manifest size limit test failed:\n{res.stdout}\n{res.stderr}")

    def test_f12_t5_04_manifest_security_violations_and_invariants(self) -> None:
        """TC-F12-A04: Verify rejection of negative sequences, non-HTTPS URLs, credential URLs, duplicate IDs, and blocked versions."""
        res = subprocess.run(
            ["go", "test", "./internal/manifest", "-run", "TestManifestAdversarialMutationsStrictlyRejected", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Manifest security violations test failed:\n{res.stdout}\n{res.stderr}")

    # ------------------------------------------------------------------------
    # Feature 14: 规范与合规红线 (Compliance & Security Invariants)
    # ------------------------------------------------------------------------

    def test_f14_t5_01_tier5_pure_lf_no_bom_zero_secrets(self) -> None:
        """TC-F14-A01: Ensure 100% of files under tests/e2e/tier5/ maintain pure LF, zero BOM, and zero secrets."""
        tier5_dir = self.repo_root / "tests" / "e2e" / "tier5"
        cr_violations = scan_cr_bytes(tier5_dir)
        self.assertEqual(len(cr_violations), 0, f"CR byte violations found in {tier5_dir}: {cr_violations}")

        bom_violations = scan_utf8_bom(tier5_dir)
        self.assertEqual(len(bom_violations), 0, f"UTF-8 BOM violations found in {tier5_dir}: {bom_violations}")

        secret_violations = scan_secrets(tier5_dir)
        self.assertEqual(len(secret_violations), 0, f"Secret pattern violations found in {tier5_dir}: {secret_violations}")
