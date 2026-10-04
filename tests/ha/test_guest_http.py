"""Guest HTTP API: session, actions, LAN-only, bad tokens, events stream, APK sharing."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.hearth_guests.const import DOMAIN, EVENT_ACTION

from .conftest import WsCall, read_event

SESSION = "/api/hearth_guests/guest/session"
ACTION = "/api/hearth_guests/guest/action"
EVENTS = "/api/hearth_guests/guest/events"
VIEWS = "custom_components.hearth_guests.views"


async def _create(ws: WsCall, **kwargs: Any) -> dict[str, Any]:
    payload = {
        "type": "hearth_guests/passes/create",
        "name": "Sam",
        "scope": {"capabilities": ["lights", "climate"], "all_areas": True},
        **kwargs,
    }
    msg = await ws(payload)
    assert msg["success"], msg
    return msg["result"]["pass"]


def _minutes_ago(minutes: int) -> str:
    return (dt_util.utcnow() - timedelta(minutes=minutes)).isoformat()


def _h(token: str) -> dict[str, str]:
    return {"X-Hearth-Guest": token}


async def test_landing_page(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """GET /hearth-guest serves the panel with strict headers, LAN only."""
    client = await hass_client_no_auth()
    resp = await client.get("/hearth-guest")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "connect-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.content_type == "text/html"
    body = await resp.text()
    assert "hearth_guest_token" in body

    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        resp = await client.get("/hearth-guest")
    assert resp.status == 403


async def test_session(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
    home: dict,
) -> None:
    """The session lists only in-scope entities and their areas."""
    pass_ = await _create(ws, duration_minutes=60)
    client = await hass_client_no_auth()
    resp = await client.get(SESSION, headers=_h(pass_["token"]))
    assert resp.status == 200, await resp.text()
    assert resp.headers["Cache-Control"] == "no-store"
    data = await resp.json()
    assert data["api_version"] == 1
    assert data["home_name"] == "Test Home"
    assert data["pass"] == {
        "name": "Sam",
        "expires_at": pass_["expires_at"],
        "permanent": False,
    }
    assert {e["entity_id"] for e in data["entities"]} == {
        home["light"],
        home["office_light"],
        home["climate"],
    }
    assert data["areas"] == [
        {"area_id": home["living"], "name": "Living room"},
        {"area_id": home["office"], "name": "Office"},
    ]
    assert "server_time" in data

    listing = await ws({"type": "hearth_guests/passes/list"})
    assert listing["result"]["passes"][0]["last_seen"] is not None


async def test_actions(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
    home: dict,
    light_calls: list[ServiceCall],
) -> None:
    """Allowed actions become service calls; everything else is refused."""
    climate_calls = async_mock_service(hass, "climate", "set_temperature")
    events = async_capture_events(hass, EVENT_ACTION)
    pass_ = await _create(ws)
    client = await hass_client_no_auth()
    headers = _h(pass_["token"])

    resp = await client.post(
        ACTION,
        json={"entity_id": home["light"], "action": "set_brightness", "value": 40},
        headers=headers,
    )
    assert resp.status == 200, await resp.text()
    assert await resp.json() == {"ok": True}
    assert light_calls[-1].data == {"entity_id": home["light"], "brightness_pct": 40}
    assert light_calls[-1].context.user_id is None

    resp = await client.post(
        ACTION,
        json={"entity_id": home["climate"], "action": "set_temperature", "value": 99},
        headers=headers,
    )
    assert resp.status == 200
    assert climate_calls[-1].data["temperature"] == 30  # clamped to max_temp

    assert [e.data for e in events] == [
        {
            "pass_id": pass_["id"],
            "name": "Sam",
            "entity_id": home["light"],
            "action": "set_brightness",
            "value": 40,
        },
        {
            "pass_id": pass_["id"],
            "name": "Sam",
            "entity_id": home["climate"],
            "action": "set_temperature",
            "value": 30.0,
        },
    ]
    activity = await ws({"type": "hearth_guests/activity", "pass_id": pass_["id"]})
    assert [a["action"] for a in activity["result"]["activity"]] == [
        "set_temperature",
        "set_brightness",
    ]
    assert activity["result"]["activity"][0]["pass_name"] == "Sam"

    async def refused(body: Any, status: int, code: str, **kwargs: Any) -> None:
        resp = await client.post(ACTION, headers=headers, **({"json": body} | kwargs))
        assert resp.status == status, (body, await resp.text())
        assert (await resp.json())["error"] == code

    # Outside the scope / not a guest capability / unknown action.
    await refused({"entity_id": home["lock"], "action": "unlock"}, 403, "not_allowed")
    await refused({"entity_id": home["alarm"], "action": "alarm_disarm"}, 403, "not_allowed")
    await refused({"entity_id": home["light"], "action": "unlock"}, 403, "not_allowed")
    await refused({"entity_id": "light.nope", "action": "turn_on"}, 403, "not_allowed")
    # Bad values.
    await refused(
        {"entity_id": home["light"], "action": "set_brightness", "value": 101},
        400,
        "bad_value",
    )
    await refused(
        {"entity_id": home["climate"], "action": "set_hvac_mode", "value": "heat"},
        400,
        "bad_value",
    )
    await refused({"entity_id": home["light"]}, 400, "bad_value")
    await refused([1, 2], 400, "bad_value")
    resp = await client.post(ACTION, data=b"{not json", headers=headers)
    assert resp.status == 400
    resp = await client.post(
        ACTION,
        data=b'{"entity_id": "' + b"x" * 5000 + b'", "action": "turn_on"}',
        headers=headers,
    )
    assert resp.status == 413

    # Scope is re-resolved at request time: excluding the light takes effect at once.
    await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": pass_["id"],
            "scope": {
                "capabilities": ["lights"],
                "all_areas": True,
                "exclude": [home["light"]],
            },
        }
    )
    await refused({"entity_id": home["light"], "action": "turn_on"}, 403, "not_allowed")


async def test_action_service_failure(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
    home: dict,
) -> None:
    """A failing service call answers 502 failed."""

    async def fail(call: ServiceCall) -> None:
        raise HomeAssistantError("device offline")

    hass.services.async_register("light", "turn_off", fail)
    pass_ = await _create(ws)
    client = await hass_client_no_auth()
    resp = await client.post(
        ACTION,
        json={"entity_id": home["light"], "action": "turn_off"},
        headers=_h(pass_["token"]),
    )
    assert resp.status == 502
    assert await resp.json() == {"error": "failed"}


async def test_paused_expired_and_rotated(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
) -> None:
    """Paused and expired passes get 403 with the guest name; old tokens get 401."""
    pass_ = await _create(ws, duration_minutes=60)
    client = await hass_client_no_auth()
    headers = _h(pass_["token"])

    await ws({"type": "hearth_guests/passes/update", "pass_id": pass_["id"], "active": False})
    resp = await client.get(SESSION, headers=headers)
    assert resp.status == 403
    assert await resp.json() == {"error": "paused", "name": "Sam"}

    await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": pass_["id"],
            "active": True,
            "expires_at": _minutes_ago(1),
        }
    )
    resp = await client.get(SESSION, headers=headers)
    assert resp.status == 403
    assert await resp.json() == {"error": "expired", "name": "Sam"}

    await ws({"type": "hearth_guests/passes/extend", "pass_id": pass_["id"], "minutes": 30})
    resp = await client.get(SESSION, headers=headers)
    assert resp.status == 200

    await ws({"type": "hearth_guests/passes/rotate", "pass_id": pass_["id"]})
    resp = await client.get(SESSION, headers=headers)
    assert resp.status == 401
    assert await resp.json() == {"error": "invalid_pass"}


async def test_lan_only_and_cloud(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
) -> None:
    """Off-LAN and Nabu Casa cloud requests are refused unless lan_only is off."""
    pass_ = await _create(ws)
    client = await hass_client_no_auth()
    headers = _h(pass_["token"])

    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        resp = await client.get(SESSION, headers=headers)
        assert resp.status == 403
        assert await resp.json() == {"error": "lan_only"}
    with patch(f"{VIEWS}.is_cloud_connection", return_value=True):
        resp = await client.get(SESSION, headers=headers)
        assert resp.status == 403
        assert await resp.json() == {"error": "lan_only"}
    with patch(f"{VIEWS}.is_cloud_connection", side_effect=RuntimeError):
        resp = await client.get(SESSION, headers=headers)
        assert resp.status == 403  # fails closed

    hass.config_entries.async_update_entry(entry, options={"lan_only": False})
    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        resp = await client.get(SESSION, headers=headers)
        assert resp.status == 200


async def test_remote_passes(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
) -> None:
    """Off the LAN only passes marked remote work; the landing page follows."""
    home = await _create(ws)
    client = await hass_client_no_auth()

    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        # No remote pass yet: refused before the token is even looked at.
        resp = await client.get(SESSION, headers=_h("x" * 43))
        assert await resp.json() == {"error": "lan_only"}
        assert (await client.get("/hearth-guest")).status == 403

    away = await _create(ws, name="Sitter", remote=True, duration_minutes=120)
    assert away["remote"] is True and home["remote"] is False
    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        resp = await client.get(SESSION, headers=_h(away["token"]))
        assert resp.status == 200
        resp = await client.get(SESSION, headers=_h(home["token"]))
        assert resp.status == 403
        assert await resp.json() == {"error": "lan_only"}
        assert (await client.get("/hearth-guest")).status == 200
    with patch(f"{VIEWS}.is_cloud_connection", return_value=True):
        resp = await client.get(SESSION, headers=_h(away["token"]))
        assert resp.status == 200

    msg = await ws({"type": "hearth_guests/passes/update", "pass_id": away["id"], "remote": False})
    assert msg["success"] and msg["result"]["pass"]["remote"] is False
    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        resp = await client.get(SESSION, headers=_h(away["token"]))
        assert await resp.json() == {"error": "lan_only"}

    info = await ws({"type": "hearth_guests/info"})
    assert "remote_passes" in info["result"]["features"]

    # With an external URL, remote passes carry a link that works from anywhere.
    await hass.config.async_update(external_url="https://example.ui.nabu.casa")
    msg = await ws({"type": "hearth_guests/passes/update", "pass_id": away["id"], "remote": True})
    link = msg["result"]["pass"]["remote_link"]
    assert link == f"https://example.ui.nabu.casa/hearth-guest#t={away['token']}"
    info = await ws({"type": "hearth_guests/info"})
    assert info["result"]["remote_base_url"] == "https://example.ui.nabu.casa"


async def test_bad_token_rate_limit(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
) -> None:
    """Ten bad tokens block the IP with 429, even for a valid token."""
    pass_ = await _create(ws)
    client = await hass_client_no_auth()
    resp = await client.get(SESSION)
    assert resp.status == 401
    assert await resp.json() == {"error": "invalid_pass"}
    for _ in range(9):
        resp = await client.get(SESSION, headers=_h("not-a-real-token"))
        assert resp.status == 401
    resp = await client.get(SESSION, headers=_h(pass_["token"]))
    assert resp.status == 429
    assert await resp.json() == {"error": "rate_limited"}


async def test_not_loaded(
    hass: HomeAssistant, entry: MockConfigEntry, hass_client_no_auth: Any
) -> None:
    """After unload the views stay registered but answer 503."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    client = await hass_client_no_auth()
    resp = await client.get(SESSION, headers=_h("x"))
    assert resp.status == 503


