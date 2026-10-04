"""Hearth Guests: temporary, scoped guest access to Home Assistant over the LAN.

The integration is a scoped gatekeeper: guests never get a Home Assistant account or
token. Each guest pass carries a random token; guest requests are checked against the
pass's scope and turned into whitelisted service calls. See API.md for the contract.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .hub import GuestHub
from .views import async_register_views
from .websocket import async_register_commands

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

HearthGuestsConfigEntry = ConfigEntry[GuestHub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register views and websocket commands once per Home Assistant run.

    Views cannot be unregistered, so they live for the whole run and look up the current
    hub in hass.data on each request; that keeps config entry reloads working.
    """
    async_register_views(hass)
    async_register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HearthGuestsConfigEntry) -> bool:
    """Set up Hearth Guests from a config entry."""
    hub = GuestHub(hass, entry)
    await hub.async_load()
    entry.runtime_data = hub
    hass.data[DOMAIN] = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HearthGuestsConfigEntry) -> bool:
    """Unload a config entry: close guest streams and flush storage."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if hass.data.get(DOMAIN) is entry.runtime_data:
            hass.data.pop(DOMAIN)
        await entry.runtime_data.async_unload()
    return unload_ok
