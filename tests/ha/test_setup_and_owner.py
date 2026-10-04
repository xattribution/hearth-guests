"""Config flow, setup/unload, the sensor and the owner websocket commands."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.hearth_guests.const import (
    API_VERSION,
    DOMAIN,
    EVENT_PASS_CREATED,
    EVENT_PASS_EXPIRED,
    STORAGE_KEY,
)

from .conftest import WsCall

SCOPE = {"capabilities": ["lights", "locks"], "all_areas": True}


async def test_config_flow_single_instance(hass: HomeAssistant, home: dict) -> None:
    """The user step creates one entry with lan_only on; a second is refused."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {"lan_only": True}
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hearth_guests_active") is not None

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """lan_only can be switched off and the hub sees it immediately."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"lan_only": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"lan_only": False}
    assert entry.runtime_data.lan_only is False


async def test_info(ws: WsCall) -> None:
    """hearth_guests/info returns version, presets and no APK yet."""
    msg = await ws({"type": "hearth_guests/info"})
    assert msg["success"], msg
    info = msg["result"]
    assert info["api_version"] == API_VERSION
    # Matches manifest.json, which HACS and the release workflow read.
    manifest = json.loads((Path(__file__).parents[2] / "custom_components/hearth_guests/manifest.json").read_text())
    assert info["version"] == manifest["version"]
    assert info["lan_only"] is True
    assert info["apk"] is None
    assert info["active_count"] == 0
    assert [p["id"] for p in info["presets"]] == [
        "essentials",
        "door_and_lights",
        "house_sitter",
        "lights_only",
    ]


async def test_pass_lifecycle(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    home: dict,
    hass_storage: dict[str, Any],
) -> None:
    """create / list / update / extend / rotate / delete, sensor and storage."""
    created = async_capture_events(hass, EVENT_PASS_CREATED)
    msg = await ws(
        {
            "type": "hearth_guests/passes/create",
            "name": "Sam",
            "scope": SCOPE,
            "duration_minutes": 120,
            "base_url": "http://192.168.1.10:8123/",
        }
    )
    assert msg["success"], msg
    pass_ = msg["result"]["pass"]
    assert pass_["name"] == "Sam"
    assert pass_["status"] == "active"
    assert pass_["last_seen"] is None
    assert pass_["entity_count"] == 3  # 2 lights + lock (alarm never allowed)
    assert pass_["link"] == f"http://192.168.1.10:8123/hearth-guest#t={pass_['token']}"
    assert pass_["scope"]["capabilities"] == ["lights", "locks"]
    assert [e.data for e in created] == [{"pass_id": pass_["id"], "name": "Sam"}]
    await hass.async_block_till_done()
    state = hass.states.get("sensor.hearth_guests_active")
    assert state.state == "1"
    assert state.attributes["guests"] == [
        {"name": "Sam", "expires_at": pass_["expires_at"]}
    ]

    second = await ws(
        {"type": "hearth_guests/passes/create", "name": "Alex", "scope": SCOPE}
    )
    assert second["result"]["pass"]["expires_at"] is None
    listing = await ws({"type": "hearth_guests/passes/list"})
    assert [p["name"] for p in listing["result"]["passes"]] == ["Alex", "Sam"]

    msg = await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": pass_["id"],
            "name": "Sam K",
            "active": False,
        }
    )
    assert msg["result"]["pass"]["status"] == "paused"
    assert msg["result"]["pass"]["name"] == "Sam K"
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hearth_guests_active").state == "1"

    msg = await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": pass_["id"],
            "active": True,
            "expires_at": None,
        }
    )
    assert msg["result"]["pass"]["expires_at"] is None

    msg = await ws(
        {"type": "hearth_guests/passes/extend", "pass_id": pass_["id"], "minutes": 60}
    )
    assert msg["result"]["pass"]["expires_at"] is None  # permanent stays permanent

    msg = await ws({"type": "hearth_guests/passes/rotate", "pass_id": pass_["id"]})
    assert msg["result"]["pass"]["token"] != pass_["token"]

    msg = await ws({"type": "hearth_guests/passes/delete", "pass_id": pass_["id"]})
    assert msg["success"] and msg["result"] == {}
    msg = await ws({"type": "hearth_guests/passes/delete", "pass_id": pass_["id"]})
    assert msg["error"]["code"] == "not_found"

    # Persisted immediately, with the token stored server side.
    stored = hass_storage[STORAGE_KEY]["data"]["passes"]
    assert [p["name"] for p in stored] == ["Alex"]
    assert stored[0]["token"] == second["result"]["pass"]["token"]


async def test_invalid_input(ws: WsCall) -> None:
    """Bad scopes, names and expiry are invalid_format."""
    for message in (
        {"name": "A", "scope": {"capabilities": ["alarm"]}},
        {"name": "", "scope": SCOPE},
        {"name": "A", "scope": SCOPE, "expires_at": "not a date"},
        {"name": "A", "scope": SCOPE, "expires_at": "2000-01-01T00:00:00+00:00"},
        {
            "name": "A",
            "scope": SCOPE,
            "expires_at": "2999-01-01T00:00:00+00:00",
            "duration_minutes": 5,
        },
    ):
        msg = await ws({"type": "hearth_guests/passes/create", **message})
        assert not msg["success"], message
        assert msg["error"]["code"] == "invalid_format", msg


async def test_requires_admin(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    hass_ws_client: Any,
    hass_read_only_access_token: str,
) -> None:
    """Non-admin users cannot use the owner API."""
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": "hearth_guests/passes/list"})
    msg = await client.receive_json()
    assert msg["error"]["code"] == "unauthorized"


async def test_preview(ws: WsCall, home: dict) -> None:
    """Preview shows exactly what a guest would see, with whitelisted attributes."""
    msg = await ws(
        {
            "type": "hearth_guests/preview",
            "scope": {"capabilities": ["lights", "climate"], "areas": [home["living"]]},
        }
    )
    entities = {e["entity_id"]: e for e in msg["result"]["entities"]}
    assert set(entities) == {home["light"], home["climate"]}
    light = entities[home["light"]]
    assert light["attributes"] == {"brightness_pct": 100}
    assert light["area_id"] == home["living"]
    climate = entities[home["climate"]]
    assert climate["attributes"]["temperature_unit"] == "°C"
    assert "supported_features" not in climate["attributes"]


async def test_expiry_timer(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    ws: WsCall,
    freezer: FrozenDateTimeFactory,
) -> None:
    """When a pass expires the timer fires the bus event and updates the sensor."""
    expired = async_capture_events(hass, EVENT_PASS_EXPIRED)
    msg = await ws(
        {
            "type": "hearth_guests/passes/create",
            "name": "Brief",
            "scope": SCOPE,
            "duration_minutes": 1,
        }
    )
    pass_id = msg["result"]["pass"]["id"]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.hearth_guests_active").state == "1"

    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert [e.data for e in expired] == [{"pass_id": pass_id, "name": "Brief"}]
    assert hass.states.get("sensor.hearth_guests_active").state == "0"
    listing = await ws({"type": "hearth_guests/passes/list"})
    assert listing["result"]["passes"][0]["status"] == "expired"

    # Ending a pass early (expires_at in the past) also announces it, once.
    msg = await ws(
        {"type": "hearth_guests/passes/create", "name": "Early", "scope": SCOPE}
    )
    early = msg["result"]["pass"]["id"]
    await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": early,
            "expires_at": (dt_util.utcnow() - timedelta(minutes=1)).isoformat(),
        }
    )
    await hass.async_block_till_done()
    assert [e.data["pass_id"] for e in expired] == [pass_id, early]

    # Purged 30 days later.
    freezer.tick(timedelta(days=30, minutes=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    listing = await ws({"type": "hearth_guests/passes/list"})
    assert listing["result"]["passes"] == []


async def test_reload_keeps_passes(
    hass: HomeAssistant, entry: MockConfigEntry, ws: WsCall
) -> None:
    """Passes survive a config entry reload; views keep working with the new hub."""
    msg = await ws(
        {"type": "hearth_guests/passes/create", "name": "Kept", "scope": SCOPE}
    )
    token = msg["result"]["pass"]["token"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    hub = hass.data[DOMAIN]
    assert hub is entry.runtime_data
    assert hub.book.find_by_token(token) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert DOMAIN not in hass.data


async def test_storage_restore(
    hass: HomeAssistant, hass_storage: dict[str, Any], home: dict
) -> None:
    """Passes load from storage, and expiries missed while HA was down are announced."""
    expired = async_capture_events(hass, EVENT_PASS_EXPIRED)
    past = (dt_util.utcnow() - timedelta(hours=1)).replace(microsecond=0).isoformat()
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {
            "passes": [
                {
                    "id": "deadbeef",
                    "name": "Old",
                    "token": "dummy-token-for-tests",
                    "created_at": past,
                    "expires_at": past,
                    "active": True,
                    "last_seen": None,
                    "expiry_notified": False,
                    "scope": SCOPE,
                }
            ],
            "activity": [],
        },
    }
    config_entry = MockConfigEntry(domain=DOMAIN, data={}, options={"lan_only": True})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert [e.data for e in expired] == [{"pass_id": "deadbeef", "name": "Old"}]
    stored = hass_storage[STORAGE_KEY]["data"]["passes"]
    assert stored[0]["expiry_notified"] is True


async def test_app_payload_shapes(ws: WsCall) -> None:
    """The shapes the Hearth app sends: expires_at null on create, Z timestamps on update."""
    msg = await ws(
        {
            "type": "hearth_guests/passes/create",
            "name": " Sam ",
            "scope": SCOPE,
            "expires_at": None,
        }
    )
    assert msg["success"], msg
    pass_ = msg["result"]["pass"]
    assert pass_["expires_at"] is None
    assert pass_["name"] == "Sam"
    when = (dt_util.utcnow() + timedelta(days=2)).replace(microsecond=0)
    msg = await ws(
        {
            "type": "hearth_guests/passes/update",
            "pass_id": pass_["id"],
            "expires_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )
    assert msg["success"], msg
    assert msg["result"]["pass"]["expires_at"] == when.isoformat()
    msg = await ws({"type": "hearth_guests/activity", "limit": 50})
    assert msg["result"] == {"activity": []}
