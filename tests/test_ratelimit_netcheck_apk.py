"""Tests for the bad-token limiter, the LAN address check and APK storage."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hearth_guests.apk import ApkError, ApkStore
from hearth_guests.const import APK_MAX_CHUNK, APK_MAX_SIZE
from hearth_guests.netcheck import is_lan_address
from hearth_guests.ratelimit import BadTokenLimiter


class FakeClock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Current time."""
        return self.now


# --- rate limit --------------------------------------------------------------------------


def test_limiter_blocks_after_ten_failures() -> None:
    """Ten failures in ten minutes block for fifteen minutes."""
    clock = FakeClock()
    limiter = BadTokenLimiter(clock=clock)
    for _ in range(9):
        assert limiter.record_failure("10.0.0.5") is False
        clock.now += 10
    assert not limiter.is_blocked("10.0.0.5")
    assert limiter.record_failure("10.0.0.5") is True
    assert limiter.is_blocked("10.0.0.5")
    assert not limiter.is_blocked("10.0.0.6")
    clock.now += 15 * 60 - 1
    assert limiter.is_blocked("10.0.0.5")
    clock.now += 1
    assert not limiter.is_blocked("10.0.0.5")
    # The counter starts over after a block.
    assert limiter.record_failure("10.0.0.5") is False


def test_limiter_window_slides() -> None:
    """Failures older than the window do not count."""
    clock = FakeClock()
    limiter = BadTokenLimiter(clock=clock)
    for _ in range(9):
        limiter.record_failure("ip")
    clock.now += 10 * 60 + 1
    for _ in range(9):
        assert limiter.record_failure("ip") is False
    assert not limiter.is_blocked("ip")


def test_limiter_memory_is_bounded() -> None:
    """Many distinct sources do not grow memory without bound."""
    clock = FakeClock()
    limiter = BadTokenLimiter(clock=clock)
    for i in range(10_000):
        limiter.record_failure(f"ip{i}")
    assert len(limiter._failures) <= 4096  # noqa: SLF001


# --- LAN check ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "192.168.1.20",
        "10.1.2.3",
        "172.16.0.9",
        "127.0.0.1",
        "169.254.10.10",
        "::1",
        "fe80::1%eth0",
        "fd00::1234",
        "::ffff:192.168.1.20",
        "[fd00::1]",
    ],
)
def test_lan_addresses(remote: str) -> None:
    """Private, link-local and loopback addresses are LAN."""
    assert is_lan_address(remote)


@pytest.mark.parametrize(
    "remote",
    [
        None,
        "",
        "8.8.8.8",
        "100.64.1.1",  # CGNAT / Tailscale: not LAN
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
        "0.0.0.0",
        "224.0.0.1",
        "not-an-ip",
        "192.168.1.20:8123",
    ],
)
def test_non_lan_addresses(remote: str | None) -> None:
    """Public, unparseable and missing addresses are not LAN."""
    assert not is_lan_address(remote)


# --- APK store ---------------------------------------------------------------------------


def _apk(size: int) -> bytes:
    body = bytes(i % 251 for i in range(size - 2))
    return b"PK" + body


def _upload(store: ApkStore, data: bytes, chunk: int) -> dict:
    result: dict = {}
    for offset in range(0, len(data), chunk):
        result = store.write_chunk(
            offset=offset,
            total=len(data),
            version_code=42,
            version_name="1.2.3",
            data=data[offset : offset + chunk],
        )
    return result


def test_apk_chunked_upload(tmp_path: Path) -> None:
    """A chunked upload lands atomically with metadata."""
    store = ApkStore(tmp_path / "hg")
    assert store.load_meta() is None
    data = _apk(10_000)
    result = _upload(store, data, 3000)
    digest = hashlib.sha256(data).hexdigest()
    assert result["done"] is True
    assert result["received"] == 10_000
    assert result["sha256"] == digest
    assert store.apk_path.read_bytes() == data
    meta = store.load_meta()
    assert meta is not None
    assert {k: meta[k] for k in ("version_name", "version_code", "size", "sha256")} == {
        "version_name": "1.2.3",
        "version_code": 42,
        "size": 10_000,
        "sha256": digest,
    }
    assert "uploaded_at" in meta
    assert not (tmp_path / "hg" / "hearth.apk.part").exists()
    assert json.loads((tmp_path / "hg" / "hearth.apk.json").read_text())["size"] == 10_000


def test_apk_intermediate_chunks_report_received(tmp_path: Path) -> None:
    """Intermediate chunks return {received}."""
    store = ApkStore(tmp_path)
    data = _apk(100)
    assert store.write_chunk(
        offset=0, total=100, version_code=1, version_name="1", data=data[:40]
    ) == {"received": 40}


def test_apk_rejects_gaps_and_mismatches(tmp_path: Path) -> None:
    """Chunks must continue the upload in progress."""
    store = ApkStore(tmp_path)
    data = _apk(100)
    with pytest.raises(ApkError) as err:
        store.write_chunk(offset=40, total=100, version_code=1, version_name="1", data=data[40:])
    assert err.value.code == "bad_offset"
    store.write_chunk(offset=0, total=100, version_code=1, version_name="1", data=data[:40])
    with pytest.raises(ApkError) as err:
        store.write_chunk(offset=50, total=100, version_code=1, version_name="1", data=data[50:])
    assert (err.value.code, err.value.received) == ("bad_offset", 40)
    with pytest.raises(ApkError) as err:
        store.write_chunk(offset=40, total=100, version_code=2, version_name="1", data=data[40:])
    assert err.value.code == "bad_offset"
    with pytest.raises(ApkError) as err:
        store.write_chunk(offset=40, total=100, version_code=1, version_name="1", data=data[40:] + b"x")
    assert err.value.code == "too_large"
    # Restarting at 0 works.
    assert _upload(store, data, 60)["done"] is True


def test_apk_rejects_non_zip_and_oversize(tmp_path: Path) -> None:
    """The file must start with PK, and sizes are bounded."""
    store = ApkStore(tmp_path)
    with pytest.raises(ApkError) as err:
        store.write_chunk(offset=0, total=4, version_code=1, version_name="1", data=b"MZ\x00\x00")
    assert err.value.code == "not_apk"
    with pytest.raises(ApkError) as err:
        store.write_chunk(
            offset=0, total=APK_MAX_SIZE + 1, version_code=1, version_name="1", data=b"PK"
        )
    assert err.value.code == "too_large"
    with pytest.raises(ApkError) as err:
        store.write_chunk(
            offset=0,
            total=APK_MAX_SIZE,
            version_code=1,
            version_name="1",
            data=b"PK" + bytes(APK_MAX_CHUNK),
        )
    assert err.value.code == "chunk_too_large"
    with pytest.raises(ApkError) as err:
        store.write_chunk(offset=0, total=4, version_code=1, version_name="", data=b"PK00")
    assert err.value.code == "bad_request"
    assert store.load_meta() is None


def test_apk_replacement_keeps_old_until_done(tmp_path: Path) -> None:
    """A new upload does not disturb the shared APK until it completes."""
    store = ApkStore(tmp_path)
    first = _apk(50)
    _upload(store, first, 50)
    second = _apk(80)
    store.write_chunk(offset=0, total=80, version_code=2, version_name="2", data=second[:30])
    assert store.apk_path.read_bytes() == first
    assert store.load_meta()["version_code"] == 42
    store.write_chunk(offset=30, total=80, version_code=2, version_name="2", data=second[30:])
    assert store.apk_path.read_bytes() == second
    assert store.load_meta()["version_code"] == 2
