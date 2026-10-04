"""Storage for the Hearth APK the owner shares with guests.

Pure Python, no Home Assistant imports. All methods do blocking file IO; Home Assistant
code calls them through `hass.async_add_executor_job`, one upload at a time.

Layout inside the directory:
    hearth.apk        the shared APK (replaced atomically)
    hearth.apk.json   metadata {version_name, version_code, size, sha256, uploaded_at}
    hearth.apk.part   the upload in progress
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .const import APK_MAX_CHUNK, APK_MAX_SIZE

APK_NAME = "hearth.apk"
META_NAME = "hearth.apk.json"
PART_NAME = "hearth.apk.part"
ZIP_MAGIC = b"PK"


class ApkError(ValueError):
    """Upload rejected. `code` is the API error string."""

    def __init__(self, code: str, message: str = "", received: int | None = None) -> None:
        """Initialize with an error code and, for offset errors, the bytes received."""
        super().__init__(message or code)
        self.code = code
        self.received = received


@dataclass(slots=True)
class _Upload:
    total: int
    version_code: int
    version_name: str
    received: int = 0


class ApkStore:
    """The shared APK on disk, plus the chunked upload in progress."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        """Initialize; nothing touches the disk until a method is called."""
        self.directory = Path(directory)
        self._upload: _Upload | None = None

    @property
    def apk_path(self) -> Path:
        """Path of the shared APK."""
        return self.directory / APK_NAME

    @property
    def _meta_path(self) -> Path:
        return self.directory / META_NAME

    @property
    def _part_path(self) -> Path:
        return self.directory / PART_NAME

    def load_meta(self) -> dict[str, Any] | None:
        """Metadata of the shared APK, or None when there is none (or it is broken)."""
        try:
            meta = json.loads(self._meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict) or not self.apk_path.is_file():
            return None
        return meta

    def write_chunk(
        self,
        *,
        offset: int,
        total: int,
        version_code: int,
        version_name: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Append one chunk. Returns {received} or, on the last chunk, the final metadata.

        Chunks must arrive in order: `offset` must equal the bytes received so far. An
        `offset` of 0 starts a new upload and discards any unfinished one.
        """
        if total <= 0 or total > APK_MAX_SIZE:
            raise ApkError("too_large", f"APK must be 1 byte to {APK_MAX_SIZE} bytes")
        if len(data) > APK_MAX_CHUNK:
            raise ApkError("chunk_too_large", f"chunks are at most {APK_MAX_CHUNK} bytes")
        if not data:
            raise ApkError("bad_request", "empty chunk")
        if not version_name or len(version_name) > 64 or version_code < 0:
            raise ApkError("bad_request", "invalid version")

        upload = self._upload
        if offset == 0:
            if not data.startswith(ZIP_MAGIC):
                raise ApkError("not_apk", "file does not start with PK")
            upload = self._upload = _Upload(total, version_code, version_name)
            self.directory.mkdir(parents=True, exist_ok=True)
            self._part_path.write_bytes(b"")
        elif (
            upload is None
            or offset != upload.received
            or (upload.total, upload.version_code, upload.version_name)
            != (total, version_code, version_name)
        ):
            raise ApkError(
                "bad_offset",
                "chunk does not continue the current upload",
                received=upload.received if upload else 0,
            )
        if offset + len(data) > total:
            self._abort()
            raise ApkError("too_large", "chunk goes past total")

        with self._part_path.open("ab") as part:
            part.write(data)
        upload.received += len(data)

        if upload.received < total:
            return {"received": upload.received}
        return self._finalize(upload)

    def _finalize(self, upload: _Upload) -> dict[str, Any]:
        """Verify the finished upload and move it into place atomically."""
        self._upload = None
        digest = hashlib.sha256()
        size = 0
        with self._part_path.open("rb") as part:
            head = part.read(2)
            part.seek(0)
            while block := part.read(1024 * 1024):
                digest.update(block)
                size += len(block)
        if head != ZIP_MAGIC or size != upload.total:
            self._part_path.unlink(missing_ok=True)
            raise ApkError("not_apk", "upload is not a valid APK")
        meta = {
            "version_name": upload.version_name,
            "version_code": upload.version_code,
            "size": size,
            "sha256": digest.hexdigest(),
            "uploaded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        os.replace(self._part_path, self.apk_path)
        tmp_meta = self._meta_path.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(meta), "utf-8")
        os.replace(tmp_meta, self._meta_path)
        return {"received": size, "done": True, "sha256": meta["sha256"], "meta": meta}

    def _abort(self) -> None:
        self._upload = None
        self._part_path.unlink(missing_ok=True)
