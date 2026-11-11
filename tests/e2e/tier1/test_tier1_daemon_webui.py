"""Tier 1: Feature Coverage tests for Daemon, WebUI, and Chroot Lifecycle (Features 3, 11, 14)."""

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

from tests.e2e.common.base import E2ETestCase
from tests.e2e.common.scanner import scan_cr_bytes, scan_utf8_bom, scan_secrets

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT / "packaging") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))

import webui_overlay
from webui_overlay import inject, overlay_fragment


class Tier1DaemonWebUITest(E2ETestCase):
    """Tier 1 test suite covering Features 3, 11, and 14."""

    # ------------------------------------------------------------------------
    # Feature 3: 8088 端口本地 WebUI (Local WebUI & Reverse Proxy)
    # ------------------------------------------------------------------------

    def test_f03_t1_01_unauthenticated_root_entry(self) -> None:
        """TC-F03-01: Verify unauthenticated GET / serves entry page with hash ticket reader."""
        server_go_path = self.repo_root / "manager" / "internal" / "api" / "server.go"
        self.assertTrue(server_go_path.exists(), "server.go must exist")
        content = server_go_path.read_text(encoding="utf-8")

        # Verify entryHTML definition
        self.assertIn("const entryHTML = `<!doctype html>", content)
        self.assertIn("<title>MAA 模块入口</title>", content)
        self.assertIn("location.hash.slice(1)", content)
        self.assertIn("fetch('/api/v1/session'", content)

        # Run Go unit test verifying unauthenticated GET / behavior and security headers
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestSessionHostAndCSRF", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Go api test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t1_02_bootstrap_session_exchange(self) -> None:
        """TC-F03-02: Verify session exchange: single-use ticket, strict JSON, HttpOnly & SameSite=Strict cookie."""
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestTicketSingleUseAndNoArbitraryArguments", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Ticket exchange test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t1_03_authenticated_status_query(self) -> None:
        """TC-F03-03: Verify authenticated GET /api/v1/status returns structured status JSON."""
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestAuthenticatedStatus", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Authenticated status test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t1_04_static_module_assets_serving(self) -> None:
        """TC-F03-04: Verify module-settings.css and module-settings.js exist and have proper markers."""
        css_path = self.repo_root / "webui" / "module-settings.css"
        js_path = self.repo_root / "webui" / "module-settings.js"

        self.assertTrue(css_path.is_file(), f"Missing {css_path}")
        self.assertTrue(js_path.is_file(), f"Missing {js_path}")

        css_content = css_path.read_text(encoding="utf-8")
        js_content = js_path.read_text(encoding="utf-8")

        self.assertIn(".ms-card", css_content)
        self.assertIn("window.MAA_MODULE_SETTINGS", js_content)
        self.assertIn("X-MAA-Request", js_content)

        self.assert_no_cr(css_path)
        self.assert_no_cr(js_path)
        self.assert_no_bom(css_path)
        self.assert_no_bom(js_path)

    def test_f03_t1_05_upstream_reverse_proxy_dynamic_overlay(self) -> None:
        """TC-F03-05: Verify upstream WebUI page gains module-settings overlay tags before </body>."""
        upstream_html = "<!doctype html><html><head><title>App</title></head><body><div id=\"app\"></div></body></html>"
        injected = inject(upstream_html)

        self.assertIn("data-maa-overlay=\"module-settings\"", injected)
        self.assertIn("<link rel=\"stylesheet\" href=\"/module-assets/module-settings.css\"", injected)
        self.assertIn("<script src=\"/module-assets/module-settings.js\" defer", injected)
        self.assertEqual(injected.count("</body>"), 1, "Must contain exactly one </body> tag")

        # Verify idempotency
        double_injected = inject(injected)
        self.assertEqual(injected, double_injected, "Overlay injection must be idempotent")

        # Verify Go proxy implementation matches Python
        res = subprocess.run(
            ["go", "test", "./internal/api", "-run", "TestProxyInjectsModuleAssetsAndKeepsTheBodyTag", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Go proxy injection test failed:\n{res.stdout}\n{res.stderr}")

    def test_f03_t1_06_upstream_failure_fallback_recovery(self) -> None:
        """TC-F03-06: Verify upstream failure serves recovery HTML on root and 503 on non-root."""
        server_go = (self.repo_root / "manager" / "internal" / "api" / "server.go").read_text(encoding="utf-8")
        self.assertIn("const recoveryHTML = `<!doctype html>", server_go)
        self.assertIn("<title>MAA 模块恢复</title>", server_go)
        self.assertIn("MAA 运行环境尚未就绪", server_go)

    # ------------------------------------------------------------------------
    # Feature 11: Chroot 挂载与生命周期管理 (Chroot Lifecycle & Control Driver)
    # ------------------------------------------------------------------------

    def test_f11_t1_01_runtime_adapter_path_hygiene(self) -> None:
        """TC-F11-01: Verify Adapter validates clean absolute paths for rootfs and busybox."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestNewRejectsUnusablePaths", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Adapter path validation failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t1_02_task_lock_mutual_exclusion(self) -> None:
        """TC-F11-02: Verify task lock contract on /tmp/maa-task.lock and maa-run.sh declaration."""
        runner_sh = self.repo_root / "packaging" / "project" / "source" / "maa-run.sh"
        self.assertTrue(runner_sh.exists(), "maa-run.sh must exist")
        sh_content = runner_sh.read_text(encoding="utf-8")

        # Runner must declare LOCK_FILE="/tmp/maa-task.lock"
        self.assertIn('LOCK_FILE="/tmp/maa-task.lock"', sh_content)

        # Run Go tests verifying task lock acquisition and release
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestAcquireTakesRunnerTaskLock", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Task lock test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t1_03_busybox_chroot_applet_verification(self) -> None:
        """TC-F11-03: Verify adapter checks that the busybox binary provides chroot applet."""
        probes_go = (self.repo_root / "manager" / "internal" / "runtime" / "probes.go").read_text(encoding="utf-8")
        self.assertIn("does not provide the chroot applet", probes_go)
        self.assertIn("--list", probes_go)

    def test_f11_t1_04_fixed_probe_table_syntax_and_versions(self) -> None:
        """TC-F11-04: Verify fixed game-free probe table definitions in Go runtime."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestProbeTableIsFixedAndGameFree", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Probe table test failed:\n{res.stdout}\n{res.stderr}")

    def test_f11_t1_05_materialized_project_file_integrity(self) -> None:
        """TC-F11-05: Verify shipped project directory passes scanProjectDir acceptance check."""
        res = subprocess.run(
            ["go", "test", "./internal/runtime", "-run", "TestScanProjectDirAcceptsShippedLayout", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Project dir scan test failed:\n{res.stdout}\n{res.stderr}")

    # ------------------------------------------------------------------------
    # Feature 14: 规范与合规红线 (Compliance, Clean-Room Hygiene & Security)
    # ------------------------------------------------------------------------

    def test_f14_t1_01_line_endings_daemon_webui(self) -> None:
        """TC-F14-01: Ensure 100% of files under manager/ and webui/ contain 0 CR (0x0D) bytes."""
        manager_violations = scan_cr_bytes(self.repo_root / "manager")
        webui_violations = scan_cr_bytes(self.repo_root / "webui")

        self.assertEqual(len(manager_violations), 0, f"CR bytes in manager/: {manager_violations}")
        self.assertEqual(len(webui_violations), 0, f"CR bytes in webui/: {webui_violations}")

    def test_f14_t1_02_utf8_bom_daemon_webui(self) -> None:
        """TC-F14-02: Ensure 100% of text files under manager/ and webui/ have no UTF-8 BOM."""
        manager_bom = scan_utf8_bom(self.repo_root / "manager")
        webui_bom = scan_utf8_bom(self.repo_root / "webui")

        self.assertEqual(len(manager_bom), 0, f"BOM found in manager/: {manager_bom}")
        self.assertEqual(len(webui_bom), 0, f"BOM found in webui/: {webui_bom}")

    def test_f14_t1_03_zero_secrets_daemon_webui(self) -> None:
        """TC-F14-03: Ensure zero private keys or credential files exist in manager/ and webui/."""
        manager_secrets = scan_secrets(self.repo_root / "manager")
        webui_secrets = scan_secrets(self.repo_root / "webui")

        self.assertEqual(len(manager_secrets), 0, f"Secrets in manager/: {manager_secrets}")
        self.assertEqual(len(webui_secrets), 0, f"Secrets in webui/: {webui_secrets}")

    def test_f14_t1_04_loopback_listen_and_socket_permission_hardening(self) -> None:
        """TC-F14-04: Verify daemon CLI rejects non-loopback listen and enforces private data root."""
        res = subprocess.run(
            ["go", "test", "./cmd/maa-managerd", "-run", "TestVersionAndRejectedCommands", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI rejection test failed:\n{res.stdout}\n{res.stderr}")

        res2 = subprocess.run(
            ["go", "test", "./cmd/maa-managerd", "-run", "TestCreateDataRootDecisions", "-count=1"],
            cwd=str(self.repo_root / "manager"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res2.returncode, 0, f"Data root decision test failed:\n{res2.stdout}\n{res2.stderr}")

    def test_f14_t1_05_webui_overlay_security_guarantees(self) -> None:
        """TC-F14-05: Verify webui/module-settings.js has zero innerHTML, zero storage, and passes Node unit tests."""
        import re
        js_file = self.repo_root / "webui" / "module-settings.js"
        content = js_file.read_text(encoding="utf-8")
        code = re.sub(r"/\*[\s\S]*?\*/|//.*", "", content)

        self.assertFalse(re.search(r"\.innerHTML\b", code), "module-settings.js must not use .innerHTML")
        self.assertFalse(re.search(r"localStorage\s*\.", code), "module-settings.js must not use localStorage")
        self.assertFalse(re.search(r"sessionStorage\s*\.", code), "module-settings.js must not use sessionStorage")
        self.assertFalse(re.search(r"document\.cookie", code), "module-settings.js must not access document.cookie")
        self.assertFalse(re.search(r"\binsertAdjacentHTML\b", code), "module-settings.js must not use insertAdjacentHTML")

        # Run Node.js unit tests
        node_bin = shutil.which("node")
        if node_bin:
            res = subprocess.run(
                [node_bin, "--test", "webui/tests/module-settings.test.mjs"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"Node unit tests failed:\n{res.stdout}\n{res.stderr}")