async def test_events_stream(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
    home: dict,
) -> None:
    """The stream sends initial states, changes, session updates and ended."""
    pass_ = await _create(
        ws, scope={"capabilities": ["lights"], "areas": [home["living"]]}
    )
    client = await hass_client_no_auth()
    resp = await client.get(EVENTS, headers=_h(pass_["token"]))
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")

    event, data = await read_event(resp)
    assert event == "state"
    assert data["entity_id"] == home["light"]
    assert data["attributes"]["brightness_pct"] == 100
    hub = hass.data[DOMAIN]
    assert hub.stream_count(pass_["id"]) == 1

    hass.states.async_set(home["light"], "on", {"brightness": 51})
    hass.states.async_set(home["office_light"], "on")  # out of scope: not sent
    event, data = await read_event(resp)
    assert (event, data["attributes"]["brightness_pct"]) == ("state", 20)

    await ws(
        {"type": "hearth_guests/passes/update", "pass_id": pass_["id"], "name": "Sammy"}
    )
    event, data = await read_event(resp)
    assert event == "session"
    assert data["pass"]["name"] == "Sammy"

    # Scope change: session, then a state for every entity of the new scope.
    await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": pass_["id"],
            "scope": {"capabilities": ["lights"], "areas": [home["office"]]},
        }
    )
    event, _ = await read_event(resp)
    assert event == "session"
    event, data = await read_event(resp)
    assert (event, data["entity_id"]) == ("state", home["office_light"])
    hass.states.async_set(home["light"], "off")  # no longer in scope
    hass.states.async_set(home["office_light"], "off")
    event, data = await read_event(resp)
    assert (event, data["entity_id"], data["state"]) == (
        "state",
        home["office_light"],
        "off",
    )

    await ws({"type": "hearth_guests/passes/update", "pass_id": pass_["id"], "active": False})
    event, data = await read_event(resp)
    assert (event, data) == ("ended", {"reason": "paused"})
    with pytest.raises(EOFError):
        await read_event(resp)
    for _ in range(50):
        if hub.stream_count(pass_["id"]) == 0:
            break
        await asyncio.sleep(0.01)
    assert hub.stream_count(pass_["id"]) == 0


