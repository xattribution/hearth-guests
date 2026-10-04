"""Owner API: Home Assistant websocket commands (admin only). See API.md, "Owner API"."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import ACTIVITY_MAX
from .hub import GuestHub, get_hub
from .passes import UNSET, PassError, PassNotFound, parse_datetime
from .scope import Scope, ScopeError

ERR_NOT_LOADED = "not_loaded"

_BASE_URL = vol.Optional("base_url")
_BASE_URL_VALUE = vol.Any(None, vol.All(str, vol.Length(max=512)))
_PASS_ID = vol.All(str, vol.Length(min=1, max=64))
_EXPIRES = vol.Any(None, str)

_Handler = Callable[
    [HomeAssistant, websocket_api.ActiveConnection, dict[str, Any], GuestHub],
    Awaitable[None],
]


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the websocket commands. Call once per Home Assistant run."""
    for command in (
        ws_info,
        ws_list,
        ws_create,
        ws_update,
        ws_extend,
        ws_rotate,
        ws_delete,
        ws_preview,
        ws_activity,
    ):
        websocket_api.async_register_command(hass, command)


def _with_hub(
    func: _Handler,
) -> Callable[
    [HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]], Awaitable[None]
]:
    """Look up the hub and turn pass/scope errors into websocket errors."""

    @wraps(func)
    async def wrapper(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        hub = get_hub(hass)
        if hub is None:
            connection.send_error(msg["id"], ERR_NOT_LOADED, "Foyer Guests is not loaded")
            return
        try:
            await func(hass, connection, msg, hub)
        except PassNotFound:
            connection.send_error(
                msg["id"], websocket_api.ERR_NOT_FOUND, "Pass not found"
            )
        except (PassError, ScopeError) as err:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))

    return wrapper


def _scope(value: Any) -> Scope:
    return Scope.from_dict(value)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "hearth_guests/info"})
@websocket_api.async_response
@_with_hub
async def ws_info(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Integration info, presets and the shared APK."""
    connection.send_result(msg["id"], hub.info())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "hearth_guests/passes/list", _BASE_URL: _BASE_URL_VALUE}
)
@websocket_api.async_response
@_with_hub
async def ws_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """All passes, newest first."""
    snapshot = hub.snapshot()
    connection.send_result(
        msg["id"],
        {
            "passes": [
                hub.pass_payload(pass_, msg.get("base_url"), snapshot)
                for pass_ in hub.book.newest_first()
            ]
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hearth_guests/passes/create",
        vol.Required("name"): str,
        vol.Required("scope"): dict,
        vol.Optional("expires_at"): _EXPIRES,
        vol.Optional("duration_minutes"): vol.Any(None, vol.All(int, vol.Range(min=1))),
        vol.Optional("remote", default=False): bool,
        _BASE_URL: _BASE_URL_VALUE,
    }
)
@websocket_api.async_response
@_with_hub
async def ws_create(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Create a pass."""
    pass_ = await hub.async_create(
        name=msg["name"],
        scope=_scope(msg["scope"]),
        expires_at=parse_datetime(msg.get("expires_at")),
        duration_minutes=msg.get("duration_minutes"),
        remote=msg["remote"],
    )
    connection.send_result(
        msg["id"], {"pass": hub.pass_payload(pass_, msg.get("base_url"))}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hearth_guests/passes/update",
        vol.Required("pass_id"): _PASS_ID,
        vol.Optional("name"): str,
        vol.Optional("scope"): dict,
        vol.Optional("expires_at"): _EXPIRES,
        vol.Optional("active"): bool,
        vol.Optional("remote"): bool,
        _BASE_URL: _BASE_URL_VALUE,
    }
)
@websocket_api.async_response
@_with_hub
async def ws_update(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Update a pass. expires_at: null makes it permanent."""
    pass_ = await hub.async_update(
        msg["pass_id"],
        name=msg.get("name", UNSET),
        scope=_scope(msg["scope"]) if "scope" in msg else UNSET,
        expires_at=parse_datetime(msg["expires_at"]) if "expires_at" in msg else UNSET,
        active=msg.get("active", UNSET),
        remote=msg.get("remote", UNSET),
    )
    connection.send_result(
        msg["id"], {"pass": hub.pass_payload(pass_, msg.get("base_url"))}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hearth_guests/passes/extend",
        vol.Required("pass_id"): _PASS_ID,
        vol.Required("minutes"): vol.All(int, vol.Range(min=-10_000_000, max=10_000_000)),
        _BASE_URL: _BASE_URL_VALUE,
    }
)
@websocket_api.async_response
@_with_hub
async def ws_extend(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Extend (or shorten, with negative minutes) a pass."""
    pass_ = await hub.async_extend(msg["pass_id"], msg["minutes"])
    connection.send_result(
        msg["id"], {"pass": hub.pass_payload(pass_, msg.get("base_url"))}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hearth_guests/passes/rotate",
        vol.Required("pass_id"): _PASS_ID,
        _BASE_URL: _BASE_URL_VALUE,
    }
)
@websocket_api.async_response
@_with_hub
async def ws_rotate(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Give a pass a new token."""
    pass_ = await hub.async_rotate(msg["pass_id"])
    connection.send_result(
        msg["id"], {"pass": hub.pass_payload(pass_, msg.get("base_url"))}
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hearth_guests/passes/delete",
        vol.Required("pass_id"): _PASS_ID,
    }
)
@websocket_api.async_response
@_with_hub
async def ws_delete(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Delete a pass."""
    await hub.async_delete(msg["pass_id"])
    connection.send_result(msg["id"], {})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): "hearth_guests/preview", vol.Required("scope"): dict}
)
@websocket_api.async_response
@_with_hub
async def ws_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """What a guest with this scope would see."""
    resolved = hub.resolve(_scope(msg["scope"]))
    connection.send_result(msg["id"], {"entities": hub.guest_entities(resolved)})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "hearth_guests/activity",
        vol.Optional("pass_id"): vol.Any(None, _PASS_ID),
        vol.Optional("limit", default=ACTIVITY_MAX): vol.All(
            int, vol.Range(min=1, max=ACTIVITY_MAX)
        ),
    }
)
@websocket_api.async_response
@_with_hub
async def ws_activity(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    hub: GuestHub,
) -> None:
    """Recent guest actions, newest first."""
    connection.send_result(
        msg["id"],
        {"activity": hub.activity_for(msg.get("pass_id"), msg["limit"])},
    )
