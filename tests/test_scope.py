"""Tests for scope resolution, GuestEntity building and action translation."""

from __future__ import annotations

import math

import pytest

from hearth_guests.const import CAPABILITY_DOMAINS, DOMAIN_ACTIONS, PRESETS
from hearth_guests.scope import (
    ActionError,
    RegistrySnapshot,
    Scope,
    ScopeError,
    entity_actions,
    guest_attributes,
    guest_entity,
    resolve_scope,
    translate_action,
)

ALL_CAPS = list(CAPABILITY_DOMAINS)


@pytest.fixture
def snapshot() -> RegistrySnapshot:
    """A small home."""
    return RegistrySnapshot.build(
        entities=[
            {"entity_id": "light.living", "area_id": "living_room"},
            {"entity_id": "light.lamp", "device_id": "dev_lamp"},  # area via device
            {"entity_id": "light.office", "area_id": "office"},
            {"entity_id": "light.porch"},  # no area
            {"entity_id": "light.disabled", "area_id": "living_room", "disabled": True},
            {"entity_id": "light.hidden", "area_id": "living_room", "hidden": True},
            {
                "entity_id": "switch.config",
                "area_id": "living_room",
                "entity_category": "config",
            },
            {"entity_id": "lock.front", "device_id": "dev_lock"},
            {"entity_id": "climate.ac", "area_id": "living_room"},
            {"entity_id": "sensor.temp", "area_id": "living_room"},
            {"entity_id": "alarm_control_panel.home", "area_id": "living_room"},
            {"entity_id": "script.party", "area_id": "living_room"},
            {"entity_id": "media_player.tv", "device_id": "dev_tv", "area_id": "office"},
        ],
        devices=[
            {"id": "dev_lamp", "area_id": "guest_room"},
            {"id": "dev_lock", "area_id": "hall"},
            {"id": "dev_tv", "area_id": "living_room"},
        ],
        areas={"living_room": "Living room", "guest_room": "Guest room", "hall": "Hall"},
    )


def test_scope_from_dict_roundtrip() -> None:
    """A valid scope survives to_dict/from_dict; duplicates collapse; unknown keys drop."""
    scope = Scope.from_dict(
        {
            "capabilities": ["lights", "lights", "climate"],
            "all_areas": False,
            "areas": ["living_room"],
            "devices": ["d1"],
            "entities": ["light.porch"],
            "exclude": ["light.office"],
            "future_key": 1,
        }
    )
    assert scope.capabilities == ("lights", "climate")
    assert Scope.from_dict(scope.to_dict()) == scope
    assert scope.domains == {"light", "climate"}


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"capabilities": ["alarm"]},
        {"capabilities": "lights"},
        {"all_areas": "yes"},
        {"areas": [1]},
        {"entities": ["not_an_entity"]},
        {"exclude": [""]},
        {"devices": ["x" * 300]},
    ],
)
def test_scope_from_dict_rejects(data: object) -> None:
    """Malformed scopes are rejected."""
    with pytest.raises(ScopeError):
        Scope.from_dict(data)


def test_resolve_areas_and_device_area(snapshot: RegistrySnapshot) -> None:
    """Entities match by own area or, failing that, their device's area."""
    scope = Scope(capabilities=("lights",), areas=("living_room", "guest_room"))
    assert resolve_scope(scope, snapshot) == {
        "light.lamp": "guest_room",
        "light.living": "living_room",
    }


def test_resolve_entity_area_beats_device_area(snapshot: RegistrySnapshot) -> None:
    """media_player.tv has its own area (office) although its device is in living room."""
    scope = Scope(capabilities=("media",), areas=("living_room",))
    assert resolve_scope(scope, snapshot) == {}
    scope = Scope(capabilities=("media",), areas=("office",))
    assert resolve_scope(scope, snapshot) == {"media_player.tv": "office"}


