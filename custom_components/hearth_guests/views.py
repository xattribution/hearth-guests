"""HTTP views: the guest API, the landing page and APK sharing (API.md, "Guest API").

Guest endpoints do not use Home Assistant auth (`requires_auth = False`); they authenticate
with the pass token in the X-Hearth-Guest header and are checked against the pass scope on
every request. Views are registered once per HA run and look the hub up per request, so
they keep working across config entry reloads.
"""

from __future__ import annotations

import asyncio
from functools import partial
from http import HTTPStatus
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import hdrs, web

from homeassistant.components.http import KEY_HASS, KEY_HASS_USER, HomeAssistantView
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.json import json_dumps
from homeassistant.util import dt as dt_util

from .apk import ApkError
from .const import (
    APK_MAX_CHUNK,
    HEADER_GUEST_TOKEN,
    MAX_ACTION_BODY,
    SSE_PING_INTERVAL,
    URL_APP_APK,
    URL_APP_INFO,
    URL_GUEST_ACTION,
    URL_GUEST_EVENTS,
    URL_GUEST_SESSION,
    URL_LANDING,
    URL_OWNER_APK,
)
from .hub import GuestHub, get_hub
from .netcheck import is_lan_address
from .passes import STATUS_EXPIRED, STATUS_PAUSED, Pass
from .scope import ActionError

try:  # Present in current Home Assistant; guarded so an older core fails closed.
    from homeassistant.helpers.network import is_cloud_connection
except ImportError:  # pragma: no cover
    is_cloud_connection = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

GUEST_HTML = Path(__file__).parent / "www" / "guest.html"

