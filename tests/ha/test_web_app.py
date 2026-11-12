"""Foyer for the web at /foyer/: static files, headers, no auth, no traversal."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.hearth_guests.const import FOYER_CSP
from custom_components.hearth_guests.webapp_files import WEB_APP_DIR, build_file_table

WEBAPP = "custom_components.hearth_guests.webapp"
VIEWS = "custom_components.hearth_guests.views"

needs_build = pytest.mark.skipif(
    not (WEB_APP_DIR / "index.html").is_file(), reason="www/foyer is not built"
)


def _asset() -> str:
    """The relative path of one hashed asset in the committed build."""
    return next(name for name in build_file_table() if name.startswith("assets/"))


async def test_redirect(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """/foyer redirects to /foyer/ and keeps the query string."""
    client = await hass_client_no_auth()
    resp = await client.get("/foyer", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/foyer/"
    resp = await client.get("/foyer?demo&a=b%20c", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/foyer/?demo&a=b%20c"


@needs_build
async def test_index(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """The page needs no auth, works off the LAN, and carries the strict headers."""
    client = await hass_client_no_auth()
    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        for path in ("/foyer/", "/foyer/index.html", "/foyer/?auth_callback=1&code=x"):
            resp = await client.get(path)
            assert resp.status == 200, path
            assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
            assert resp.headers["Cache-Control"] == "no-cache"
            assert resp.headers["Content-Security-Policy"] == FOYER_CSP
            assert resp.headers["Referrer-Policy"] == "no-referrer"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin"
            assert "microphone=(self)" in resp.headers["Permissions-Policy"]
            body = await resp.text()
            assert 'rel="manifest"' in body
            assert "apple-touch-icon" in body


@needs_build
async def test_files_and_caching(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """Service worker and manifest revalidate; hashed assets are immutable."""
    client = await hass_client_no_auth()

    resp = await client.get("/foyer/sw.js")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers["Content-Security-Policy"] == FOYER_CSP
    assert "Service-Worker-Allowed" not in resp.headers

    resp = await client.get("/foyer/manifest.webmanifest")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/manifest+json"
    assert resp.headers["Cache-Control"] == "no-cache"
    assert (await resp.json(content_type=None))["name"] == "Foyer"

    resp = await client.get("/foyer/icons/apple-touch-icon.png")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "image/png"
    assert resp.headers["Cache-Control"] == "public, max-age=86400"
    assert (await resp.read()).startswith(b"\x89PNG")

    resp = await client.head("/foyer/")
    assert resp.status == 200
    assert resp.headers["Content-Security-Policy"] == FOYER_CSP
    assert await resp.read() == b""
    resp = await client.head("/foyer", allow_redirects=False)
    assert resp.status == 302

    asset = _asset()
    resp = await client.get(f"/foyer/{asset}")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "Content-Security-Policy" not in resp.headers


@needs_build
async def test_precompressed(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """Brotli and gzip siblings are sent when the client accepts them."""
    client = await hass_client_no_auth()
    plain = (WEB_APP_DIR / "index.html").read_bytes()
    for encoding in ("br", "gzip"):
        resp = await client.get(
            "/foyer/",
            headers={"Accept-Encoding": encoding},
            auto_decompress=False,
        )
        assert resp.status == 200
        assert resp.headers["Content-Encoding"] == encoding
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        assert resp.headers["Vary"] == "Accept-Encoding"
        assert await resp.read() != plain
    resp = await client.get("/foyer/", headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in resp.headers
    assert await resp.read() == plain


@needs_build
async def test_not_found_and_traversal(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """Only files in the table are served: no traversal, no siblings, no source."""
    client = await hass_client_no_auth()
    for path in (
        "/foyer/nope.js",
        "/foyer/index.html.br",
        "/foyer/index.html.gz",
        "/foyer/assets/",
        "/foyer/room/kitchen",
        "/foyer/%2e%2e/manifest.json",
        "/foyer/..%2f..%2fconst.py",
        "/foyer/assets/..%2F..%2Fguest.html",
        "/foyer/%2Fetc%2Fpasswd",
    ):
        resp = await client.get(path)
        assert resp.status in (400, 404), path
        body = await resp.text()
        assert "hearth_guests" not in body, path
        assert "<html" not in body.lower(), path


async def test_missing_build(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """Without a build the integration still loads and /foyer/ says so; a later build is
    picked up without a restart."""
    client = await hass_client_no_auth()
    with patch(f"{WEBAPP}.build_file_table", return_value={}):
        resp = await client.get("/foyer/")
        assert resp.status == 503
        assert "not built" in await resp.text()
        resp = await client.get("/foyer/sw.js")
        assert resp.status == 503
    # Guest endpoints are unaffected.
    resp = await client.get("/api/hearth_guests/guest/session")
    assert resp.status == 401
    if (WEB_APP_DIR / "index.html").is_file():
        resp = await client.get("/foyer/")
        assert resp.status == 200


async def test_custom_root(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    tmp_path: Path,
    hass_client_no_auth: Any,
) -> None:
    """The view serves whatever folder it is given, and caches the table once built."""
    from custom_components.hearth_guests.webapp import FoyerAppView

    root = tmp_path / "foyer"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>Foyer</title>", "utf-8")
    (root / "secret.py").write_text("TOKEN = 1", "utf-8")
    view = FoyerAppView(root)
    view.url = "/foyer-test/{path:.*}"
    view.name = "hearth_guests:foyer_test"
    hass.http.register_view(view)
    client = await hass_client_no_auth()

    resp = await client.get("/foyer-test/")
    assert resp.status == 200
    assert "<title>Foyer</title>" in await resp.text()
    assert (await client.get("/foyer-test/secret.py")).status == 404
    (root / "late.js").write_text("1", "utf-8")
    # Rebuilt moments ago (the 404 above): misses don't hit the disk again yet.
    assert (await client.get("/foyer-test/late.js")).status == 404


async def test_update_in_place_serves_new_assets(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    tmp_path: Path,
    hass_client_no_auth: Any,
) -> None:
    """A HACS update replaces www/foyer without a restart: the new index.html names new hashed
    assets, and they are served at once instead of 404ing until Home Assistant restarts."""
    from custom_components.hearth_guests.webapp import FoyerAppView

    root = tmp_path / "foyer"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<script src="./assets/app-old.js"></script>', "utf-8")
    (root / "assets" / "app-old.js").write_text("old", "utf-8")
    view = FoyerAppView(root)
    view.url = "/foyer-upd/{path:.*}"
    view.name = "hearth_guests:foyer_upd"
    hass.http.register_view(view)
    client = await hass_client_no_auth()
    assert (await client.get("/foyer-upd/assets/app-old.js")).status == 200

    # The update: new index and asset, the old asset gone.
    (root / "assets" / "app-old.js").unlink()
    (root / "assets" / "app-new.js").write_text("new", "utf-8")
    (root / "index.html").write_text('<script src="./assets/app-new.js"></script>', "utf-8")
    view.refresh_interval = 0
    assert "app-new.js" in await (await client.get("/foyer-upd/")).text()
    resp = await client.get("/foyer-upd/assets/app-new.js")
    assert resp.status == 200
    assert await resp.text() == "new"
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert (await client.get("/foyer-upd/assets/app-old.js")).status == 404
    # Still never serves what isn't a web app file.
    (root / "secret.py").write_text("TOKEN = 1", "utf-8")
    assert (await client.get("/foyer-upd/secret.py")).status == 404
