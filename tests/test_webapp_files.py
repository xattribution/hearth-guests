"""Foyer for the web: the file table behind /foyer/ and the committed build."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest

from hearth_guests.const import FOYER_CSP
from hearth_guests.webapp_files import (
    CACHE_ICONS,
    CACHE_IMMUTABLE,
    CACHE_REVALIDATE,
    WEB_APP_DIR,
    build_file_table,
    headers_for,
    lookup,
)


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A small build folder with every kind of file, plus things that must be skipped."""
    root = tmp_path / "foyer"
    for name in (
        "index.html",
        "index.html.br",
        "index.html.gz",
        "sw.js",
        "boot.js",
        "manifest.webmanifest",
        "licenses.txt",
        "assets/index-AbC123.js",
        "assets/index-AbC123.js.gz",
        "assets/index-Def456.css",
        "icons/icon-192.png",
        "icons/mark.svg",
        ".hidden.js",
        ".git/config.txt",
        "notes.md",
        "server.py",
        "assets/index-AbC123.js.map",
    ):
        _write(root / name)
    secret = tmp_path / "outside.txt"
    _write(secret, "not for the web")
    outside_dir = tmp_path / "outside"
    _write(outside_dir / "leak.js")
    try:
        (root / "link.txt").symlink_to(secret)
        (root / "linked").symlink_to(outside_dir, target_is_directory=True)
    except OSError:  # pragma: no cover - filesystems without symlinks
        pass
    return root


def test_table_lists_only_servable_files(site: Path) -> None:
    """Hidden, unknown, precompressed, source map and symlinked files are left out."""
    assert sorted(build_file_table(site)) == [
        "assets/index-AbC123.js",
        "assets/index-Def456.css",
        "boot.js",
        "icons/icon-192.png",
        "icons/mark.svg",
        "index.html",
        "licenses.txt",
        "manifest.webmanifest",
        "sw.js",
    ]


def test_missing_folder_gives_an_empty_table(tmp_path: Path) -> None:
    """A missing build is not an error."""
    assert build_file_table(tmp_path / "nope") == {}


def test_lookup_never_leaves_the_table(site: Path) -> None:
    """Paths are only looked up, never joined onto the filesystem."""
    table = build_file_table(site)
    assert lookup(table, "") is table["index.html"]
    assert lookup(table, "index.html") is table["index.html"]
    for path in (
        "../outside.txt",
        "assets/../index.html",
        "/etc/passwd",
        "..%2foutside.txt",
        "index.html.br",
        "index.html.gz",
        "assets/index-AbC123.js.map",
        "server.py",
        "link.txt",
        "linked/leak.js",
        ".hidden.js",
        "ASSETS/index-AbC123.js",
        "assets/",
        "room/kitchen",
    ):
        assert lookup(table, path) is None, path