NO_STORE = {hdrs.CACHE_CONTROL: "no-store"}
LANDING_HEADERS = {
    hdrs.CACHE_CONTROL: "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
LAN_ONLY_HTML = (
    "<!doctype html><meta charset=utf-8><meta name=viewport "
    'content="width=device-width,initial-scale=1"><title>Hearth</title>'
    "<p style=\"font:17px system-ui;margin:2em\">Guest access only works on the home "
    "Wi-Fi. Connect to the home network and try again.</p>"
)


def async_register_views(hass: HomeAssistant) -> None:
    """Register all views. Call once per Home Assistant run."""
    for view in (
        GuestLandingView(),
        GuestSessionView(),
        GuestEventsView(),
        GuestActionView(),
        AppInfoView(),
        AppApkView(),
        OwnerApkUploadView(),
    ):
        hass.http.register_view(view)


def request_is_lan(hass: HomeAssistant, request: web.Request) -> bool:
    """True when the request comes from the local network and not via Nabu Casa cloud.

    `request.remote` is already rewritten by HA's forwarded middleware for trusted
    proxies. Cloud (remote UI) requests reach HA from a local address, so they are
    recognised through HA's own cloud check; if that check is unavailable or fails while
    the cloud integration is loaded, the request is refused.
    """
    if not is_lan_address(request.remote):
        return False
    try:
        if is_cloud_connection is None:
            return "cloud" not in hass.config.components
        return not is_cloud_connection(hass)
    except Exception:  # noqa: BLE001 - fail closed on any surprise
        _LOGGER.debug("Cloud connection check failed; refusing guest request")
        return False


def _error(code: str, status: HTTPStatus, **extra: Any) -> web.Response:
    return HomeAssistantView.json({"error": code, **extra}, status, headers=NO_STORE)


class _GuestView(HomeAssistantView):
    """Base for guest endpoints: LAN check, rate limit, token and pass status."""

    requires_auth = False

    def _hub(self, request: web.Request) -> tuple[HomeAssistant, GuestHub | None]:
        hass = request.app[KEY_HASS]
        return hass, get_hub(hass)

    def _authenticate(self, request: web.Request) -> tuple[GuestHub, Pass] | web.Response:
        """Return (hub, pass) for a usable pass, or the error response to send.

        Off the LAN (with lan_only on) only passes marked `remote` work. When no such pass
        exists, off-LAN requests are refused before any token is looked at, exactly as if
        remote passes didn't exist.
        """
        hass, hub = self._hub(request)
        if hub is None:
            return _error("not_loaded", HTTPStatus.SERVICE_UNAVAILABLE)
        now = dt_util.utcnow()
        on_lan = not hub.lan_only or request_is_lan(hass, request)
        if not on_lan and not hub.book.has_remote(now):
            return _error("lan_only", HTTPStatus.FORBIDDEN)
        remote = request.remote or "unknown"
        if hub.limiter.is_blocked(remote):
            return _error("rate_limited", HTTPStatus.TOO_MANY_REQUESTS)
        token = request.headers.get(HEADER_GUEST_TOKEN, "").strip()
        pass_ = hub.book.find_by_token(token)
        if pass_ is None:
            if hub.limiter.record_failure(remote):
                _LOGGER.warning(
                    "Blocking %s for guest requests after repeated bad tokens", remote
                )
            return _error("invalid_pass", HTTPStatus.UNAUTHORIZED)
        if not on_lan and not pass_.remote:
            return _error("lan_only", HTTPStatus.FORBIDDEN)
        status = pass_.status(now)
        if status in (STATUS_EXPIRED, STATUS_PAUSED):
            return _error(status, HTTPStatus.FORBIDDEN, name=pass_.name)
        hub.touch(pass_)
        return hub, pass_


class GuestLandingView(HomeAssistantView):
    """GET /hearth-guest: the web guest panel."""

    url = URL_LANDING
    name = "hearth_guests:landing"
    requires_auth = False

    def __init__(self) -> None:
        """Initialize; the HTML is read on first use."""
        self._html: bytes | None = None

    async def get(self, request: web.Request) -> web.Response:
        """Serve guest.html."""
        hass = request.app[KEY_HASS]
        hub = get_hub(hass)
        if hub is None:
            return web.Response(
                text="Hearth Guests is not set up.", status=503, headers=NO_STORE
            )
        # The page itself holds no data; off the LAN it is served only while some pass
        # may be used away from home.
        if (
            hub.lan_only
            and not request_is_lan(hass, request)
            and not hub.book.has_remote(dt_util.utcnow())
        ):
            return web.Response(
                text=LAN_ONLY_HTML,
                status=HTTPStatus.FORBIDDEN,
                content_type="text/html",
                headers=LANDING_HEADERS,
            )
        if self._html is None:
            self._html = await hass.async_add_executor_job(GUEST_HTML.read_bytes)
        return web.Response(
            body=self._html,
            content_type="text/html",
            charset="utf-8",
            headers=LANDING_HEADERS,
        )


class GuestSessionView(_GuestView):
    """GET /api/hearth_guests/guest/session."""

    url = URL_GUEST_SESSION
    name = "api:hearth_guests:guest:session"

    async def get(self, request: web.Request) -> web.Response:
        """Return the guest session."""
        auth = self._authenticate(request)
        if isinstance(auth, web.Response):
            return auth
        hub, pass_ = auth
        return self.json(hub.session(pass_), headers=NO_STORE)


class GuestActionView(_GuestView):
    """POST /api/hearth_guests/guest/action."""

    url = URL_GUEST_ACTION
    name = "api:hearth_guests:guest:action"

    async def post(self, request: web.Request) -> web.Response:
        """Validate and run one guest action."""
        auth = self._authenticate(request)
        if isinstance(auth, web.Response):
            return auth
        hub, pass_ = auth

        if (request.content_length or 0) > MAX_ACTION_BODY:
            return _error("bad_value", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        body = bytearray()
        async for chunk in request.content.iter_any():
            body += chunk
            if len(body) > MAX_ACTION_BODY:
                return _error("bad_value", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            data = json.loads(body)
        except ValueError:
            return _error("bad_value", HTTPStatus.BAD_REQUEST)
        if not isinstance(data, dict) or "entity_id" not in data or "action" not in data:
            return _error("bad_value", HTTPStatus.BAD_REQUEST)

        try:
            await hub.async_action(
                pass_, data["entity_id"], data["action"], data.get("value")
            )
        except ActionError as err:
            status = (
                HTTPStatus.FORBIDDEN
                if err.code == "not_allowed"
                else HTTPStatus.BAD_REQUEST
            )
            return _error(err.code, status)
        except HomeAssistantError as err:
            _LOGGER.debug("Guest action failed: %s", err)
            return _error("failed", HTTPStatus.BAD_GATEWAY)
        return self.json({"ok": True}, headers=NO_STORE)


class GuestEventsView(_GuestView):
    """GET /api/hearth_guests/guest/events: server-sent events."""

    url = URL_GUEST_EVENTS
    name = "api:hearth_guests:guest:events"

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream state changes for the pass's entities until the pass ends."""
        auth = self._authenticate(request)
        if isinstance(auth, web.Response):
            return auth
        hub, pass_ = auth
        hass = hub.hass

        response = web.StreamResponse(
            headers={
                hdrs.CONTENT_TYPE: "text/event-stream",
                hdrs.CACHE_CONTROL: "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        stream = hub.open_stream(pass_)
        resolved: dict[str, str | None] = {}
        unsub: Any = None

        @callback
        def on_state(event: Event[EventStateChangedData]) -> None:
            entity_id = event.data["entity_id"]
            if event.data["new_state"] is None or entity_id not in resolved:
                return
            entity = hub.guest_entity(entity_id, resolved[entity_id])
            if entity is not None:
                stream.push("state", entity)

        async def resubscribe() -> None:
            """(Re)resolve the scope, subscribe, and send every entity's state."""
            nonlocal resolved, unsub
            if unsub is not None:
                unsub()
                unsub = None
            resolved = hub.resolve(pass_.scope)
            if resolved:
                unsub = async_track_state_change_event(hass, list(resolved), on_state)
            for entity in hub.guest_entities(resolved):
                await _send(response, "state", entity)

        try:
            await resubscribe()
            while True:
                try:
                    async with asyncio.timeout(SSE_PING_INTERVAL):
                        item = await stream.queue.get()
                except TimeoutError:
                    await response.write(b": ping\n\n")
                    continue
                if item is None:
                    break
                kind, data = item
                if kind == "rescope":
                    await resubscribe()
                else:
                    await _send(response, kind, data)
        except ConnectionResetError:
            pass  # The guest went away.
        finally:
            if unsub is not None:
                unsub()
            hub.release_stream(stream)
        return response


async def _send(response: web.StreamResponse, event: str, data: Any) -> None:
    await response.write(f"event: {event}\ndata: {json_dumps(data)}\n\n".encode())


class _AppView(HomeAssistantView):
    """Base for the public APK endpoints (LAN only when lan_only is on)."""

    requires_auth = False

    def _check(self, request: web.Request) -> GuestHub | web.Response:
        hass = request.app[KEY_HASS]
        hub = get_hub(hass)
        if hub is None:
            return _error("not_loaded", HTTPStatus.SERVICE_UNAVAILABLE)
        if hub.lan_only and not request_is_lan(hass, request):
            return _error("lan_only", HTTPStatus.FORBIDDEN)
        if not hub.apk_meta:
            return _error("not_found", HTTPStatus.NOT_FOUND)
        return hub


class AppInfoView(_AppView):
    """GET /api/hearth_guests/app.json."""

    url = URL_APP_INFO
    name = "api:hearth_guests:app_info"

    async def get(self, request: web.Request) -> web.Response:
        """Describe the shared APK."""
        hub = self._check(request)
        if isinstance(hub, web.Response):
            return hub
        meta = hub.apk_meta or {}
        return self.json(
            {
                key: meta.get(key)
                for key in ("version_name", "version_code", "size", "sha256")
            },
            headers=NO_STORE,
        )


class AppApkView(_AppView):
    """GET /api/hearth_guests/app.apk."""

    url = URL_APP_APK
    name = "api:hearth_guests:app_apk"

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream the shared APK."""
        hub = self._check(request)
        if isinstance(hub, web.Response):
            return hub
        if not await hub.hass.async_add_executor_job(hub.apk.apk_path.is_file):
            return _error("not_found", HTTPStatus.NOT_FOUND)
        return web.FileResponse(
            hub.apk.apk_path,
            headers={
                hdrs.CONTENT_TYPE: "application/vnd.android.package-archive",
                hdrs.CONTENT_DISPOSITION: 'attachment; filename="hearth.apk"',
                hdrs.CACHE_CONTROL: "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )


def _int_param(request: web.Request, key: str) -> int | None:
    try:
        return int(request.query[key])
    except (KeyError, ValueError):
        return None


class OwnerApkUploadView(HomeAssistantView):
    """POST /api/hearth_guests/owner/apk: chunked APK upload (admin only)."""

    url = URL_OWNER_APK
    name = "api:hearth_guests:owner_apk"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        """Append one chunk of the APK upload."""
        user = request[KEY_HASS_USER]
        if not user.is_admin:
            return _error("admin_required", HTTPStatus.FORBIDDEN)
        hass = request.app[KEY_HASS]
        hub = get_hub(hass)
        if hub is None:
            return _error("not_loaded", HTTPStatus.SERVICE_UNAVAILABLE)

        offset = _int_param(request, "offset")
        total = _int_param(request, "total")
        version_code = _int_param(request, "version_code")
        version_name = request.query.get("version_name", "").strip()
        if offset is None or total is None or version_code is None or offset < 0:
            return _error("bad_request", HTTPStatus.BAD_REQUEST)

        if (request.content_length or 0) > APK_MAX_CHUNK:
            return _error("chunk_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        body = bytearray()
        async for chunk in request.content.iter_chunked(64 * 1024):
            body += chunk
            if len(body) > APK_MAX_CHUNK:
                return _error("chunk_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        async with hub.apk_lock:
            try:
                result = await hass.async_add_executor_job(
                    partial(
                        hub.apk.write_chunk,
                        offset=offset,
                        total=total,
                        version_code=version_code,
                        version_name=version_name,
                        data=bytes(body),
                    )
                )
            except ApkError as err:
                extra = {"received": err.received} if err.received is not None else {}
                status = (
                    HTTPStatus.CONFLICT
                    if err.code == "bad_offset"
                    else HTTPStatus.BAD_REQUEST
                )
                return _error(err.code, status, **extra)

        if not result.get("done"):
            return self.json({"received": result["received"]})
        hub.apk_meta = result["meta"]
        _LOGGER.info(
            "Shared Hearth APK %s (%s bytes)",
            result["meta"]["version_name"],
            result["received"],
        )
        return self.json(
            {"received": result["received"], "done": True, "sha256": result["sha256"]}
        )
