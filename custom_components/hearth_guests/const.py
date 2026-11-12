"""Constants for the Hearth Guests integration.

This module must stay free of Home Assistant imports: the pure core (passes, scope,
ratelimit, netcheck, apk) imports it and is unit tested without Home Assistant.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "hearth_guests"
NAME: Final = "Foyer Guests"
VERSION: Final = "0.3.0"

# Bump together with API.md and the app's GuestApi.kt.
API_VERSION: Final = 1
# Additive capabilities clients can check in hearth_guests/info (API.md, "Features").
FEATURES: Final = ("remote_passes", "access_covers_need_locks", "scope_member_check", "web_app")

# Config entry options.
CONF_LAN_ONLY: Final = "lan_only"
DEFAULT_LAN_ONLY: Final = True

# Storage.
STORAGE_KEY: Final = "hearth_guests"
STORAGE_VERSION: Final = 1
STORAGE_DIR: Final = "hearth_guests"  # Directory inside .storage for the shared APK.

# Passes.
PURGE_AFTER: Final = timedelta(days=30)
MAX_NAME_LENGTH: Final = 64
ACTIVITY_MAX: Final = 200
LAST_SEEN_SAVE_DELAY: Final = 10  # seconds; debounced save for activity / last_seen

# Guest HTTP.
HEADER_GUEST_TOKEN: Final = "X-Hearth-Guest"
MAX_TOKEN_LENGTH: Final = 256
MAX_ACTION_BODY: Final = 4 * 1024
SSE_PING_INTERVAL: Final = 25  # seconds
SSE_QUEUE_SIZE: Final = 512

# Bad-token rate limit: 10 failures in 10 minutes block the IP for 15 minutes.
RATE_LIMIT_FAILURES: Final = 10
RATE_LIMIT_WINDOW: Final = 10 * 60.0
RATE_LIMIT_BLOCK: Final = 15 * 60.0

# APK sharing.
APK_MAX_SIZE: Final = 200 * 1024 * 1024
APK_MAX_CHUNK: Final = 4 * 1024 * 1024

# URLs.
URL_LANDING: Final = "/hearth-guest"
URL_GUEST_SESSION: Final = "/api/hearth_guests/guest/session"
URL_GUEST_EVENTS: Final = "/api/hearth_guests/guest/events"
URL_GUEST_ACTION: Final = "/api/hearth_guests/guest/action"
URL_APP_INFO: Final = "/api/hearth_guests/app.json"
URL_APP_APK: Final = "/api/hearth_guests/app.apk"
URL_OWNER_APK: Final = "/api/hearth_guests/owner/apk"
URL_WEB_APP: Final = "/foyer"  # Foyer for the web; files under /foyer/

# Content-Security-Policy for Foyer for the web (foyer-web/DESIGN.md section 6). The single
# source of truth: foyer-web/src/app/csp.ts must hold the same string, and a foyer-web unit
# test compares the two. The built index.html carries it as a <meta> without frame-ancestors.
FOYER_CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'self'; object-src 'none'; "
    "require-trusted-types-for 'script'; trusted-types foyer"
)

# Bus events.
EVENT_PASS_CREATED: Final = "hearth_guests_pass_created"
EVENT_PASS_EXPIRED: Final = "hearth_guests_pass_expired"
EVENT_ACTION: Final = "hearth_guests_action"

# Dispatcher signal sent whenever the set of passes changes (sensor refresh).
SIGNAL_PASSES_CHANGED: Final = f"{DOMAIN}_passes_changed"

# Capabilities: capability -> domains, and domain -> actions (API.md, "Scope").
CAPABILITY_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    "lights": ("light",),
    "switches": ("switch", "input_boolean"),
    "fans": ("fan",),
    "climate": ("climate",),
    "locks": ("lock",),
    "covers": ("cover",),
    "media": ("media_player",),
    "scenes": ("scene",),
}

DOMAIN_ACTIONS: Final[dict[str, tuple[str, ...]]] = {
    "light": ("turn_on", "turn_off", "toggle", "set_brightness"),
    "switch": ("turn_on", "turn_off", "toggle"),
    "input_boolean": ("turn_on", "turn_off", "toggle"),
    "fan": ("turn_on", "turn_off", "toggle", "set_percentage"),
    "climate": ("set_temperature", "set_hvac_mode", "turn_on", "turn_off"),
    "lock": ("lock", "unlock"),
    "cover": ("open", "close", "stop", "set_position"),
    "media_player": (
        "turn_on",
        "turn_off",
        "play_pause",
        "next",
        "previous",
        "set_volume",
        "mute",
    ),
    "scene": ("activate",),
}

# Covers that let someone into the home (API.md, "Scope" rule 5): garage doors, gates and
# doors. They resolve only for passes that also grant "locks", never for "covers" alone.
ACCESS_COVER_CLASSES: Final = frozenset({"garage", "gate", "door"})

CAPABILITIES: Final = tuple(CAPABILITY_DOMAINS)
SUPPORTED_DOMAINS: Final = frozenset(DOMAIN_ACTIONS)

# Presets (API.md, "Presets"), served by hearth_guests/info.
PRESETS: Final[tuple[dict[str, object], ...]] = (
    {
        "id": "essentials",
        "name": "Guest essentials",
        "description": (
            "Lights, climate, fans, media, blinds and scenes. "
            "No locks, garage doors or gates."
        ),
        "capabilities": ["lights", "climate", "fans", "media", "covers", "scenes"],
        "all_areas": True,
    },
    {
        "id": "door_and_lights",
        "name": "Door & lights",
        "description": "Lights and door locks.",
        "capabilities": ["lights", "locks"],
        "all_areas": True,
    },
    {
        "id": "house_sitter",
        "name": "House sitter",
        "description": "Everything a guest can be given, including locks.",
        "capabilities": [
            "lights",
            "switches",
            "fans",
            "climate",
            "locks",
            "covers",
            "media",
            "scenes",
        ],
        "all_areas": True,
    },
    {
        "id": "lights_only",
        "name": "Just the lights",
        "description": "Lights only.",
        "capabilities": ["lights"],
        "all_areas": True,
    },
)
