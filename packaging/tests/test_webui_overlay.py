"""Tests for packaging/webui_overlay.py against the real generated WebUI HTML.

The fixture is not a hand-written mock: it is produced by importing the actual
`maa-webui.py` from this repository and calling its `web_html()` entrypoint, so
the overlay is verified against the real navigation, script and style blocks.

Run: python -m unittest discover -s packaging/tests -v
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # never touch packaging/project/source/__pycache__

# Allow `python packaging/tests/test_webui_overlay.py` as well
# as `python -m unittest discover` from the packaging directory.
PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import webui_overlay


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "packaging" / "project" / "source" / "maa-webui.py").is_file():
            return parent
    raise RuntimeError("repository root with packaging/project/source/maa-webui.py not found")


WEBUI_SOURCE = (
    repo_root()
    / "packaging"
    / "project"
    / "source"
    / "maa-webui.py"
)


def real_webui_html() -> str:
    script_dir = str(WEBUI_SOURCE.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("maa_webui_under_test", WEBUI_SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.web_html()


class WebuiOverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = real_webui_html()

    def test_source_html_is_the_real_page(self):
        self.assertIn('id="n_home"', self.original)
        self.assertIn('aria-label="主导航"', self.original)
        self.assertIn("async function go(page)", self.original)
        self.assertIn("function render()", self.original)
        self.assertNotIn("module-settings", self.original)
        self.assertLess(self.original.find("</head>"), self.original.rfind("</body>"))

    def test_injects_exactly_one_stylesheet_and_one_script(self):
        injected = webui_overlay.inject(self.original)
        self.assertEqual(injected.count(webui_overlay.STYLESHEET_TAG), 1)
        self.assertEqual(injected.count(webui_overlay.SCRIPT_TAG), 1)
        self.assertEqual(injected.count(f'href="{webui_overlay.CSS_HREF}"'), 1)
        self.assertEqual(injected.count(f'src="{webui_overlay.SCRIPT_SRC}"'), 1)

    def test_injection_is_idempotent(self):
        once = webui_overlay.inject(self.original)
        twice = webui_overlay.inject(once)
        self.assertEqual(once, twice)
        self.assertTrue(webui_overlay.is_injected(once))
        self.assertFalse(webui_overlay.is_injected(self.original))

    def test_removing_injected_markup_restores_the_original_page(self):
        injected = webui_overlay.inject(self.original)
        restored = injected.replace(webui_overlay.overlay_fragment(), "", 1)
        self.assertEqual(restored, self.original)

    def test_injection_positions_and_attributes(self):
        injected = webui_overlay.inject(self.original)
        fragment = webui_overlay.overlay_fragment()
        body_end = injected.lower().rfind("</body>")
        self.assertEqual(injected[body_end - len(fragment):body_end], fragment)
        self.assertLess(injected.find("<meta charset"), body_end)
        self.assertIn("defer", webui_overlay.SCRIPT_TAG)
        self.assertIn('data-maa-overlay="module-settings"', webui_overlay.STYLESHEET_TAG)
        self.assertIn('data-maa-overlay="module-settings"', webui_overlay.SCRIPT_TAG)
        self.assertEqual(injected.count("</body>"), 1)
        self.assertEqual(injected.count("</html>"), 1)

    def test_existing_page_behaviour_is_untouched(self):
        injected = webui_overlay.inject(self.original)
        for marker in (
            'id="n_home"',
            'id="n_conn"',
            "async function go(page)",
            "function render()",
            "localStorage.getItem('maa-webui-theme')",
        ):
            self.assertEqual(
                injected.count(marker),
                self.original.count(marker),
                f"{marker} changed by the overlay",
            )

    def test_overlay_does_not_inline_any_logic(self):
        fragment = webui_overlay.overlay_fragment()
        self.assertNotIn("MAA_MODULE_SETTINGS", fragment)
        self.assertNotIn("fetch(", fragment)
        self.assertEqual(fragment.replace(webui_overlay.SCRIPT_TAG, ""), webui_overlay.STYLESHEET_TAG)
        # Only the two tag references were added to the page.
        injected = webui_overlay.inject(self.original)
        self.assertEqual(len(injected) - len(self.original), len(fragment))

    def test_bodyless_and_headless_pages_follow_the_proxy_rule(self):
        # A page without </head> is still injectable: the anchor is </body>.
        injected = webui_overlay.inject("<html><body>x</body></html>")
        self.assertIn(webui_overlay.overlay_fragment(), injected)
        # A page without </body> gains the assets and a closing tag, like the proxy.
        appended = webui_overlay.inject("<html><body>x")
        self.assertTrue(appended.endswith(webui_overlay.overlay_fragment() + "</body>"))
        self.assertEqual(appended.count("</body>"), 1)
        # Non-page input is still refused instead of being decorated blindly.
        with self.assertRaises(ValueError):
            webui_overlay.inject("")

    def test_fails_loudly_on_partial_injection(self):
        partial = self.original.replace(
            "</body>", webui_overlay.STYLESHEET_TAG + "</body>", 1
        )
        with self.assertRaises(ValueError):
            webui_overlay.inject(partial)

    def test_asset_paths_are_served_by_the_manager_prefix(self):
        self.assertTrue(webui_overlay.CSS_HREF.startswith("/module-assets/"))
        self.assertTrue(webui_overlay.SCRIPT_SRC.startswith("/module-assets/"))
        self.assertTrue(webui_overlay.CSS_HREF.endswith("module-settings.css"))
        self.assertTrue(webui_overlay.SCRIPT_SRC.endswith("module-settings.js"))

    def test_asset_paths_match_the_go_reverse_proxy_hook(self):
        """The page must request exactly the URLs the manager serves."""
        referenced = set(
            re.findall(
                r'"(/(?:module-assets|static)/module-settings\.(?:css|js))"',
                self.production_fragment_source(),
            )
        )
        if not referenced:
            self.skipTest("no module-settings asset reference found in the Go proxy")
        self.assertEqual(referenced, {webui_overlay.CSS_HREF, webui_overlay.SCRIPT_SRC})

    @staticmethod
    def production_fragment_source():
        server_go = PACKAGING_DIR.parent / "manager" / "internal" / "api" / "server.go"
        if not server_go.is_file():
            raise unittest.SkipTest("manager/internal/api/server.go is not present in this checkout")
        return server_go.read_text(encoding="utf-8")

    def production_fragment(self):
        match = re.search(r"extra\s*:=\s*`([^`]*</body>)`", self.production_fragment_source())
        if not match:
            self.skipTest("cannot locate the injected fragment literal in server.go")
        return match.group(1)

    def test_go_proxy_fragment_injects_into_the_real_page_exactly_once(self):
        """Reproduce what the Go reverse proxy does to the untouched page."""
        fragment = self.production_fragment()
        self.assertTrue(fragment.startswith('<link rel="stylesheet" href="/module-assets/'))
        self.assertIn('src="/module-assets/module-settings.js"', fragment)

        # Mirrors bytes.LastIndex + replace, i.e. the fragment replaces the last
        # closing body tag (the fragment carries its own).
        body_end = self.original.lower().rfind("</body>")
        injected = (
            self.original[:body_end]
            + fragment
            + self.original[body_end + len("</body>"):]
        )
        self.assertEqual(injected.count('href="/module-assets/module-settings.css"'), 1)
        self.assertEqual(injected.count('src="/module-assets/module-settings.js"'), 1)
        self.assertEqual(injected.count("</body>"), self.original.count("</body>"))
        self.assertEqual(injected.count("</html>"), 1)
        self.assertLess(injected.rfind(webui_overlay.SCRIPT_TAG), injected.rfind("</body>"))
        # The served bytes are exactly what the reference overlay produces.
        self.assertEqual(webui_overlay.inject(self.original), injected)
        # Removing what the proxy added restores the untouched page.
        self.assertEqual(injected.replace(fragment, "</body>", 1), self.original)

    def test_fragment_matches_the_go_proxy_byte_for_byte(self):
        self.assertEqual(webui_overlay.overlay_fragment() + "</body>", self.production_fragment())

    def test_marker_guard_prevents_a_second_injection(self):
        once = webui_overlay.inject(self.original)
        self.assertIn(webui_overlay.OVERLAY_MARKER, once)
        # Mirrors the proxy's bytes.Contains(b, moduleAssetOverlay) guard.
        self.assertEqual(webui_overlay.inject(once), once)
        self.assertEqual(once.count(webui_overlay.overlay_fragment()), 1)

    def test_emit_raw_writes_the_untouched_page(self):
        # Written inside the workspace: sandboxed runs cannot always clean the
        # system temp directory, and this keeps the test self-contained.
        target = PACKAGING_DIR / "tests" / "raw-emitted.tmp.html"
        try:
            webui_overlay.emit_raw(target, WEBUI_SOURCE)
            raw = target.read_text(encoding="utf-8")
        finally:
            target.unlink(missing_ok=True)
        self.assertFalse(webui_overlay.is_injected(raw))
        self.assertEqual(raw, self.original)

    def test_injection_paths_are_configurable(self):
        injected = webui_overlay.inject(
            self.original, css_href="/static/module-settings.css", script_src="/static/module-settings.js"
        )
        self.assertIn('href="/static/module-settings.css"', injected)
        self.assertIn('src="/static/module-settings.js"', injected)
        self.assertFalse(webui_overlay.is_injected(injected))


if __name__ == "__main__":
    unittest.main()
