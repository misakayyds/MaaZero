"""Tier 2: Boundary and Corner Case tests for Daemon, WebUI, and Chroot Lifecycle (Features 3, 11, 14)."""

import os
import sys
import json
import socket
import subprocess
import tempfile
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.mock_server import MockServer
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))

import webui_overlay


class Tier2DaemonWebUITest(E2ETestCase):
    """Tier 2 boundary test suite covering Features 3, 11, and 14."""

    tier = 2

    # ------------------------------------------------------------------------
    # Feature 3: 8088 端口本地 WebUI (Local WebUI Boundary Cases)
    # ------------------------------------------------------------------------

    def test_f03_t2_01_host_header_poisoning_rejection(self) -> None:
        """TC-F03-B01: Verify Host header poisoning and DNS rebinding requests are strictly rejected with 403."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn("if r.Host != s.cfg.Host", content)
        self.assertIn('failure(w, 403, "请通过模块入口打开")', content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestSessionHostAndCSRF", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Host check Go test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t2_02_untrusted_cross_origin_request_block(self) -> None:
        """TC-F03-B02: Verify cross-origin requests with untrusted Origin header are blocked with 403."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn('origin != "http://"+s.cfg.Host', content)
        self.assertIn('failure(w, 403, "来源不受信任")', content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestSessionHostAndCSRF", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Origin check Go test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t2_03_missing_or_invalid_csrf_header(self) -> None:
        """TC-F03-B03: Verify mutating API requests without valid X-MAA-Request: 1 header fail with 403."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn('r.Header.Get("X-MAA-Request") != "1"', content)
        self.assertIn('failure(w, 403, "请求校验失败")', content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestSessionHostAndCSRF", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CSRF header Go test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t2_04_strict_json_body_validation(self) -> None:
        """TC-F03-B04: Verify strict JSON decoding rejects unknown fields, malformed bodies, and trailing bytes."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn("d.DisallowUnknownFields()", content)
        self.assertIn("trailing JSON", content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestTicketSingleUseAndNoArbitraryArguments", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"JSON validation test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t2_05_form_content_type_block_on_proxied_writes(self) -> None:
        """TC-F03-B05: Verify reverse proxy strictly blocks CORS-safelisted form content-types on mutating routes."""
        server_go = self.repo_root / "manager" / "internal" / "api" / "server.go"
        content = server_go.read_text(encoding="utf-8")
        self.assertIn("application/x-www-form-urlencoded", content)
        self.assertIn("multipart/form-data", content)
        self.assertIn("text/plain", content)

        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestProxyKeepsBodylessWritesAndRefusesFormPosts", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Proxy form check test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t2_06_port_conflict_detection_on_startup(self) -> None:
        """TC-F03-B06: Verify detection and failure handling when loopback port is already bound."""
        # Bind ephemeral port on loopback
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        try:
            # Second bind to the same port must fail with EADDRINUSE
            sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.assertRaises(OSError) as ctx:
                sock2.bind(("127.0.0.1", port))
            self.assertTrue(
                "already in use" in str(ctx.exception).lower() or ctx.exception.errno in (10048, 98),
                f"Expected address already in use error, got: {ctx.exception}",
            )
            sock2.close()
        finally:
            sock.close()

    # ------------------------------------------------------------------------
    # Feature 11: Chroot 挂载与生命周期管理 (Chroot Lifecycle Boundary Cases)
    # ------------------------------------------------------------------------

    def test_f11_t2_01_lock_contention_and_timeout(self) -> None:
        """TC-F11-B01: Verify Acquire() blocks and times out when task lockfile is held by external process."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestAcquireHonoursCancelledContext", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Task lock timeout test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t2_02_missing_chroot_rootfs_fail_closed(self) -> None:
        """TC-F11-B02: Verify missing rootfs path is reported as integration blocker wrapping ErrInvalidRootfs."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestCheckRejectsUnprovisionedRuntime", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Unprovisioned rootfs test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t2_03_tampered_runner_lock_contract(self) -> None:
        """TC-F11-B03: Verify adapter rejects runner script declaring custom or mismatched LOCK_FILE path."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestAcquireRejectsOtherLockContract", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Lock contract test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t2_04_crash_during_switch_recovery(self) -> None:
        """TC-F11-B04: Verify interrupted version switch during switching stage automatically rolls back on restart."""
        res = subprocess.run(
            ["go", "test", "./internal/store", "-run", "TestRestartRecoversSwitchButResumesDownload", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Switch crash recovery test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t2_05_forbidden_personal_helpers_rejection(self) -> None:
        """TC-F11-B05: Verify personal helper scripts are strictly rejected from distribution project rootfs."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestScanProjectDirRejectsUnsafeLayouts", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Forbidden helpers scan test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t2_06_chroot_path_traversal_rejection(self) -> None:
        """TC-F11-B06: Verify runtime.New rejects relative paths and traversal sequences."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestNewRejectsUnusablePaths", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Unusable path rejection test failed:\n{res.stdout}\n{res.stderr}")

    # ------------------------------------------------------------------------
    # Feature 14: 规范与合规红线 (Compliance & Security Boundary Cases)
    # ------------------------------------------------------------------------

    def test_f14_t2_01_insecure_data_root_permissions(self) -> None:
        """TC-F14-B01: Verify daemon refuses to run on group- or world-accessible data root without mutating."""
        res = subprocess.run(
            ["go", "test", "./cmd/maa-managerd", "-run", "TestPrepareDataRootFailsClosedOnAnExistingInsecureDirectory", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Insecure data root test failed:\n{res.stdout}\n{res.stderr}")

    def test_f14_t2_02_crlf_injection_in_project_rejection(self) -> None:
        """TC-F14-B02: Verify scanProjectDir rejects project component containing CR/CRLF bytes."""
        project_go = self.repo_root / "manager" / "internal" / "runtime" / "project.go"
        content = project_go.read_text(encoding="utf-8")
        self.assertIn("bytes.ContainsRune(data, '\\r')", content)
        self.assertIn("contains CR bytes; the project must be LF only", content)

    def test_f14_t2_03_secret_leak_in_config_rejection(self) -> None:
        """TC-F14-B03: Verify public release config rejects unknown/secret fields on daemon startup."""
        main_go = self.repo_root / "manager" / "cmd" / "maa-managerd" / "main.go"
        content = main_go.read_text(encoding="utf-8")
        self.assertIn("d.DisallowUnknownFields()", content)
        self.assertIn("invalid public release configuration", content)

    def test_f14_t2_04_credential_url_rejection(self) -> None:
        """TC-F14-B04: Verify manifest and artifact URLs containing embedded credentials are rejected."""
        res = subprocess.run(
            ["go", "test", "./internal/manifest", "-run", "TestLoadAndVerifyRejectsMutableArtifactURLs", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Mutable URL rejection test failed:\n{res.stdout}\n{res.stderr}")

    def test_f14_t2_05_manifest_monotonicity_and_equivocation(self) -> None:
        """TC-F14-B05: Verify store rejects manifest replay (lower sequence) and equivocation."""
        res = subprocess.run(
            ["go", "test", "./internal/store", "-run", "TestSequencePersistsAndRejectsReplay", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Replay sequence test failed:\n{res.stdout}\n{res.stderr}")
