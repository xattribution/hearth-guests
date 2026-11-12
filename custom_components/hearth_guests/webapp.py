"""HTTP views for Foyer for the web at /foyer/ (API.md, "Foyer for the web").

The files are a static app shell with no data and no secrets: the app signs in with Home
Assistant's own OAuth and talks to the websocket like the frontend does. So the views need
no auth and are not LAN only (owners use the app away from home through their external URL).
Only files in the table from webapp_files are served, so no request path reaches the disk.

The table is cached, but a HACS update replaces www/foyer in place and only asks for a
restart: until then the new index.html would name new content-hashed assets the cached table
doesn't have. So a request the table can't answer rebuilds it (at most every few seconds, so
404 scans can't keep the executor busy), and the new files are served at once.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from pathlib import Path
import time

from aiohttp import hdrs, web

from homeassistant.components.http import KEY_HASS, HomeAssistantView

from .const import URL_WEB_APP
from .webapp_files import INDEX, WEB_APP_DIR, FileEntry, build_file_table, lookup

TEXT_HEADERS = {
    hdrs.CACHE_CONTROL: "no-store",
    "X-Content-Type-Options": "nosniff",
}


class FoyerRedirectView(HomeAssistantView):
    """GET /foyer: redirect to /foyer/, so relative asset paths resolve."""

    url = URL_WEB_APP
    name = "hearth_guests:foyer:redirect"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Redirect, keeping the query string."""
        location = f"{URL_WEB_APP}/"
        if query := request.rel_url.raw_query_string:
            location = f"{location}?{query}"
        return web.Response(
            status=HTTPStatus.FOUND,
            headers={hdrs.LOCATION: location, hdrs.CACHE_CONTROL: "no-cache"},
        )

    head = get


class FoyerAppView(HomeAssistantView):
    """GET /foyer/{path}: the built web app from www/foyer."""

    url = URL_WEB_APP + "/{path:.*}"
    name = "hearth_guests:foyer"
    requires_auth = False
    # A miss rebuilds the table at most this often (seconds).
    refresh_interval = 5.0

    def __init__(self, root: Path = WEB_APP_DIR) -> None:
        """Initialize; the folder is read on first use, so a missing build is harmless."""
        self._root = root
        self._files: dict[str, FileEntry] | None = None
        self._built_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _build(self, request: web.Request) -> dict[str, FileEntry]:
        hass = request.app[KEY_HASS]
        files = await hass.async_add_executor_job(build_file_table, self._root)
        self._built_at = time.monotonic()
        if INDEX in files:
            self._files = files
        return files  # Not built (yet): looked at again on the next request.

    async def _table(self, request: web.Request) -> dict[str, FileEntry]:
        if self._files is None:
            async with self._lock:
                if self._files is None:
                    return await self._build(request)
        return self._files

    async def _refreshed(self, request: web.Request) -> dict[str, FileEntry] | None:
        """The table rebuilt from disk, unless it was rebuilt moments ago (then None)."""
        asked = time.monotonic()
        async with self._lock:
            if self._built_at >= asked:
                return self._files  # rebuilt while we waited for the lock
            if asked - self._built_at < self.refresh_interval:
                return None
            return await self._build(request)

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        """Serve one file from the table, or 404."""
        files = await self._table(request)
        if INDEX not in files:
            return web.Response(
                text="Foyer web app is not built.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                headers=TEXT_HEADERS,
            )
        entry = lookup(files, path)
        if entry is None and (fresh := await self._refreshed(request)) is not None:
            entry = lookup(fresh, path)
        if entry is None:
            return web.Response(
                text="Not found.", status=HTTPStatus.NOT_FOUND, headers=TEXT_HEADERS
            )
        return web.FileResponse(entry.path, headers=dict(entry.headers))

    head = get  # FileResponse sends the headers only