async def test_events_stream_ends_on_expiry_and_delete(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client_no_auth: Any,
    home: dict,
) -> None:
    """Expiry and deletion end open streams with the right reason."""
    client = await hass_client_no_auth()
    for reason in ("expired", "deleted"):
        pass_ = await _create(ws, scope={"capabilities": ["locks"], "all_areas": True})
        resp = await client.get(EVENTS, headers=_h(pass_["token"]))
        event, _ = await read_event(resp)
        assert event == "state"
        if reason == "expired":
            await ws(
                {
                    "type": "hearth_guests/passes/update",
                    "pass_id": pass_["id"],
                    "expires_at": _minutes_ago(1),
                }
            )
        else:
            await ws({"type": "hearth_guests/passes/delete", "pass_id": pass_["id"]})
        event, data = await read_event(resp)
        assert (event, data) == ("ended", {"reason": reason})


async def test_apk_upload_and_download(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    hass_client: Any,
    hass_client_no_auth: Any,
    hass_read_only_access_token: str,
) -> None:
    """The owner uploads an APK in chunks; guests on the LAN download it."""
    guest = await hass_client_no_auth()
    resp = await guest.get("/api/hearth_guests/app.json")
    assert resp.status == 404

    apk = b"PK\x03\x04" + bytes(range(256)) * 40
    owner = await hass_client()
    url = "/api/hearth_guests/owner/apk"
    params = {"total": len(apk), "version_code": 7, "version_name": "1.4.0"}

    read_only = await hass_client(hass_read_only_access_token)
    resp = await read_only.post(url, params={**params, "offset": 0}, data=apk[:10])
    assert resp.status == 403

    resp = await guest.post(url, params={**params, "offset": 0}, data=apk[:10])
    assert resp.status == 401

    resp = await owner.post(url, params={**params, "offset": 0}, data=apk[:4000])
    assert resp.status == 200
    assert await resp.json() == {"received": 4000}
    resp = await owner.post(url, params={**params, "offset": 5000}, data=apk[5000:])
    assert resp.status == 409
    assert await resp.json() == {"error": "bad_offset", "received": 4000}
    resp = await owner.post(url, params={**params, "offset": 4000}, data=apk[4000:])
    assert resp.status == 200
    sha = hashlib.sha256(apk).hexdigest()
    assert await resp.json() == {"received": len(apk), "done": True, "sha256": sha}

    resp = await guest.get("/api/hearth_guests/app.json")
    assert await resp.json() == {
        "version_name": "1.4.0",
        "version_code": 7,
        "size": len(apk),
        "sha256": sha,
    }
    resp = await guest.get("/api/hearth_guests/app.apk")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/vnd.android.package-archive"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="hearth.apk"'
    assert await resp.read() == apk

    info = await ws({"type": "hearth_guests/info"})
    assert info["result"]["apk"]["sha256"] == sha

    with patch(f"{VIEWS}.is_lan_address", return_value=False):
        resp = await guest.get("/api/hearth_guests/app.apk")
        assert resp.status == 403

    resp = await owner.post(url, params={**params, "offset": 0}, data=b"MZ" + apk[2:])
    assert resp.status == 400
    assert (await resp.json())["error"] == "not_apk"
