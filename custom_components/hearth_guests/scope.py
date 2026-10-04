"""Scope model, scope resolution and guest action translation.

Pure Python, no Home Assistant imports: everything here works on plain snapshots of the
entity / device / area registries and on plain state attribute mappings, so it can be unit
tested anywhere. See API.md, "Scope" and "GuestEntity".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import math
from typing import Any

from .const import CAPABILITIES, CAPABILITY_DOMAINS, DOMAIN_ACTIONS, SUPPORTED_DOMAINS

MAX_SCOPE_ITEMS = 2000
MAX_ID_LENGTH = 255

# Feature bits used to hide actions an entity cannot perform (mirrors HA's IntFlags).
_LIGHT_NON_DIMMABLE_MODES = frozenset({"onoff", "unknown"})
_FAN_SET_SPEED = 1
_CLIMATE_TARGET_TEMPERATURE = 1
_CLIMATE_TURN_OFF = 128
_CLIMATE_TURN_ON = 256
_COVER_OPEN = 1
_COVER_CLOSE = 2
_COVER_SET_POSITION = 4
_COVER_STOP = 8
_MEDIA_PAUSE = 1
_MEDIA_VOLUME_SET = 4
_MEDIA_VOLUME_MUTE = 8
_MEDIA_PREVIOUS = 16
_MEDIA_NEXT = 32
_MEDIA_TURN_ON = 128
_MEDIA_TURN_OFF = 256
_MEDIA_PLAY = 16384

# action -> required feature bits (any of), per domain. Actions not listed are always offered.
_ACTION_FEATURES: dict[str, dict[str, int]] = {
    "fan": {"set_percentage": _FAN_SET_SPEED},
    "climate": {
        "set_temperature": _CLIMATE_TARGET_TEMPERATURE,
        "turn_on": _CLIMATE_TURN_ON,
        "turn_off": _CLIMATE_TURN_OFF,
    },
    "cover": {
        "open": _COVER_OPEN,
        "close": _COVER_CLOSE,
        "stop": _COVER_STOP,
        "set_position": _COVER_SET_POSITION,
    },
    "media_player": {
        "turn_on": _MEDIA_TURN_ON,
        "turn_off": _MEDIA_TURN_OFF,
        "play_pause": _MEDIA_PAUSE | _MEDIA_PLAY,
        "next": _MEDIA_NEXT,
        "previous": _MEDIA_PREVIOUS,
        "set_volume": _MEDIA_VOLUME_SET,
        "mute": _MEDIA_VOLUME_MUTE,
    },
}


class ScopeError(ValueError):
    """Raised for a malformed scope."""


class ActionError(Exception):
    """Raised when a guest action is refused. `code` is the API error string."""

    def __init__(self, code: str, message: str = "") -> None:
        """Initialize with an API error code: not_allowed or bad_value."""
        super().__init__(message or code)
        self.code = code


def split_entity_id(entity_id: str) -> tuple[str, str]:
    """Split an entity id into (domain, object_id)."""
    domain, _, object_id = entity_id.partition(".")
    return domain, object_id


def _valid_entity_id(entity_id: str) -> bool:
    domain, object_id = split_entity_id(entity_id)
    return bool(domain) and bool(object_id) and len(entity_id) <= MAX_ID_LENGTH


def _str_list(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Read an optional list of non-empty strings, de-duplicated, order preserved."""
    raw = data.get(key, [])
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        raise ScopeError(f"{key} must be a list")
    if len(raw) > MAX_SCOPE_ITEMS:
        raise ScopeError(f"{key} has too many items")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item or len(item) > MAX_ID_LENGTH:
            raise ScopeError(f"{key} must contain non-empty strings")
        if item not in out:
            out.append(item)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Scope:
    """What a pass may see and control (API.md, "Scope")."""

    capabilities: tuple[str, ...] = ()
    all_areas: bool = False
    areas: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> Scope:
        """Validate and build a scope from a JSON-like dict. Unknown keys are ignored."""
        if not isinstance(data, Mapping):
            raise ScopeError("scope must be an object")
        capabilities = _str_list(data, "capabilities")
        unknown = [cap for cap in capabilities if cap not in CAPABILITIES]
        if unknown:
            raise ScopeError(f"unknown capabilities: {', '.join(unknown)}")
        all_areas = data.get("all_areas", False)
        if not isinstance(all_areas, bool):
            raise ScopeError("all_areas must be a boolean")
        entities = _str_list(data, "entities")
        exclude = _str_list(data, "exclude")
        for entity_id in (*entities, *exclude):
            if not _valid_entity_id(entity_id):
                raise ScopeError(f"invalid entity id: {entity_id}")
        return cls(
            capabilities=capabilities,
            all_areas=all_areas,
            areas=_str_list(data, "areas"),
            devices=_str_list(data, "devices"),
            entities=entities,
            exclude=exclude,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the API / storage form."""
        return {
            "capabilities": list(self.capabilities),
            "all_areas": self.all_areas,
            "areas": list(self.areas),
            "devices": list(self.devices),
            "entities": list(self.entities),
            "exclude": list(self.exclude),
        }

    @property
    def domains(self) -> frozenset[str]:
        """Domains enabled by this scope's capabilities."""
        return frozenset(
            domain for cap in self.capabilities for domain in CAPABILITY_DOMAINS[cap]
        )


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """The registry facts scope resolution needs about one entity."""

    entity_id: str
    area_id: str | None = None
    device_id: str | None = None
    disabled: bool = False
    hidden: bool = False
    entity_category: str | None = None

    @property
    def domain(self) -> str:
        """Entity domain."""
        return split_entity_id(self.entity_id)[0]


@dataclass(slots=True)
class RegistrySnapshot:
    """A plain snapshot of the entity, device and area registries.

    Entities that only exist in the state machine (no registry entry) are included as
    records without area or device, so they can still be listed explicitly.
    """

    entities: dict[str, EntityRecord] = field(default_factory=dict)
    device_areas: dict[str, str | None] = field(default_factory=dict)
    area_names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        entities: Iterable[EntityRecord | Mapping[str, Any]],
        devices: Iterable[Mapping[str, Any]] = (),
        areas: Mapping[str, str] | None = None,
    ) -> RegistrySnapshot:
        """Build from plain records: entities, devices {id, area_id}, areas {id: name}."""
        records: dict[str, EntityRecord] = {}
        for item in entities:
            record = item if isinstance(item, EntityRecord) else EntityRecord(**item)
            records[record.entity_id] = record
        return cls(
            entities=records,
            device_areas={d["id"]: d.get("area_id") for d in devices},
            area_names=dict(areas or {}),
        )

    def area_of(self, record: EntityRecord) -> str | None:
        """An entity's area: its own, or else its device's."""
        if record.area_id:
            return record.area_id
        if record.device_id:
            return self.device_areas.get(record.device_id)
        return None