def test_resolve_all_areas_skips_unassigned_and_filtered(
    snapshot: RegistrySnapshot,
) -> None:
    """all_areas covers every area; skips no-area, disabled, hidden, config entities."""
    scope = Scope(capabilities=tuple(ALL_CAPS), all_areas=True)
    assert set(resolve_scope(scope, snapshot)) == {
        "light.living",
        "light.lamp",
        "light.office",
        "lock.front",
        "climate.ac",
        "media_player.tv",
    }


def test_resolve_devices(snapshot: RegistrySnapshot) -> None:
    """A listed device brings in its entities."""
    scope = Scope(capabilities=("locks",), devices=("dev_lock",))
    assert resolve_scope(scope, snapshot) == {"lock.front": "hall"}


def test_resolve_explicit_entities(snapshot: RegistrySnapshot) -> None:
    """Explicit entities skip the expansion filters but not the capability filter."""
    scope = Scope(
        capabilities=("lights", "switches"),
        entities=(
            "light.porch",
            "light.disabled",
            "switch.config",
            "climate.ac",  # no climate capability
            "alarm_control_panel.home",  # never allowed
            "script.party",  # never allowed
            "light.does_not_exist",
        ),
    )
    assert resolve_scope(scope, snapshot) == {
        "light.disabled": "living_room",
        "light.porch": None,
        "switch.config": "living_room",
    }


def test_resolve_exclude_wins(snapshot: RegistrySnapshot) -> None:
    """Exclude removes entities however they were matched."""
    scope = Scope(
        capabilities=("lights",),
        all_areas=True,
        entities=("light.porch",),
        exclude=("light.porch", "light.office"),
    )
    assert set(resolve_scope(scope, snapshot)) == {"light.living", "light.lamp"}


def test_resolve_no_capabilities_is_empty(snapshot: RegistrySnapshot) -> None:
    """Without capabilities nothing is visible."""
    assert resolve_scope(Scope(all_areas=True, entities=("light.porch",)), snapshot) == {}


def test_presets_use_known_capabilities() -> None:
    """Presets match API.md and only reference real capabilities."""
    assert [p["id"] for p in PRESETS] == [
        "essentials",
        "door_and_lights",
        "house_sitter",
        "lights_only",
    ]
    for preset in PRESETS:
        Scope.from_dict({"capabilities": preset["capabilities"]})
        assert preset["all_areas"] is True
    essentials = next(p for p in PRESETS if p["id"] == "essentials")
    assert "locks" not in essentials["capabilities"]


# --- actions offered ---------------------------------------------------------------------


def test_entity_actions_default_to_table() -> None:
    """Without feature info every action in the table is offered."""
    for domain, actions in DOMAIN_ACTIONS.items():
        assert entity_actions(domain, {}) == list(actions)
    assert entity_actions("sensor", {}) == []


def test_entity_actions_feature_filtering() -> None:
    """Supported features hide actions the entity cannot do."""
    assert "set_brightness" not in entity_actions(
        "light", {"supported_color_modes": ["onoff"]}
    )
    assert "set_brightness" in entity_actions(
        "light", {"supported_color_modes": ["onoff", "brightness"]}
    )
    assert entity_actions("cover", {"supported_features": 1 | 2}) == ["open", "close"]
    assert "set_percentage" not in entity_actions("fan", {"supported_features": 0})
    media = entity_actions("media_player", {"supported_features": 4 | 16384})
    assert media == ["play_pause", "set_volume"]
    assert entity_actions("climate", {"supported_features": 1}) == [
        "set_temperature",
        "set_hvac_mode",
    ]
    assert entity_actions("lock", {"supported_features": 0}) == ["lock", "unlock"]


# --- GuestEntity -------------------------------------------------------------------------


