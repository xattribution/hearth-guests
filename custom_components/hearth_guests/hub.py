"""Runtime object for Hearth Guests.

The hub owns the pass book, its storage, the expiry timer, the activity log, the shared APK
and the open guest event streams. HTTP views and websocket commands look it up through
`hass.data[DOMAIN]` on every request, so they keep working across config entry reloads.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Context, HassJob, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .apk import ApkStore
from .const import (
    ACTIVITY_MAX,
    API_VERSION,
    FEATURES,
    CONF_LAN_ONLY,
    DEFAULT_LAN_ONLY,
    DOMAIN,
    EVENT_ACTION,
    EVENT_PASS_CREATED,
    EVENT_PASS_EXPIRED,
    LAST_SEEN_SAVE_DELAY,
    PRESETS,
    SIGNAL_PASSES_CHANGED,
    SSE_QUEUE_SIZE,
    STORAGE_DIR,
    STORAGE_KEY,
    STORAGE_VERSION,
    URL_LANDING,
    VERSION,
)
from .passes import STATUS_ACTIVE, STATUS_PAUSED, UNSET, Pass, PassBook, format_datetime
from .ratelimit import BadTokenLimiter
from .scope import (
    ActionError,
    EntityRecord,
    RegistrySnapshot,
    Scope,
    guest_entity,
    resolve_scope,
    translate_action,
)

_LOGGER = logging.getLogger(__name__)

# How long a guest action waits for the service call before answering ok anyway.
SERVICE_CALL_TIMEOUT = 10
# last_seen is only persisted when it moved by more than this (avoids a write per request).
LAST_SEEN_RESOLUTION = 60

END_EXPIRED = "expired"
END_PAUSED = "paused"
END_DELETED = "deleted"


class GuestStream:
    """One open guest event stream. The events view drains `queue`.

    Items are (kind, data): ("state", GuestEntity), ("session", {...}), ("rescope", None),
    ("ended", reason). None closes the stream.
    """

    def __init__(self, pass_id: str) -> None:
        """Initialize."""
        self.pass_id = pass_id
        self.queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue(SSE_QUEUE_SIZE)

    @callback
    def push(self, kind: str, data: Any = None) -> None:
        """Queue an event. A client too slow to keep up is disconnected."""
        try:
            self.queue.put_nowait((kind, data))
        except asyncio.QueueFull:
            self.close()

    @callback
    def close(self) -> None:
        """Close the stream once queued events are sent (immediately if the queue is full)."""
        if self.queue.full():
            while not self.queue.empty():
                self.queue.get_nowait()
        self.queue.put_nowait(None)


class GuestHub:
    """Everything Hearth Guests keeps at runtime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True
        )
        self.book = PassBook()
        self.activity: deque[dict[str, Any]] = deque(maxlen=ACTIVITY_MAX)
        self.limiter = BadTokenLimiter()
        self.apk = ApkStore(hass.config.path(".storage", STORAGE_DIR))
        self.apk_meta: dict[str, Any] | None = None
        self.apk_lock = asyncio.Lock()
        self._streams: defaultdict[str, set[GuestStream]] = defaultdict(set)
        self._unsub_timer: CALLBACK_TYPE | None = None

    # --- lifecycle -------------------------------------------------------------------

    async def async_load(self) -> None:
        """Load passes and activity, announce expiries missed while HA was down."""
        data = await self.store.async_load() or {}
        self.book = PassBook.from_storage(data.get("passes", []))
        self.activity.extend(
            item for item in data.get("activity", []) if isinstance(item, dict)
        )
        self.apk_meta = await self.hass.async_add_executor_job(self.apk.load_meta)
        if self._process_deadlines(dt_util.utcnow()):
            await self._async_save()
        self._schedule_next()

    async def async_unload(self) -> None:
        """Stop the timer, close all streams and flush storage."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        for streams in self._streams.values():
            for stream in list(streams):
                stream.close()
        self._streams.clear()
        await self._async_save()

    def _data(self) -> dict[str, Any]:
        return {"passes": self.book.to_storage(), "activity": list(self.activity)}

    async def _async_save(self) -> None:
        await self.store.async_save(self._data())

    @callback
    def _schedule_save(self, delay: float = LAST_SEEN_SAVE_DELAY) -> None:
        self.store.async_delay_save(self._data, delay)

    # --- options / info --------------------------------------------------------------

    @property
    def lan_only(self) -> bool:
        """Whether guest endpoints are restricted to the local network."""
        return bool(self.entry.options.get(CONF_LAN_ONLY, DEFAULT_LAN_ONLY))

    @callback
    def guest_base_url(self) -> str | None:
        """HA's internal URL, used for links when the client sends no base URL."""
        try:
            return get_url(
                self.hass, allow_external=False, allow_cloud=False, prefer_external=False
            )
        except NoURLAvailableError:
            return None

    @callback
    def remote_base_url(self) -> str | None:
        """HA's external URL (Nabu Casa first), used for passes that work away from home."""
        try:
            return get_url(
                self.hass,
                allow_internal=False,
                allow_ip=False,
                prefer_cloud=True,
            )
        except NoURLAvailableError:
            return None

    @callback
    def info(self) -> dict[str, Any]:
        """Payload of hearth_guests/info."""
        return {
            "api_version": API_VERSION,
            "version": VERSION,
            "lan_only": self.lan_only,
            "guest_base_url": self.guest_base_url(),
            "remote_base_url": self.remote_base_url(),
            "features": list(FEATURES),
            "apk": dict(self.apk_meta) if self.apk_meta else None,
            "presets": [dict(preset) for preset in PRESETS],
            "active_count": len(self.book.active_passes(dt_util.utcnow())),
        }

    # --- registries and entities -----------------------------------------------------

    @callback
    def snapshot(self) -> RegistrySnapshot:
        """Snapshot the entity, device and area registries plus state-only entities."""
        records: dict[str, EntityRecord] = {}
        for entry in er.async_get(self.hass).entities.values():
            records[entry.entity_id] = EntityRecord(
                entity_id=entry.entity_id,
                area_id=entry.area_id,
                device_id=entry.device_id,
                disabled=entry.disabled_by is not None,
                hidden=entry.hidden_by is not None,
                entity_category=(
                    str(entry.entity_category) if entry.entity_category else None
                ),
            )
        for entity_id in self.hass.states.async_entity_ids():
            if entity_id not in records:
                records[entity_id] = EntityRecord(entity_id=entity_id)
        return RegistrySnapshot(
            entities=records,
            device_areas={
                device.id: device.area_id
                for device in dr.async_get(self.hass).devices.values()
            },
            area_names={
                area.id: area.name for area in ar.async_get(self.hass).async_list_areas()
            },
        )

    @callback
    def resolve(
        self, scope: Scope, snapshot: RegistrySnapshot | None = None
    ) -> dict[str, str | None]:
        """Resolve a scope against the live registries."""
        return resolve_scope(scope, snapshot or self.snapshot())

    @callback
    def guest_entity(self, entity_id: str, area_id: str | None) -> dict[str, Any] | None:
        """GuestEntity for one entity, or None when it has no state."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return guest_entity(
            entity_id,
            name=state.name,
            state=state.state,
            attributes=state.attributes,
            area_id=area_id,
            temperature_unit=self.hass.config.units.temperature_unit,
        )

    @callback
    def guest_entities(self, resolved: dict[str, str | None]) -> list[dict[str, Any]]:
        """GuestEntities for a resolved scope (entities without a state are skipped)."""
        entities = (self.guest_entity(eid, area) for eid, area in resolved.items())
        return [entity for entity in entities if entity is not None]

    @callback
    def session(self, pass_: Pass) -> dict[str, Any]:
        """Payload of GET /guest/session."""
        snapshot = self.snapshot()
        resolved = self.resolve(pass_.scope, snapshot)
        entities = self.guest_entities(resolved)
        area_ids = {entity["area_id"] for entity in entities if entity["area_id"]}
        areas = sorted(
            (
                {"area_id": area_id, "name": snapshot.area_names.get(area_id, area_id)}
                for area_id in area_ids
            ),
            key=lambda area: (area["name"].casefold(), area["area_id"]),
        )
        return {
            "api_version": API_VERSION,
            "pass": pass_.guest_info(),
            "server_time": dt_util.utcnow().replace(microsecond=0).isoformat(),
            "home_name": self.hass.config.location_name,
            "areas": areas,
            "entities": entities,
        }

    # --- owner view of passes --------------------------------------------------------

    def link(self, token: str, base_url: str | None) -> str | None:
        """The landing URL encoded in the QR."""
        base = (base_url or self.guest_base_url() or "").rstrip("/")
        if not base:
            return None
        return f"{base}{URL_LANDING}#t={token}"

    @callback
    def pass_payload(
        self,
        pass_: Pass,
        base_url: str | None = None,
        snapshot: RegistrySnapshot | None = None,
    ) -> dict[str, Any]:
        """The owner's view of a pass (API.md, "Pass object")."""
        now = dt_util.utcnow()
        return {
            "id": pass_.id,
            "name": pass_.name,
            "created_at": format_datetime(pass_.created_at),
            "expires_at": format_datetime(pass_.expires_at),
            "active": pass_.active,
            "status": pass_.status(now),
            "last_seen": format_datetime(pass_.last_seen),
            "scope": pass_.scope.to_dict(),
            "entity_count": len(self.resolve(pass_.scope, snapshot)),
            "token": pass_.token,
            "link": self.link(pass_.token, base_url),
            "remote": pass_.remote,
            "remote_link": self.link(pass_.token, self.remote_base_url())
            if pass_.remote and self.remote_base_url()
            else None,
        }

    # --- pass mutations (owner) ------------------------------------------------------

    async def async_create(
        self,
        *,
        name: Any,
        scope: Scope,
        expires_at: datetime | None,
        duration_minutes: int | None,
        remote: bool = False,
    ) -> Pass:
        """Create a pass and announce it."""
        pass_ = self.book.create(
            name=name,
            scope=scope,
            now=dt_util.utcnow(),
            expires_at=expires_at,
            duration_minutes=duration_minutes,
            remote=remote,
        )
        _LOGGER.info("Guest pass %s (%s) created", pass_.id, pass_.name)
        self.hass.bus.async_fire(
            EVENT_PASS_CREATED, {"pass_id": pass_.id, "name": pass_.name}
        )
        await self._async_changed()
        return pass_

    async def async_update(
        self,
        pass_id: str,
        *,
        name: Any = UNSET,
        scope: Any = UNSET,
        expires_at: Any = UNSET,
        active: Any = UNSET,
        remote: Any = UNSET,
    ) -> Pass:
        """Update a pass and tell its open streams."""
        pass_ = self.book.get(pass_id)
        before = (pass_.name, pass_.expires_at)
        old_scope = pass_.scope
        was_remote = pass_.remote
        self.book.update(
            pass_id,
            now=dt_util.utcnow(),
            name=name,
            scope=scope,
            expires_at=expires_at,
            active=active,
            remote=remote,
        )
        if was_remote and not pass_.remote:
            # Turning "away from home" off ends streams that came in from outside; they
            # reconnect and the LAN check decides.
            self._close_streams(pass_.id)
        self._notify_streams(pass_, before, scope_changed=pass_.scope != old_scope)
        await self._async_changed()
        return pass_

    async def async_extend(self, pass_id: str, minutes: int) -> Pass:
        """Extend (or shorten) a pass."""
        pass_ = self.book.get(pass_id)
        before = (pass_.name, pass_.expires_at)
        self.book.extend(pass_id, minutes, now=dt_util.utcnow())
        self._notify_streams(pass_, before, scope_changed=False)
        await self._async_changed()
        return pass_

    async def async_rotate(self, pass_id: str) -> Pass:
        """New token; streams opened with the old one are closed."""
        pass_ = self.book.rotate(pass_id)
        self._close_streams(pass_id)
        _LOGGER.info("Guest pass %s (%s) token rotated", pass_.id, pass_.name)
        await self._async_changed()
        return pass_

    async def async_delete(self, pass_id: str) -> None:
        """Delete a pass and end its streams."""
        pass_ = self.book.delete(pass_id)
        self._end_streams(pass_id, END_DELETED)
        _LOGGER.info("Guest pass %s (%s) deleted", pass_.id, pass_.name)
        await self._async_changed()

    async def _async_changed(self) -> None:
        """After any owner change: announce expiries, persist, re-arm the timer."""
        self._process_deadlines(dt_util.utcnow())
        await self._async_save()
        self._schedule_next()
        async_dispatcher_send(self.hass, SIGNAL_PASSES_CHANGED)

    # --- expiry timer ----------------------------------------------------------------

    @callback
    def _process_deadlines(self, now: datetime) -> bool:
        """Announce new expiries and purge old passes. Returns True if anything changed."""
        expired = self.book.due_expiries(now)
        for pass_ in expired:
            _LOGGER.info("Guest pass %s (%s) expired", pass_.id, pass_.name)
            self._end_streams(pass_.id, END_EXPIRED)
            self.hass.bus.async_fire(
                EVENT_PASS_EXPIRED, {"pass_id": pass_.id, "name": pass_.name}
            )
        purged = self.book.purge(now)
        for pass_ in purged:
            _LOGGER.debug("Purged guest pass %s (%s)", pass_.id, pass_.name)
            self._close_streams(pass_.id)
        return bool(expired or purged)

    @callback
    def _schedule_next(self) -> None:
        """(Re)arm the timer for the next expiry or purge."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        deadline = self.book.next_deadline(dt_util.utcnow())
        if deadline is not None:
            self._unsub_timer = async_track_point_in_utc_time(
                self.hass,
                HassJob(
                    self._handle_deadline,
                    f"{DOMAIN} pass expiry",
                    cancel_on_shutdown=True,
                ),
                deadline,
            )

    @callback
    def _handle_deadline(self, _now: datetime) -> None:
        self._unsub_timer = None
        if self._process_deadlines(dt_util.utcnow()):
            self._schedule_save(0)
            async_dispatcher_send(self.hass, SIGNAL_PASSES_CHANGED)
        self._schedule_next()

    # --- streams ---------------------------------------------------------------------

    @callback
    def open_stream(self, pass_: Pass) -> GuestStream:
        """Register a new event stream for a pass."""
        stream = GuestStream(pass_.id)
        self._streams[pass_.id].add(stream)
        return stream

    @callback
    def release_stream(self, stream: GuestStream) -> None:
        """Forget a stream once its response has finished."""
        streams = self._streams.get(stream.pass_id)
        if streams is not None:
            streams.discard(stream)
            if not streams:
                del self._streams[stream.pass_id]

    def stream_count(self, pass_id: str) -> int:
        """Open streams for a pass (used by tests and diagnostics)."""
        return len(self._streams.get(pass_id, ()))

    @callback
    def _end_streams(self, pass_id: str, reason: str) -> None:
        for stream in list(self._streams.get(pass_id, ())):
            stream.push("ended", {"reason": reason})
            stream.close()

    @callback
    def _close_streams(self, pass_id: str) -> None:
        for stream in list(self._streams.get(pass_id, ())):
            stream.close()

    @callback
    def _notify_streams(
        self, pass_: Pass, before: tuple[str, datetime | None], *, scope_changed: bool
    ) -> None:
        """Tell open streams about an owner change (expiry is handled by the timer)."""
        status = pass_.status(dt_util.utcnow())
        if status == STATUS_PAUSED:
            self._end_streams(pass_.id, END_PAUSED)
            return
        if status != STATUS_ACTIVE:
            return
        if scope_changed or before != (pass_.name, pass_.expires_at):
            for stream in self._streams.get(pass_.id, ()):
                stream.push("session", {"pass": pass_.guest_info()})
        if scope_changed:
            for stream in self._streams.get(pass_.id, ()):
                stream.push("rescope")

    # --- guests ----------------------------------------------------------------------

    @callback
    def touch(self, pass_: Pass) -> None:
        """Record that the guest used the pass."""
        now = dt_util.utcnow()
        last = pass_.last_seen
        self.book.touch(pass_, now)
        if last is None or (now - last).total_seconds() >= LAST_SEEN_RESOLUTION:
            self._schedule_save()

    async def async_action(
        self, pass_: Pass, entity_id: Any, action: Any, value: Any
    ) -> None:
        """Run one guest action. Raises ActionError, or HomeAssistantError on failure.

        The scope is resolved again here, at request time, and the pass is re-checked.
        """
        if pass_.status(dt_util.utcnow()) != STATUS_ACTIVE or (
            self.book.passes.get(pass_.id) is not pass_
        ):
            raise ActionError("not_allowed", "pass is not active")
        if not isinstance(entity_id, str):
            raise ActionError("bad_value", "entity_id must be a string")
        if entity_id not in self.resolve(pass_.scope):
            raise ActionError("not_allowed", "entity is outside the scope")
        state = self.hass.states.get(entity_id)
        if state is None:
            raise ActionError("not_allowed", "entity has no state")
        call = translate_action(entity_id, state.attributes, action, value)

        task = self.hass.async_create_task(
            self.hass.services.async_call(
                call.domain, call.service, call.data, blocking=True, context=Context()
            ),
            f"{DOMAIN} guest action",
        )
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT):
                await asyncio.shield(task)
        except TimeoutError:
            _LOGGER.debug("Guest action %s on %s still running", action, entity_id)
        except vol.Invalid as err:
            raise HomeAssistantError(str(err)) from err

        self._record_activity(pass_, entity_id, call.service, action, call.value)

    @callback
    def _record_activity(
        self, pass_: Pass, entity_id: str, service: str, action: str, value: Any
    ) -> None:
        _LOGGER.debug(
            "Guest %s (%s): %s %s via %s", pass_.id, pass_.name, action, entity_id, service
        )
        self.activity.appendleft(
            {
                "at": dt_util.utcnow().replace(microsecond=0).isoformat(),
                "pass_id": pass_.id,
                "pass_name": pass_.name,
                "entity_id": entity_id,
                "action": action,
                "value": value,
            }
        )
        self._schedule_save()
        self.hass.bus.async_fire(
            EVENT_ACTION,
            {
                "pass_id": pass_.id,
                "name": pass_.name,
                "entity_id": entity_id,
                "action": action,
                "value": value,
            },
        )

    @callback
    def activity_for(self, pass_id: str | None, limit: int) -> list[dict[str, Any]]:
        """Activity, newest first, optionally for one pass."""
        items = (a for a in self.activity if pass_id is None or a["pass_id"] == pass_id)
        out: list[dict[str, Any]] = []
        for item in items:
            if len(out) >= limit:
                break
            out.append(dict(item))
        return out

    @callback
    def active_guests(self) -> list[dict[str, Any]]:
        """Active passes for the sensor: names and expiry times."""
        return [
            {"name": p.name, "expires_at": format_datetime(p.expires_at)}
            for p in self.book.active_passes(dt_util.utcnow())
        ]


def get_hub(hass: HomeAssistant) -> GuestHub | None:
    """The loaded hub, or None when the integration is not set up."""
    return hass.data.get(DOMAIN)
