"""Extract the inline script from guest.html so it can be syntax-checked with `node --check`.

Usage: python scripts/extract_guest_js.py OUT.js
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

GUEST_HTML = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hearth_guests"
    / "www"
    / "guest.html"
)


def extract(html: str) -> str:
    """Return the body of the single inline <script> element."""
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    if len(scripts) != 1:
        raise SystemExit(f"expected exactly one inline <script>, found {len(scripts)}")
    return scripts[0]


def main() -> None:
    """Write the script to the path given on the command line."""
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    Path(sys.argv[1]).write_text(extract(GUEST_HTML.read_text("utf-8")), "utf-8")


if __name__ == "__main__":
    main()
