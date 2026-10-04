"""Static checks for the self-contained web guest panel."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "custom_components" / "hearth_guests" / "www" / "guest.html").read_text(
    "utf-8"
)


def test_no_external_resources() -> None:
    """No CDN, fonts or other external requests: everything is inline."""
    assert not re.search(r"""(src|href)\s*=\s*["']?(https?:)?//""", HTML)
    assert "@import" not in HTML
    assert "url(http" not in HTML


def test_no_html_injection_sinks() -> None:
    """Text goes in through textContent; no innerHTML / document.write / eval."""
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in HTML, sink


def test_token_handling() -> None:
    """Token comes from the fragment, is stored, the fragment stripped, sent in the header."""
    assert "hearth_guest_token" in HTML
    assert "history.replaceState" in HTML
    assert '"X-Hearth-Guest"' in HTML
    assert "new EventSource" not in HTML  # EventSource cannot send the header


def test_script_syntax(tmp_path: Path) -> None:
    """The inline script parses (node --check), when node is available."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = tmp_path / "guest.js"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "extract_guest_js.py"), str(out)],
        check=True,
    )
    result = subprocess.run(
        [node, "--check", str(out)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