def test_headers(site: Path) -> None:
    """Content types, caching and the security headers per DESIGN.md section 6."""
    table = build_file_table(site)

    index = table["index.html"].headers
    assert index["Content-Type"] == "text/html; charset=utf-8"
    assert index["Cache-Control"] == CACHE_REVALIDATE == "no-cache"
    assert index["Content-Security-Policy"] == FOYER_CSP
    assert index["Referrer-Policy"] == "no-referrer"
    assert index["X-Content-Type-Options"] == "nosniff"
    assert index["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "microphone=(self)" in index["Permissions-Policy"]
    assert index["Vary"] == "Accept-Encoding"

    sw = table["sw.js"].headers
    assert sw["Content-Type"] == "text/javascript; charset=utf-8"
    assert sw["Cache-Control"] == "no-cache"
    assert sw["Content-Security-Policy"] == FOYER_CSP
    assert "Vary" not in sw

    assert table["boot.js"].headers["Cache-Control"] == "no-cache"
    assert "Content-Security-Policy" not in table["boot.js"].headers
    manifest = table["manifest.webmanifest"].headers
    assert manifest["Content-Type"] == "application/manifest+json"
    assert manifest["Cache-Control"] == "no-cache"
    assert table["licenses.txt"].headers["Content-Type"] == "text/plain; charset=utf-8"

    js = table["assets/index-AbC123.js"].headers
    assert js["Cache-Control"] == CACHE_IMMUTABLE == "public, max-age=31536000, immutable"
    assert js["Vary"] == "Accept-Encoding"
    assert table["assets/index-Def456.css"].headers["Content-Type"] == "text/css; charset=utf-8"

    assert table["icons/icon-192.png"].headers["Cache-Control"] == CACHE_ICONS
    assert table["icons/icon-192.png"].headers["Content-Type"] == "image/png"
    assert table["icons/mark.svg"].headers["Content-Type"] == "image/svg+xml"

    for entry in table.values():
        assert entry.headers["X-Content-Type-Options"] == "nosniff"
        assert entry.headers["Referrer-Policy"] == "no-referrer"
        with pytest.raises(TypeError):
            entry.headers["Cache-Control"] = "public"  # type: ignore[index]


def test_csp_is_strict() -> None:
    """No unsafe keywords, wildcards or remote origins."""
    assert "unsafe" not in FOYER_CSP
    assert "*" not in FOYER_CSP
    assert not re.search(r"https?:", FOYER_CSP)
    assert "default-src 'none'" in FOYER_CSP
    assert "connect-src 'self'" in FOYER_CSP
    assert "frame-ancestors 'self'" in FOYER_CSP
    assert headers_for("index.html")["Content-Security-Policy"] == FOYER_CSP


# --- The committed build (www/foyer) ------------------------------------------------------------

BUILT = (WEB_APP_DIR / "index.html").is_file()
needs_build = pytest.mark.skipif(not BUILT, reason="www/foyer is not built")


@needs_build
def test_build_has_the_app_shell() -> None:
    """Everything the page and the home screen need is in the table."""
    table = build_file_table()
    for name in (
        "index.html",
        "sw.js",
        "boot.js",
        "manifest.webmanifest",
        "licenses.txt",
        "icons/apple-touch-icon.png",
        "icons/icon-192.png",
        "icons/icon-512.png",
        "icons/maskable-512.png",
    ):
        assert name in table, name
    assert not list(WEB_APP_DIR.rglob("*.map")), "source maps must not be published"


@needs_build
def test_build_index_references_resolve() -> None:
    """Every relative src/href in index.html is served, and the meta CSP matches."""
    table = build_file_table()
    html = (WEB_APP_DIR / "index.html").read_text("utf-8")
    refs = re.findall(r"""(?:src|href)="\./([^"#?]+)""", html)
    assert refs
    for ref in refs:
        assert ref in table, ref
    meta = "; ".join(
        d for d in FOYER_CSP.split("; ") if not d.startswith("frame-ancestors")
    )
    assert f'<meta http-equiv="Content-Security-Policy" content="{meta}">' in html
    assert not re.search(r"""(?:src|href)=["']?(?:https?:)?//""", html)


@needs_build
def test_build_manifest_and_precache() -> None:
    """The manifest is relative, and the service worker precaches only served files.

    `scope` is "/" on purpose (foyer-web/DESIGN.md section 2): it keeps Home Assistant's
    sign-in page inside the home screen app on iOS.
    """
    table = build_file_table()
    manifest = json.loads((WEB_APP_DIR / "manifest.webmanifest").read_text("utf-8"))
    assert manifest["name"] == "Foyer"
    assert manifest["display"] == "standalone"
    assert not re.match(r"(https?:)?/", manifest["start_url"])
    assert manifest["scope"] in ("/", "./")
    for icon in manifest["icons"]:
        assert icon["src"].removeprefix("./") in table, icon["src"]

    sw = (WEB_APP_DIR / "sw.js").read_text("utf-8")
    lists = re.findall(r'\["\./"(?:,"[^"]+")*\]', sw)
    assert len(lists) == 1, "sw.js should carry exactly one precache list"
    precache = json.loads(lists[0])
    assert precache[0] == "./"
    for name in precache[1:]:
        assert name in table, name
    assert "index.html" not in precache  # cached as "./"


@needs_build
def test_build_has_no_private_addresses() -> None:
    """The folder is published to a public repository: no private IPs or real hosts."""
    private = re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    )
    for entry in build_file_table().values():
        if entry.headers["Content-Type"].startswith("image/png"):
            continue
        text = entry.path.read_text("utf-8")
        assert not private.search(text), entry.path.name
        assert "sourceMappingURL" not in text, entry.path.name


def test_precompressed_siblings_match(tmp_path: Path) -> None:
    """Headers mark a file as varying only when a precompressed sibling exists."""
    root = tmp_path / "foyer"
    _write(root / "index.html")
    _write(root / "boot.js")
    _write(root / "boot.js.br")
    table = build_file_table(root)
    assert "Vary" not in table["index.html"].headers
    assert table["boot.js"].headers["Vary"] == "Accept-Encoding"
    os.remove(root / "boot.js.br")
    assert "Vary" not in build_file_table(root)["boot.js"].headers
