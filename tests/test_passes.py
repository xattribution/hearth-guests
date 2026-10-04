"""Tests for the pass model and PassBook lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import itertools

import pytest

from hearth_guests.passes import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_PAUSED,
    Pass,
    PassBook,
    PassError,
    PassNotFound,
    parse_datetime,
)
from hearth_guests.scope import Scope

NOW = datetime(2026, 10, 4, 20, 0, 0, tzinfo=UTC)
SCOPE = Scope(capabilities=("lights",), all_areas=True)


def _book() -> PassBook:
    return PassBook()


def test_create_defaults() -> None:
    """A new pass is active, permanent, with a strong token and an 8-hex-digit id."""
    book = _book()
    pass_ = book.create(name="  Sam  ", scope=SCOPE, now=NOW)
    assert pass_.name == "Sam"
    assert len(pass_.id) == 8 and int(pass_.id, 16) >= 0
    assert len(pass_.token) >= 43  # token_urlsafe(32)
    assert pass_.permanent and pass_.expires_at is None
    assert pass_.status(NOW) == STATUS_ACTIVE
    assert pass_.created_at == NOW
    assert pass_.last_seen is None


def test_create_with_duration_and_expiry() -> None:
    """duration_minutes and expires_at both work; not together, not in the past."""
    book = _book()
    timed = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=90)
    assert timed.expires_at == NOW + timedelta(minutes=90)
    dated = book.create(
        name="B", scope=SCOPE, now=NOW, expires_at=NOW + timedelta(days=2)
    )
    assert dated.expires_at == NOW + timedelta(days=2)
    with pytest.raises(PassError):
        book.create(
            name="C",
            scope=SCOPE,
            now=NOW,
            expires_at=NOW + timedelta(days=1),
            duration_minutes=5,
        )
    with pytest.raises(PassError):
        book.create(name="C", scope=SCOPE, now=NOW, expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(PassError):
        book.create(name="C", scope=SCOPE, now=NOW, duration_minutes=0)


@pytest.mark.parametrize("name", ["", "   ", None, 5, "x" * 65])
def test_create_rejects_bad_names(name: object) -> None:
    """Names must be 1-64 visible characters."""
    with pytest.raises(PassError):
        _book().create(name=name, scope=SCOPE, now=NOW)


def test_id_and_token_collisions_retry() -> None:
    """Colliding ids and tokens are regenerated."""
    ids: Iterator[str] = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    tokens: Iterator[str] = iter(["t1", "t1", "t2"])
    book = PassBook(id_factory=lambda: next(ids), token_factory=lambda: next(tokens))
    first = book.create(name="A", scope=SCOPE, now=NOW)
    second = book.create(name="B", scope=SCOPE, now=NOW)
    assert (first.id, second.id) == ("aaaaaaaa", "bbbbbbbb")
    assert (first.token, second.token) == ("t1", "t2")


def test_find_by_token() -> None:
    """Lookup finds exactly the matching pass and rejects junk."""
    book = _book()
    a = book.create(name="A", scope=SCOPE, now=NOW)
    b = book.create(name="B", scope=SCOPE, now=NOW)
    assert book.find_by_token(a.token) is a
    assert book.find_by_token(b.token) is b
    assert book.find_by_token(a.token[:-1]) is None
    assert book.find_by_token("") is None
    assert book.find_by_token(None) is None
    assert book.find_by_token("x" * 10_000) is None


def test_status_and_pause() -> None:
    """Status is expired over paused over active."""
    book = _book()
    pass_ = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=60)
    book.update(pass_.id, now=NOW, active=False)
    assert pass_.status(NOW) == STATUS_PAUSED
    assert pass_.status(NOW + timedelta(hours=1)) == STATUS_EXPIRED
    book.update(pass_.id, now=NOW, active=True)
    assert pass_.status(NOW) == STATUS_ACTIVE


def test_update_partial() -> None:
    """Only given fields change; expires_at=None makes the pass permanent."""
    book = _book()
    pass_ = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=60)
    new_scope = Scope(capabilities=("locks",), areas=("hall",))
    book.update(pass_.id, now=NOW, name="Bea", scope=new_scope)
    assert (pass_.name, pass_.scope) == ("Bea", new_scope)
    assert pass_.expires_at == NOW + timedelta(hours=1)
    book.update(pass_.id, now=NOW, expires_at=None)
    assert pass_.permanent
    with pytest.raises(PassError):
        book.update(pass_.id, now=NOW, name="")
    with pytest.raises(PassError):
        book.update(pass_.id, now=NOW, active="yes")
    assert pass_.name == "Bea"
    with pytest.raises(PassNotFound):
        book.update("nope", now=NOW, name="X")


def test_extend() -> None:
    """extend adds to max(now, expires_at); permanent stays permanent."""
    book = _book()
    pass_ = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=60)
    book.extend(pass_.id, 30, now=NOW)
    assert pass_.expires_at == NOW + timedelta(minutes=90)
    # Already expired: extend counts from now.
    later = NOW + timedelta(hours=5)
    book.extend(pass_.id, 60, now=later)
    assert pass_.expires_at == later + timedelta(hours=1)
    # Negative into the past: expires now.
    book.extend(pass_.id, -10_000, now=later)
    assert pass_.expires_at == later
    assert pass_.status(later) == STATUS_EXPIRED
    permanent = book.create(name="P", scope=SCOPE, now=NOW)
    book.extend(permanent.id, 60, now=NOW)
    assert permanent.permanent
    with pytest.raises(PassError):
        book.extend(pass_.id, 1.5, now=NOW)  # type: ignore[arg-type]


def test_rotate_and_delete() -> None:
    """Rotation replaces the token; delete removes the pass."""
    book = _book()
    pass_ = book.create(name="A", scope=SCOPE, now=NOW)
    old = pass_.token
    book.rotate(pass_.id)
    assert pass_.token != old
    assert book.find_by_token(old) is None
    assert book.find_by_token(pass_.token) is pass_
    book.delete(pass_.id)
    assert book.find_by_token(pass_.token) is None
    with pytest.raises(PassNotFound):
        book.delete(pass_.id)


def test_newest_first_and_active_passes() -> None:
    """Listing is newest first; active passes exclude paused and expired."""
    book = _book()
    a = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=10)
    b = book.create(name="B", scope=SCOPE, now=NOW + timedelta(minutes=1))
    c = book.create(
        name="C", scope=SCOPE, now=NOW + timedelta(minutes=2), duration_minutes=60
    )
    book.update(c.id, now=NOW, active=False)
    assert [p.id for p in book.newest_first()] == [c.id, b.id, a.id]
    assert book.active_passes(NOW + timedelta(minutes=5)) == [a, b]
    assert book.active_passes(NOW + timedelta(minutes=20)) == [b]


def test_expiry_announced_once_and_purge() -> None:
    """Expiries are announced once; re-expiry after extension is announced again."""
    book = _book()
    pass_ = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=60)
    assert book.next_deadline(NOW) == NOW + timedelta(hours=1)
    assert book.due_expiries(NOW) == []
    at = NOW + timedelta(hours=1)
    assert book.due_expiries(at) == [pass_]
    assert book.due_expiries(at) == []
    # Next deadline is now the purge.
    assert book.next_deadline(at) == at + timedelta(days=30)
    # Renewing and letting it expire again announces again.
    book.extend(pass_.id, 30, now=at)
    assert not pass_.expiry_notified
    assert book.due_expiries(at + timedelta(minutes=30)) == [pass_]
    # Changing the expiry of an already expired pass to another past time: no new event.
    book.update(pass_.id, now=at + timedelta(hours=1), expires_at=at)
    assert book.due_expiries(at + timedelta(hours=1)) == []
    # Purge after 30 days.
    assert book.purge(at + timedelta(days=29)) == []
    assert book.purge(at + timedelta(days=30)) == [pass_]
    assert book.passes == {}
    assert book.next_deadline(NOW) is None


def test_shortening_into_past_is_announced() -> None:
    """Ending a pass early (update to a past time) announces the expiry."""
    book = _book()
    pass_ = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=60)
    book.update(pass_.id, now=NOW, expires_at=NOW - timedelta(minutes=1))
    assert book.due_expiries(NOW) == [pass_]


def test_storage_roundtrip() -> None:
    """Passes survive to_storage/from_storage; corrupt entries are skipped."""
    book = _book()
    a = book.create(name="A", scope=SCOPE, now=NOW, duration_minutes=60)
    book.touch(a, NOW + timedelta(seconds=30, microseconds=5))
    b = book.create(name="B", scope=Scope(capabilities=("locks",)), now=NOW)
    book.update(b.id, now=NOW, active=False)
    stored = book.to_storage()
    loaded = PassBook.from_storage([*stored, {"id": "broken"}, {"garbage": True}])
    assert set(loaded.passes) == {a.id, b.id}
    for original in (a, b):
        copy = loaded.passes[original.id]
        assert copy == original
    assert loaded.passes[a.id].last_seen == NOW + timedelta(seconds=30)
    assert stored[0]["created_at"] == "2026-10-04T20:00:00+00:00"


def test_guest_info() -> None:
    """The guest-facing pass object."""
    pass_ = Pass(
        id="a1b2c3d4",
        name="Sam",
        token="t",
        created_at=NOW,
        expires_at=NOW + timedelta(days=2),
        scope=SCOPE,
    )
    assert pass_.guest_info() == {
        "name": "Sam",
        "expires_at": "2026-10-06T20:00:00+00:00",
        "permanent": False,
    }


def test_parse_datetime() -> None:
    """ISO strings with offsets, Z, or naive (UTC) are accepted."""
    assert parse_datetime("2026-10-06T18:00:00+00:00") == datetime(
        2026, 10, 6, 18, tzinfo=UTC
    )
    assert parse_datetime("2026-10-06T18:00:00Z") == datetime(2026, 10, 6, 18, tzinfo=UTC)
    assert parse_datetime("2026-10-06T20:00:00+02:00") == datetime(
        2026, 10, 6, 18, tzinfo=UTC
    )
    assert parse_datetime("2026-10-06T18:00:00") == datetime(2026, 10, 6, 18, tzinfo=UTC)
    assert parse_datetime(None) is None
    for bad in ("tomorrow", 5, ""):
        with pytest.raises(PassError):
            parse_datetime(bad)


def test_tokens_are_unique_at_scale() -> None:
    """Sanity: real tokens and ids do not collide in a big book."""
    book = _book()
    for i in itertools.islice(itertools.count(), 300):
        book.create(name=f"G{i}", scope=SCOPE, now=NOW)
    assert len({p.token for p in book.passes.values()}) == 300


def test_remote_flag() -> None:
    """Passes are LAN-only unless marked remote; the flag persists and validates."""
    book = _book()
    home = book.create(name="Home", scope=SCOPE, now=NOW)
    away = book.create(name="Sitter", scope=SCOPE, now=NOW, duration_minutes=60, remote=True)
    assert not home.remote and away.remote
    assert book.has_remote(NOW)
    assert not book.has_remote(NOW + timedelta(hours=2))  # the remote pass expired
    book.update(away.id, now=NOW, active=False)
    assert not book.has_remote(NOW)  # paused passes don't count
    book.update(away.id, now=NOW, active=True, remote=False)
    assert not book.has_remote(NOW)
    with pytest.raises(PassError):
        book.update(home.id, now=NOW, remote="yes")
    with pytest.raises(PassError):
        book.create(name="X", scope=SCOPE, now=NOW, remote=1)
    book.update(home.id, now=NOW, remote=True)
    loaded = PassBook.from_storage(book.to_storage())
    assert loaded.passes[home.id].remote is True
    assert PassBook.from_storage([{**home.to_storage(), "remote": None}]).passes[home.id].remote is False
