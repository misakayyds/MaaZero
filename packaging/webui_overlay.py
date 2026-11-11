#!/usr/bin/env python3
"""Inject the phone-manager "关于与模块设置" extension into the existing WebUI HTML.

Design (approved 2026-09-12)
---------------------------
* The daily UI stays the existing single-file ``maa-webui.py`` page. Nothing in
  ``packaging/project/source/`` is edited in place; the packaging layer rewrites
  the generated HTML string instead.
* Exactly one injection point is active in production: the Go reverse proxy in
  ``manager/internal/api/server.go`` injects
  ``overlay_fragment()`` before the last ``</body>`` of the proxied ``GET /``
  response and skips any page that already carries ``OVERLAY_MARKER``. The
  fragment here is byte-identical to that ``extra`` literal (see the parity
  test), so a locally generated page is faithful to the phone. This
  Python module is the reference implementation, the local fixture generator and
  the packaging test target. Do not wire it into ``maa-webui.py`` as well;
  running both would be redundant.
* Only two external references are appended - no logic is inlined - so a page
  that loses the static files still boots normally, and the extension is easy to
  audit. The markup carries ``data-maa-overlay="module-settings"``; injection is
  idempotent and refuses to touch a partially injected page.

CLI
---
Generate a real preview/fixture page (used by the local browser test)::

    python packaging/webui_overlay.py --emit out.html

Generate the untouched page the Go proxy starts from::

    python packaging/webui_overlay.py --emit-raw raw.html

Verify an already-generated page is injected exactly once::

    python packaging/webui_overlay.py --check out.html
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

CSS_HREF = "/module-assets/module-settings.css"
SCRIPT_SRC = "/module-assets/module-settings.js"
OVERLAY_MARKER = 'data-maa-overlay="module-settings"'

STYLESHEET_TAG = f'<link rel="stylesheet" href="{CSS_HREF}" {OVERLAY_MARKER}>'
SCRIPT_TAG = f'<script src="{SCRIPT_SRC}" defer {OVERLAY_MARKER}></script>'
FRAGMENT = STYLESHEET_TAG + SCRIPT_TAG


class OverlayError(ValueError):
    """Raised when the source HTML cannot be safely injected."""


def is_injected(html: str) -> bool:
    """True when both overlay references are already present."""
    return STYLESHEET_TAG in html and SCRIPT_TAG in html


def overlay_fragment(css_href: str = CSS_HREF, script_src: str = SCRIPT_SRC) -> str:
    """The exact markup the reverse proxy inserts (must match its ``extra``)."""
    return (
        f'<link rel="stylesheet" href="{css_href}" {OVERLAY_MARKER}>'
        f'<script src="{script_src}" defer {OVERLAY_MARKER}></script>'
    )


def inject(html: str, css_href: str = CSS_HREF, script_src: str = SCRIPT_SRC) -> str:
    """Return ``html`` with the module-settings stylesheet and script references.

    Both references replace the last ``</body>`` (or are appended with one when
    the page has none) - the same rule the Go proxy applies - so the extension
    still loads after the host page script has defined ``go``/``render``/
    ``PAGE_META``. Calling this twice returns the same string, which is also the
    proxy's ``OVERLAY_MARKER`` guard.
    """
    if not isinstance(html, str) or not html:
        raise OverlayError("html must be a non-empty string")

    stylesheet_tag = f'<link rel="stylesheet" href="{css_href}" {OVERLAY_MARKER}>'
    script_tag = f'<script src="{script_src}" defer {OVERLAY_MARKER}></script>'
    fragment = stylesheet_tag + script_tag

    has_style = stylesheet_tag in html
    has_script = script_tag in html
    if has_style and has_script:
        return html
    if has_style or has_script:
        raise OverlayError("page is already partially injected; refusing to guess")

    body_end = html.lower().rfind("</body>")
    if body_end < 0:
        # The proxy appends the fragment (which carries its own closing tag) to a
        # page that has none; mirror it rather than guessing a position.
        return html + fragment + "</body>"
    return html[:body_end] + fragment + html[body_end:]


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "packaging" / "project" / "source" / "maa-webui.py").is_file():
            return parent
    raise OverlayError("repository root with packaging/project/source/maa-webui.py not found")


def default_webui_source() -> Path:
    return repo_root() / "packaging" / "project" / "source" / "maa-webui.py"


def render_webui_html(source: Path | None = None) -> str:
    """Import the real ``maa-webui.py`` and return its generated page HTML."""
    source = Path(source) if source else default_webui_source()
    if not source.is_file():
        raise OverlayError(f"maa-webui.py not found at {source}")

    sys.dont_write_bytecode = True
    script_dir = str(source.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("maa_webui_overlay_source", source)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise OverlayError(f"cannot load {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for entry in ("web_html", "get_html"):
        renderer = getattr(module, entry, None)
        if callable(renderer):
            return renderer()
    raise OverlayError(f"{source} exposes neither web_html() nor get_html()")


def emit(output: Path, source: Path | None = None) -> Path:
    """Write a preview page (real WebUI HTML + overlay) to ``output``."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(inject(render_webui_html(source)), encoding="utf-8")
    return output


def emit_raw(output: Path, source: Path | None = None) -> Path:
    """Write the untouched generated WebUI page (no overlay references)."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_webui_html(source), encoding="utf-8")
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--emit", type=Path, help="write generated HTML + overlay to this file")
    parser.add_argument("--emit-raw", type=Path, help="write the untouched generated HTML to this file")
    parser.add_argument("--check", type=Path, help="verify an HTML file already contains the overlay exactly once")
    parser.add_argument("--source", type=Path, help="path to the authoritative maa-webui.py")
    args = parser.parse_args(argv)

    if args.emit:
        path = emit(args.emit, args.source)
        print(f"WROTE {path}")
        return 0

    if args.emit_raw:
        path = emit_raw(args.emit_raw, args.source)
        print(f"WROTE_RAW {path}")
        return 0

    if args.check:
        html = Path(args.check).read_text(encoding="utf-8")
        if not is_injected(html):
            print("NOT_INJECTED")
            return 1
        if html.count(STYLESHEET_TAG) != 1 or html.count(SCRIPT_TAG) != 1:
            print("DUPLICATE_INJECTION")
            return 1
        print("OVERLAY_OK")
        return 0

    parser.error("one of --emit, --emit-raw or --check is required")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
