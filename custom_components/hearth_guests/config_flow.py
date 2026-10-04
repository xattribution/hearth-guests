"""Config flow for Hearth Guests: a single instance, plus a LAN-only option."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .const import CONF_LAN_ONLY, DEFAULT_LAN_ONLY, DOMAIN, NAME


class HearthGuestsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Hearth Guests. Nothing to configure: one confirmation step."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and create the single entry."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(
            title=NAME, data={}, options={CONF_LAN_ONLY: DEFAULT_LAN_ONLY}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return HearthGuestsOptionsFlow()


class HearthGuestsOptionsFlow(OptionsFlow):
    """Options: whether guest access is restricted to the local network."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_LAN_ONLY, DEFAULT_LAN_ONLY)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_LAN_ONLY, default=current): bool}),
        )