def test_guest_attributes_whitelist() -> None:
    """Only whitelisted attributes pass, with conversions."""
    attrs = guest_attributes(
        "light",
        {
            "brightness": 128,
            "entity_picture": "/api/camera_proxy/x?token=secret",
            "access_token": "secret",
            "friendly_name": "Lamp",
            "icon": "mdi:lamp",
            "supported_color_modes": ["brightness"],
            "user_id": "abc",
        },
        "°C",
    )
    assert attrs == {"brightness_pct": 50, "icon": "mdi:lamp"}


def test_guest_attributes_climate_and_media() -> None:
    """Climate gets the unit; media volume becomes 0-100."""
    climate = guest_attributes(
        "climate",
        {
            "current_temperature": 77,
            "temperature": 74,
            "min_temp": 60,
            "max_temp": 86,
            "target_temp_step": 1,
            "hvac_modes": ["off", "cool"],
            "hvac_action": "cooling",
            "fan_modes": ["auto"],
        },
        "°F",
    )
    assert climate == {
        "current_temperature": 77,
        "temperature": 74,
        "min_temp": 60,
        "max_temp": 86,
        "target_temp_step": 1,
        "hvac_modes": ["off", "cool"],
        "hvac_action": "cooling",
        "temperature_unit": "°F",
    }
    media = guest_attributes(
        "media_player",
        {
            "volume_level": 0.333,
            "is_volume_muted": False,
            "media_title": "Song",
            "media_artist": "Band",
            "entity_picture": "/x",
            "source_list": ["a"],
        },
        "°C",
    )
    assert media == {
        "volume_level": 33,
        "is_volume_muted": False,
        "media_title": "Song",
        "media_artist": "Band",
    }


def test_guest_attributes_drop_bad_numbers() -> None:
    """NaN, booleans and strings are not passed as numbers."""
    attrs = guest_attributes(
        "climate",
        {"temperature": math.nan, "min_temp": True, "max_temp": "86"},
        None,
    )
    assert attrs == {}


def test_guest_entity_shape() -> None:
    """GuestEntity has the fields from API.md."""
    entity = guest_entity(
        "climate.ac",
        name="Living room AC",
        state="cool",
        attributes={"temperature": 74, "hvac_modes": ["off", "cool"]},
        area_id="living_room",
        temperature_unit="°F",
    )
    assert entity == {
        "entity_id": "climate.ac",
        "name": "Living room AC",
        "domain": "climate",
        "area_id": "living_room",
        "state": "cool",
        "attributes": {
            "temperature": 74,
            "hvac_modes": ["off", "cool"],
            "temperature_unit": "°F",
        },
        "actions": ["set_temperature", "set_hvac_mode", "turn_on", "turn_off"],
    }


# --- action translation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity_id", "action", "value", "service", "extra", "stored"),
    [
        ("light.a", "turn_on", None, ("light", "turn_on"), {}, None),
        ("light.a", "toggle", None, ("light", "toggle"), {}, None),
        ("light.a", "set_brightness", 40, ("light", "turn_on"), {"brightness_pct": 40}, 40),
        ("light.a", "set_brightness", 0, ("light", "turn_on"), {"brightness_pct": 0}, 0),
        ("switch.a", "turn_off", None, ("switch", "turn_off"), {}, None),
        ("input_boolean.a", "toggle", None, ("input_boolean", "toggle"), {}, None),
        ("fan.a", "set_percentage", 33.4, ("fan", "set_percentage"), {"percentage": 33}, 33),
        ("lock.a", "unlock", None, ("lock", "unlock"), {}, None),
        ("cover.a", "open", None, ("cover", "open_cover"), {}, None),
        ("cover.a", "stop", None, ("cover", "stop_cover"), {}, None),
        ("cover.a", "set_position", 100, ("cover", "set_cover_position"), {"position": 100}, 100),
        ("media_player.a", "play_pause", None, ("media_player", "media_play_pause"), {}, None),
        ("media_player.a", "next", None, ("media_player", "media_next_track"), {}, None),
        ("media_player.a", "previous", None, ("media_player", "media_previous_track"), {}, None),
        ("media_player.a", "set_volume", 25, ("media_player", "volume_set"), {"volume_level": 0.25}, 25),
        ("media_player.a", "mute", True, ("media_player", "volume_mute"), {"is_volume_muted": True}, True),
        ("scene.a", "activate", None, ("scene", "turn_on"), {}, None),
        ("climate.a", "turn_off", None, ("climate", "turn_off"), {}, None),
    ],
)
def test_translate_action(
    entity_id: str,
    action: str,
    value: object,
    service: tuple[str, str],
    extra: dict[str, object],
    stored: object,
) -> None:
    """Every guest action maps onto exactly one whitelisted service call."""
    call = translate_action(entity_id, {}, action, value)
    assert (call.domain, call.service) == service
    assert call.data == {"entity_id": entity_id, **extra}
    assert call.value == stored


