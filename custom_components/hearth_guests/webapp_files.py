"""Foyer for the web: the file table behind /foyer/ (API.md, "Foyer for the web").

Pure, with no Home Assistant imports, so it is unit tested with plain pytest. The table is
built once from www/foyer and maps a request path to a file and its response headers. Only
paths in the table are served: the request path is never joined onto the filesystem, so it
cannot reach outside the folder.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from types import MappingProxyType

from .const import FOYER_CSP

WEB_APP_DIR = Path(__file__).parent / "www" / "foyer"
INDEX = "index.html"

# Only these file types are served; anything else in the folder is ignored.
CONTENT_TYPES: Mapping[str, str] = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".webmanifest": "application/manifest+json",
        ".json": "application/json",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".txt": "text/plain; charset=utf-8",
    }
)
# Precompressed siblings written by foyer-web's postbuild; aiohttp's FileResponse picks them
# when the request's Accept-Encoding allows. They are never served under their own names.
COMPRESSED_SUFFIXES = (".br", ".gz")

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"  # assets/: content-hashed names
CACHE_ICONS = "public, max-age=86400"
CACHE_REVALIDATE = "no-cache"

PERMISSIONS_POLICY = "camera=(), geolocation=(), microphone=(self), payment=(), usb=()"


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One servable file: where it is and the headers it is sent with."""

    path: Path
    headers: Mapping[str, str]


def headers_for(rel_path: str, *, compressed: bool = False) -> dict[str, str]:
    """Response headers for a file at `rel_path` (a POSIX path relative to www/foyer)."""
    suffix = os.path.splitext(rel_path)[1].lower()
    if rel_path.startswith("assets/"):
        cache = CACHE_IMMUTABLE
    elif rel_path.startswith("icons/"):
        cache = CACHE_ICONS
    else:
        cache = CACHE_REVALIDATE
    headers = {
        "Content-Type": CONTENT_TYPES.get(suffix, "application/octet-stream"),
        "Cache-Control": cache,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if compressed:
        headers["Vary"] = "Accept-Encoding"
    if rel_path == INDEX:
        headers["Content-Security-Policy"] = FOYER_CSP
        headers["Permissions-Policy"] = PERMISSIONS_POLICY
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
    elif rel_path == "sw.js":
        headers["Content-Security-Policy"] = FOYER_CSP
    return headers


def build_file_table(root: Path = WEB_APP_DIR) -> dict[str, FileEntry]:
    """Map each servable file under `root` to its entry; empty when the folder is missing.

    Skips hidden files and folders, symlinks, precompressed siblings and unknown types.
    Runs blocking I/O: call it in the executor.
    """
    table: dict[str, FileEntry] = {}
    if not root.is_dir():
        return table
    for dirpath, dirnames, filenames in os.walk(root):  # does not follow symlinked dirs
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        base = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(COMPRESSED_SUFFIXES):
                continue
            if os.path.splitext(name)[1].lower() not in CONTENT_TYPES:
                continue
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            compressed = any(
                (base / f"{name}{suffix}").is_file() for suffix in COMPRESSED_SUFFIXES
            )
            table[rel] = FileEntry(
                path, MappingProxyType(headers_for(rel, compressed=compressed))
            )
    return table


def lookup(table: Mapping[str, FileEntry], path: str) -> FileEntry | None:
    """The entry for a request path below /foyer/ ("" is the index), or None."""
    return table.get(path or INDEX)
