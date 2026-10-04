"""Fixtures for tests against a real Home Assistant (pytest-homeassistant-custom-component).

The parent conftest skips this directory when Home Assistant is not installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
from typing import Any

from aiohttp import ClientResponse
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from custom_components.hearth_guests.const import DOMAIN

DIMMABLE = {"supported_color_modes": ["brightness"], "friendly_name": "Living light"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom_components/hearth_guests."""


@pytest.fixture
def home(hass: HomeAssistant, tmp_path: Path) -> dict[str, Any]:
    """A small home: areas, a device, entities and their states."""
    # The shared APK is written to real files under <config>/.storage; keep them per test.
    hass.config.config_dir = str(tmp_path)
    hass.config.location_name = "Test Home"
    area_reg = ar.async_get(hass)
    living = area_reg.async_create("Living room")
    hall = area_reg.async_create("Hall")
    office = area_reg.async_create("Office")

    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    lock_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "lock")},
        name="Front door",
    )
    dr.async_get(hass).async_update_device(lock_device.id, area_id=hall.id)

    ent_reg = er.async_get(hass)

    def add(domain: str, object_id: str, **kwargs: Any) -> str:
        entry = ent_reg.async_get_or_create(
            domain, "test", object_id, suggested_object_id=object_id, **kwargs
        )
        return entry.entity_id

    light = add("light", "living")
    ent_reg.async_update_entity(light, area_id=living.id)
    office_light = add("light", "office")
    ent_reg.async_update_entity(office_light, area_id=office.id)
    lock = add("lock", "front", device_id=lock_device.id)
    climate = add("climate", "living_ac")
    ent_reg.async_update_entity(climate, area_id=living.id)
    alarm = add("alarm_control_panel", "home")
    ent_reg.async_update_entity(alarm, area_id=living.id)

    hass.states.async_set(light, "on", {**DIMMABLE, "brightness": 255})
    hass.states.async_set(office_light, "off", {"friendly_name": "Office light"})
    hass.states.async_set(lock, "locked", {"friendly_name": "Front door"})
    hass.states.async_set(
        climate,
        "cool",
        {
            "friendly_name": "Living AC",
            "current_temperature": 25,
            "temperature": 23,
            "min_temp": 16,
            "max_temp": 30,
            "target_temp_step": 0.5,
            "hvac_modes": ["off", "cool", "fan_only"],
            "supported_features": 1 | 128 | 256,
        },
    )
    hass.states.async_set(alarm, "armed_away", {"friendly_name": "Alarm"})
    return {
        "living": living.id,
        "hall": hall.id,
        "office": office.id,
        "light": light,
        "office_light": office_light,
        "lock": lock,
        "climate": climate,
        "alarm": alarm,
        "lock_device": lock_device.id,
    }


@pytest.fixture
def light_calls(hass: HomeAssistant) -> list[ServiceCall]:
    """Mocked light.turn_on calls."""
    return async_mock_service(hass, "light", "turn_on")


@pytest.fixture
async def entry(hass: HomeAssistant, home: dict[str, Any]) -> MockConfigEntry:
    """Set up the integration (LAN only)."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, title="Hearth Guests", data={}, options={"lan_only": True}
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


WsCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@pytest.fixture
async def ws(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client: Any) -> WsCall:
    """Send a command as the admin owner and return the response message."""
    client = await hass_ws_client(hass)

    async def call(message: dict[str, Any]) -> dict[str, Any]:
        await client.send_json_auto_id(message)
        return await client.receive_json()

    return call


async def read_event(response: ClientResponse, timeout: float = 5) -> tuple[str, Any]:
    """Read the next server-sent event (comments are skipped)."""
    event: str | None = None
    data: Any = None
    async with asyncio.timeout(timeout):
        while True:
            raw = await response.content.readline()
            if not raw:
                raise EOFError("stream closed")
            line = raw.decode().rstrip("\n")
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
            elif line == "" and event is not None:
                return event, data
