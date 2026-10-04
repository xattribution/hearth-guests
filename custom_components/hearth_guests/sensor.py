"""The sensor.hearth_guests_active entity: the number of active guest passes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, NAME, SIGNAL_PASSES_CHANGED, VERSION
from .hub import GuestHub

if TYPE_CHECKING:
    from . import HearthGuestsConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HearthGuestsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the active guests sensor."""
    async_add_entities([ActiveGuestsSensor(entry.runtime_data, entry.entry_id)])


class ActiveGuestsSensor(SensorEntity):
    """Number of active passes; the `guests` attribute lists names and expiry times."""

    _attr_has_entity_name = True
    _attr_translation_key = "active"
    _attr_should_poll = False
    _attr_icon = "mdi:account-clock"

    def __init__(self, hub: GuestHub, entry_id: str) -> None:
        """Initialize."""
        self._hub = hub
        # Kept from the Hearth Guests days so automations and the docs keep working; the
        # device is named after the product (Foyer Guests) instead.
        self.entity_id = "sensor.hearth_guests_active"
        self._attr_unique_id = f"{entry_id}_active"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            entry_type=DeviceEntryType.SERVICE,
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        """Refresh whenever passes change or expire."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_PASSES_CHANGED, self._handle_change
            )
        )

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        """Number of active passes."""
        return len(self._hub.active_guests())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Names and expiry times of active guests."""
        return {"guests": self._hub.active_guests()}