def resolve_scope(scope: Scope, snapshot: RegistrySnapshot) -> dict[str, str | None]:
    """Resolve a scope to {entity_id: area_id}, sorted by entity id (API.md rules 1-4)."""
    domains = scope.domains
    explicit = set(scope.entities)
    devices = set(scope.devices)
    areas = set(scope.areas)
    excluded = set(scope.exclude)
    resolved: dict[str, str | None] = {}

    for record in snapshot.entities.values():
        entity_id = record.entity_id
        if entity_id in excluded or record.domain not in domains:
            continue  # Rules 3 and 4.
        area_id = snapshot.area_of(record)
        if entity_id in explicit:
            resolved[entity_id] = area_id
            continue
        # Rule 2: expansion skips disabled, hidden, categorized and unsupported entities.
        if (
            record.disabled
            or record.hidden
            or record.entity_category is not None
            or record.domain not in SUPPORTED_DOMAINS
        ):
            continue
        if (record.device_id is not None and record.device_id in devices) or (
            area_id is not None and (scope.all_areas or area_id in areas)
        ):
            resolved[entity_id] = area_id

    return dict(sorted(resolved.items()))


def entity_actions(domain: str, attributes: Mapping[str, Any]) -> list[str]:
    """Actions offered for an entity, hiding ones its supported features rule out."""
    actions = list(DOMAIN_ACTIONS.get(domain, ()))
    if domain == "light":
        modes = attributes.get("supported_color_modes")
        if (
            isinstance(modes, list | tuple | set | frozenset)
            and modes
            and all(str(mode) in _LIGHT_NON_DIMMABLE_MODES for mode in modes)
        ):
            actions.remove("set_brightness")
        return actions
    features = attributes.get("supported_features")
    required = _ACTION_FEATURES.get(domain)
    if not required or not isinstance(features, int) or isinstance(features, bool):
        return actions
    return [a for a in actions if a not in required or features & required[a]]


# --- GuestEntity -------------------------------------------------------------------------

_PASSTHROUGH_NUMBERS = (
    "current_temperature",
    "temperature",
    "target_temp_low",
    "target_temp_high",
    "min_temp",
    "max_temp",
    "target_temp_step",
    "percentage",
    "current_position",
)
_PASSTHROUGH_STRINGS = ("hvac_action", "media_title", "media_artist", "icon")