CLIMATE = {"min_temp": 60, "max_temp": 86, "hvac_modes": ["off", "cool", "fan_only"]}


def test_translate_climate_clamps_temperature() -> None:
    """Temperatures are clamped to min_temp..max_temp."""
    assert translate_action("climate.a", CLIMATE, "set_temperature", 100).data[
        "temperature"
    ] == 86
    assert translate_action("climate.a", CLIMATE, "set_temperature", 10).data[
        "temperature"
    ] == 60
    assert translate_action("climate.a", CLIMATE, "set_temperature", 72.5).data[
        "temperature"
    ] == 72.5


def test_translate_hvac_mode_membership() -> None:
    """Only the entity's own hvac modes are accepted."""
    call = translate_action("climate.a", CLIMATE, "set_hvac_mode", "cool")
    assert call.data == {"entity_id": "climate.a", "hvac_mode": "cool"}
    for bad in ("heat", "", None, 1, ["cool"]):
        with pytest.raises(ActionError) as err:
            translate_action("climate.a", CLIMATE, "set_hvac_mode", bad)
        assert err.value.code == "bad_value"


@pytest.mark.parametrize(
    ("entity_id", "action", "value"),
    [
        ("light.a", "set_brightness", None),
        ("light.a", "set_brightness", 101),
        ("light.a", "set_brightness", -1),
        ("light.a", "set_brightness", "50"),
        ("light.a", "set_brightness", True),
        ("light.a", "set_brightness", math.nan),
        ("light.a", "set_brightness", math.inf),
        ("fan.a", "set_percentage", [50]),
        ("cover.a", "set_position", {"v": 1}),
        ("media_player.a", "set_volume", 150),
        ("media_player.a", "mute", "true"),
        ("media_player.a", "mute", 1),
        ("climate.a", "set_temperature", "72"),
        ("climate.a", "set_temperature", None),
    ],
)
def test_translate_bad_values(entity_id: str, action: str, value: object) -> None:
    """Invalid values are refused with bad_value."""
    with pytest.raises(ActionError) as err:
        translate_action(entity_id, CLIMATE, action, value)
    assert err.value.code == "bad_value"


@pytest.mark.parametrize(
    ("entity_id", "action", "attributes"),
    [
        ("light.a", "unlock", {}),
        ("light.a", "set_brightness", {"supported_color_modes": ["onoff"]}),
        ("lock.a", "open", {}),
        ("lock.a", "toggle", {}),
        ("scene.a", "turn_off", {}),
        ("cover.a", "set_position", {"supported_features": 3}),
        ("alarm_control_panel.a", "alarm_disarm", {}),
        ("script.a", "turn_on", {}),
        ("light.a", None, {}),
        ("light.a", ["turn_on"], {}),
    ],
)
def test_translate_not_allowed(
    entity_id: str, action: object, attributes: dict[str, object]
) -> None:
    """Actions outside the table (or the entity's features) are not allowed."""
    with pytest.raises(ActionError) as err:
        translate_action(entity_id, attributes, action, None)
    assert err.value.code == "not_allowed"
