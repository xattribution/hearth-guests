"""Guest passes: model, lifecycle and token lookup.

Pure Python, no Home Assistant imports. All times are timezone-aware UTC datetimes; callers
pass `now` in explicitly so the logic is deterministic and testable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hmac
import secrets
from typing import Any, Final

from .const import MAX_NAME_LENGTH, MAX_TOKEN_LENGTH, PURGE_AFTER
from .scope import Scope

STATUS_ACTIVE: Final = "active"
STATUS_PAUSED: Final = "paused"
STATUS_EXPIRED: Final = "expired"

# Sentinel for "argument not given" in PassBook.update (None means permanent there).
UNSET: Final[Any] = object()

_ID_ATTEMPTS = 20


class PassError(ValueError):
    """Raised for invalid pass input."""


class PassNotFound(KeyError):
    """Raised when a pass id does not exist."""


def utc_seconds(value: datetime) -> datetime:
    """Normalize to UTC with whole seconds (keeps the API timestamps tidy)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO 8601 string (naive means UTC). None stays None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_seconds(value)
    if not isinstance(value, str):
        raise PassError("expected an ISO 8601 date-time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise PassError(f"invalid date-time: {value}") from err
    return utc_seconds(parsed)


def format_datetime(value: datetime | None) -> str | None:
    """Serialize a datetime as ISO 8601 (or None)."""
    return value.isoformat() if value is not None else None


def clean_name(name: Any) -> str:
    """Validate and normalize a guest name."""
    if not isinstance(name, str):
        raise PassError("name must be a string")
    name = " ".join(name.split())
    if not name:
        raise PassError("name must not be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise PassError(f"name must be at most {MAX_NAME_LENGTH} characters")
    return name


@dataclass(slots=True)
class Pass:
    """One guest pass."""

    id: str
    name: str
    token: str
    created_at: datetime
    expires_at: datetime | None
    scope: Scope
    active: bool = True
    # Usable away from the home network (e.g. through Nabu Casa) even when lan_only is on.
    remote: bool = False
    last_seen: datetime | None = None
    # True once the expiry for the current `expires_at` has been announced (event fired,
    # streams closed), so it is announced exactly once, also across restarts.
    expiry_notified: bool = False

    @property
    def permanent(self) -> bool:
        """True when the pass has no end date."""
        return self.expires_at is None

    def is_expired(self, now: datetime) -> bool:
        """True when the expiry time has passed."""
        return self.expires_at is not None and self.expires_at <= now

    def status(self, now: datetime) -> str:
        """active, paused or expired. Expiry wins over paused."""
        if self.is_expired(now):
            return STATUS_EXPIRED
        if not self.active:
            return STATUS_PAUSED
        return STATUS_ACTIVE

    def guest_info(self) -> dict[str, Any]:
        """The `pass` object guests see (session and session events)."""
        return {
            "name": self.name,
            "expires_at": format_datetime(self.expires_at),
            "permanent": self.permanent,
        }

    def to_storage(self) -> dict[str, Any]:
        """Serialize for the HA store."""
        return {
            "id": self.id,
            "name": self.name,
            "token": self.token,
            "created_at": format_datetime(self.created_at),
            "expires_at": format_datetime(self.expires_at),
            "active": self.active,
            "remote": self.remote,
            "last_seen": format_datetime(self.last_seen),
            "expiry_notified": self.expiry_notified,
            "scope": self.scope.to_dict(),
        }

    @classmethod
    def from_storage(cls, data: Mapping[str, Any]) -> Pass:
        """Deserialize from the HA store."""
        created_at = parse_datetime(data["created_at"])
        assert created_at is not None
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            token=str(data["token"]),
            created_at=created_at,
            expires_at=parse_datetime(data.get("expires_at")),
            scope=Scope.from_dict(data.get("scope", {})),
            active=bool(data.get("active", True)),
            remote=bool(data.get("remote", False)),
            last_seen=parse_datetime(data.get("last_seen")),
            expiry_notified=bool(data.get("expiry_notified", False)),
        )


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _new_id() -> str:
    return secrets.token_hex(4)


@dataclass
class PassBook:
    """All passes, with the lifecycle operations from API.md."""

    passes: dict[str, Pass] = field(default_factory=dict)
    token_factory: Callable[[], str] = _new_token
    id_factory: Callable[[], str] = _new_id

    # --- storage ---------------------------------------------------------------------

    @classmethod
    def from_storage(cls, items: Iterable[Mapping[str, Any]], **kwargs: Any) -> PassBook:
        """Load passes from stored dicts, skipping corrupt entries."""
        book = cls(**kwargs)
        for item in items:
            try:
                pass_ = Pass.from_storage(item)
            except (KeyError, TypeError, ValueError):
                continue
            book.passes[pass_.id] = pass_
        return book

    def to_storage(self) -> list[dict[str, Any]]:
        """Serialize all passes for the HA store."""
        return [p.to_storage() for p in self.passes.values()]

    # --- queries ---------------------------------------------------------------------

    def get(self, pass_id: str) -> Pass:
        """Return a pass or raise PassNotFound."""
        try:
            return self.passes[pass_id]
        except KeyError:
            raise PassNotFound(pass_id) from None

    def newest_first(self) -> list[Pass]:
        """All passes, newest first."""
        # Stable sort over reversed insertion order: ties (same second) stay newest first.
        return sorted(
            reversed(list(self.passes.values())),
            key=lambda p: p.created_at,
            reverse=True,
        )

    def active_passes(self, now: datetime) -> list[Pass]:
        """Passes whose status is active, soonest expiry first, permanent last."""
        active = [p for p in self.passes.values() if p.status(now) == STATUS_ACTIVE]
        return sorted(
            active,
            key=lambda p: (p.expires_at is None, p.expires_at or now, p.name),
        )

    def has_remote(self, now: datetime) -> bool:
        """True when any usable pass may be used away from home."""
        return any(p.remote and p.status(now) == STATUS_ACTIVE for p in self.passes.values())

    def find_by_token(self, token: str | None) -> Pass | None:
        """Find the pass holding `token`, in constant time per pass.

        Every pass is compared with hmac.compare_digest; the loop never exits early, so
        timing does not reveal which (or whether a) pass matched.
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        candidate = token.encode()
        found: Pass | None = None
        for pass_ in self.passes.values():
            if hmac.compare_digest(pass_.token.encode(), candidate):
                found = pass_
        return found

    # --- mutations -------------------------------------------------------------------

    def _unique(self, factory: Callable[[], str], taken: Iterable[str]) -> str:
        used = set(taken)
        for _ in range(_ID_ATTEMPTS):
            value = factory()
            if value not in used:
                return value
        raise RuntimeError("could not generate a unique value")

    def create(
        self,
        *,
        name: Any,
        scope: Scope,
        now: datetime,
        expires_at: datetime | None = None,
        duration_minutes: int | None = None,
        remote: bool = False,
    ) -> Pass:
        """Create a pass. Give at most one of expires_at / duration_minutes."""
        now = utc_seconds(now)
        if expires_at is not None and duration_minutes is not None:
            raise PassError("give either expires_at or duration_minutes, not both")
        if duration_minutes is not None:
            if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int):
                raise PassError("duration_minutes must be an integer")
            if duration_minutes <= 0:
                raise PassError("duration_minutes must be positive")
            expires_at = now + timedelta(minutes=duration_minutes)
        elif expires_at is not None:
            expires_at = utc_seconds(expires_at)
            if expires_at <= now:
                raise PassError("expires_at must be in the future")
        if not isinstance(remote, bool):
            raise PassError("remote must be a boolean")
        pass_ = Pass(
            id=self._unique(self.id_factory, self.passes),
            name=clean_name(name),
            token=self._unique(self.token_factory, (p.token for p in self.passes.values())),
            created_at=now,
            expires_at=expires_at,
            scope=scope,
            remote=remote,
        )
        self.passes[pass_.id] = pass_
        return pass_

    def _set_expiry(self, pass_: Pass, expires_at: datetime | None, now: datetime) -> None:
        """Change the expiry; announce it again only if the pass newly becomes expired."""
        was_expired = pass_.is_expired(now)
        pass_.expires_at = utc_seconds(expires_at) if expires_at is not None else None
        pass_.expiry_notified = (
            pass_.expiry_notified and was_expired and pass_.is_expired(now)
        )

    def update(
        self,
        pass_id: str,
        *,
        now: datetime,
        name: Any = UNSET,
        scope: Scope | Any = UNSET,
        expires_at: datetime | None | Any = UNSET,
        active: bool | Any = UNSET,
        remote: bool | Any = UNSET,
    ) -> Pass:
        """Update fields that are given. expires_at=None makes the pass permanent."""
        now = utc_seconds(now)
        pass_ = self.get(pass_id)
        new_name = clean_name(name) if name is not UNSET else pass_.name
        if active is not UNSET and not isinstance(active, bool):
            raise PassError("active must be a boolean")
        if remote is not UNSET and not isinstance(remote, bool):
            raise PassError("remote must be a boolean")
        if scope is not UNSET and not isinstance(scope, Scope):
            raise PassError("invalid scope")
        # All validated; apply.
        pass_.name = new_name
        if scope is not UNSET:
            pass_.scope = scope
        if active is not UNSET:
            pass_.active = active
        if remote is not UNSET:
            pass_.remote = remote
        if expires_at is not UNSET:
            self._set_expiry(pass_, expires_at, now)
        return pass_

    def extend(self, pass_id: str, minutes: int, *, now: datetime) -> Pass:
        """Add minutes to max(now, expires_at). Permanent passes stay permanent."""
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise PassError("minutes must be an integer")
        now = utc_seconds(now)
        pass_ = self.get(pass_id)
        if pass_.expires_at is None:
            return pass_
        new_expiry = max(now, pass_.expires_at) + timedelta(minutes=minutes)
        self._set_expiry(pass_, max(new_expiry, now), now)
        return pass_

    def rotate(self, pass_id: str) -> Pass:
        """Give the pass a new token; the old one stops working."""
        pass_ = self.get(pass_id)
        pass_.token = self._unique(
            self.token_factory, (p.token for p in self.passes.values())
        )
        return pass_

    def delete(self, pass_id: str) -> Pass:
        """Remove a pass and return it."""
        pass_ = self.get(pass_id)
        del self.passes[pass_id]
        return pass_

    def touch(self, pass_: Pass, now: datetime) -> None:
        """Record guest activity."""
        pass_.last_seen = utc_seconds(now)

    # --- expiry ----------------------------------------------------------------------

    def due_expiries(self, now: datetime) -> list[Pass]:
        """Mark and return passes that expired and have not been announced yet."""
        due = [
            p for p in self.passes.values() if p.is_expired(now) and not p.expiry_notified
        ]
        for pass_ in due:
            pass_.expiry_notified = True
        return due

    def purge(self, now: datetime) -> list[Pass]:
        """Remove passes that expired more than PURGE_AFTER ago."""
        stale = [
            p
            for p in self.passes.values()
            if p.expires_at is not None and p.expires_at + PURGE_AFTER <= now
        ]
        for pass_ in stale:
            del self.passes[pass_.id]
        return stale

    def next_deadline(self, now: datetime) -> datetime | None:
        """The next time something needs doing: an expiry to announce, or a purge."""
        deadlines: list[datetime] = []
        for pass_ in self.passes.values():
            if pass_.expires_at is None:
                continue
            if not pass_.expiry_notified:
                deadlines.append(pass_.expires_at)
            else:
                deadlines.append(pass_.expires_at + PURGE_AFTER)
        return min(deadlines) if deadlines else None