def _finite(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def guest_attributes(
    domain: str, attributes: Mapping[str, Any], temperature_unit: str | None
) -> dict[str, Any]:
    """Whitelist and convert state attributes for guests (API.md, "GuestEntity")."""
    out: dict[str, Any] = {}
    if domain == "light":
        brightness = _finite(attributes.get("brightness"))
        if brightness is not None:
            out["brightness_pct"] = max(0, min(100, round(brightness / 255 * 100)))
    for key in _PASSTHROUGH_NUMBERS:
        value = _finite(attributes.get(key))
        if value is not None:
            out[key] = value
    for key in _PASSTHROUGH_STRINGS:
        value = attributes.get(key)
        if value is not None:
            out[key] = str(value)
    modes = attributes.get("hvac_modes")
    if isinstance(modes, list | tuple):
        out["hvac_modes"] = [str(mode) for mode in modes]
    volume = _finite(attributes.get("volume_level"))
    if volume is not None:
        out["volume_level"] = max(0, min(100, round(volume * 100)))
    muted = attributes.get("is_volume_muted")
    if isinstance(muted, bool):
        out["is_volume_muted"] = muted
    if domain == "climate" and temperature_unit:
        out["temperature_unit"] = temperature_unit
    return out


def guest_entity(
    entity_id: str,
    *,
    name: str,
    state: str,
    attributes: Mapping[str, Any],
    area_id: str | None,
    temperature_unit: str | None = None,
) -> dict[str, Any]:
    """Build the GuestEntity dict for one entity."""
    domain = split_entity_id(entity_id)[0]
    return {
        "entity_id": entity_id,
        "name": name,
        "domain": domain,
        "area_id": area_id,
        "state": state,
        "attributes": guest_attributes(domain, attributes, temperature_unit),
        "actions": entity_actions(domain, attributes),
    }


# --- Action translation ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServiceCall:
    """A whitelisted Home Assistant service call produced from a guest action."""

    domain: str
    service: str
    data: dict[str, Any]
    value: Any = None  # The validated value, for activity and bus events.


def _number(value: Any) -> float:
    number = _finite(value)
    if number is None:
        raise ActionError("bad_value", "value must be a number")
    return float(number)


def _percent(value: Any) -> int:
    number = _number(value)
    if not 0 <= number <= 100:
        raise ActionError("bad_value", "value must be between 0 and 100")
    return round(number)


# (domain, action) -> service name for actions without a value.
_SIMPLE_SERVICES: dict[tuple[str, str], str] = {
    **{
        (domain, action): action
        for domain in ("light", "switch", "input_boolean", "fan")
        for action in ("turn_on", "turn_off", "toggle")
    },
    ("climate", "turn_on"): "turn_on",
    ("climate", "turn_off"): "turn_off",
    ("lock", "lock"): "lock",
    ("lock", "unlock"): "unlock",
    ("cover", "open"): "open_cover",
    ("cover", "close"): "close_cover",
    ("cover", "stop"): "stop_cover",
    ("media_player", "turn_on"): "turn_on",
    ("media_player", "turn_off"): "turn_off",
    ("media_player", "play_pause"): "media_play_pause",
    ("media_player", "next"): "media_next_track",
    ("media_player", "previous"): "media_previous_track",
    ("scene", "activate"): "turn_on",
}


def translate_action(
    entity_id: str, attributes: Mapping[str, Any], action: Any, value: Any = None
) -> ServiceCall:
    """Validate a guest action and translate it into a service call.

    The caller has already checked that the entity is in the pass's resolved scope.
    Raises ActionError("not_allowed") for an action the entity does not offer and
    ActionError("bad_value") for a missing or invalid value.
    """
    domain = split_entity_id(entity_id)[0]
    if not isinstance(action, str) or action not in entity_actions(domain, attributes):
        raise ActionError("not_allowed", "action not allowed for this entity")
    target = {"entity_id": entity_id}

    if (domain, action) in _SIMPLE_SERVICES:
        return ServiceCall(domain, _SIMPLE_SERVICES[(domain, action)], target)

    if domain == "light" and action == "set_brightness":
        pct = _percent(value)
        return ServiceCall("light", "turn_on", {**target, "brightness_pct": pct}, pct)
    if domain == "fan" and action == "set_percentage":
        pct = _percent(value)
        return ServiceCall("fan", "set_percentage", {**target, "percentage": pct}, pct)
    if domain == "cover" and action == "set_position":
        pct = _percent(value)
        return ServiceCall(
            "cover", "set_cover_position", {**target, "position": pct}, pct
        )
    if domain == "media_player" and action == "set_volume":
        pct = _percent(value)
        return ServiceCall(
            "media_player", "volume_set", {**target, "volume_level": pct / 100}, pct
        )
    if domain == "media_player" and action == "mute":
        if not isinstance(value, bool):
            raise ActionError("bad_value", "value must be a boolean")
        return ServiceCall(
            "media_player", "volume_mute", {**target, "is_volume_muted": value}, value
        )
    if domain == "climate" and action == "set_temperature":
        temp = _number(value)
        low = _finite(attributes.get("min_temp"))
        high = _finite(attributes.get("max_temp"))
        if low is not None:
            temp = max(temp, float(low))
        if high is not None:
            temp = min(temp, float(high))
        return ServiceCall(
            "climate", "set_temperature", {**target, "temperature": temp}, temp
        )
    if domain == "climate" and action == "set_hvac_mode":
        modes = attributes.get("hvac_modes")
        allowed = [str(m) for m in modes] if isinstance(modes, list | tuple) else []
        if not isinstance(value, str) or value not in allowed:
            raise ActionError("bad_value", "unsupported hvac mode")
        return ServiceCall(
            "climate", "set_hvac_mode", {**target, "hvac_mode": value}, value
        )

    raise ActionError("not_allowed", "action not allowed for this entity")
