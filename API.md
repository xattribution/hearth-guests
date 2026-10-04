# Hearth Guests — API contract

The contract between the `hearth_guests` Home Assistant integration, the Hearth Android app
(owner screens and guest mode) and the built-in web guest panel. All three are built against
this file. If the contract changes, change this file first, then bump `API_VERSION` in the
integration's `const.py` and in the app's `data/guests/GuestModels.kt`.

`API_VERSION = 1`

## Concepts

- **Pass**: one guest. It has a name, an optional expiry (`null` means permanent), an owner
  on/off switch (`active`) and a **scope**. A pass carries a random **token**
  (`secrets.token_urlsafe(32)`). Anyone holding the token can use exactly what the scope
  allows, nothing else.
- **No HA account for guests.** The integration is a scoped gatekeeper. Guests never get a
  Home Assistant token. Every guest request is checked against the pass's scope and turned
  into a whitelisted service call by the integration.
- **Expiry**: when `expires_at` passes, the pass stops working immediately. Every request
  re-checks it, and a timer also closes open event streams and fires the
  `hearth_guests_pass_expired` bus event. Expired passes stay listed so the owner can renew
  them. They are purged 30 days after expiry.
- **LAN only** (default, option `lan_only`): guest endpoints, the landing page and the APK
  download refuse requests that do not come from a private, link-local or loopback address.
  They also refuse requests that come through Nabu Casa cloud. Owner endpoints are normal
  authenticated HA endpoints and work wherever HA works.
- **Away from home** (per pass, `remote: true`, feature `remote_passes`): that pass also works
  off the LAN, typically through Nabu Casa or another external URL. While no usable remote pass
  exists, off-LAN requests are refused before any token is checked, and the landing page is
  only served off the LAN while one does. The APK download stays LAN only.

## Scope

```json
{
  "capabilities": ["lights", "climate", "locks"],
  "all_areas": false,
  "areas": ["living_room", "guest_room"],
  "devices": ["<device_id>"],
  "entities": ["light.porch"],
  "exclude": ["light.office_lamp"]
}
```

Resolution:

1. The candidate set is:
   - every entity in `entities`;
   - every entity of each device in `devices`;
   - every entity whose area is in `areas`, or every area when `all_areas` is true. An entity's
     area is its own area, or its device's area if it has none.
2. Area and device expansion skips disabled entities, hidden entities, entities with an
   `entity_category` (config or diagnostic) and entities in unsupported domains.
3. Every entity, including ones listed explicitly, must be in a domain enabled by a capability.
4. Remove `exclude`.

| capability | domains | actions |
|---|---|---|
| `lights` | `light` | `turn_on`, `turn_off`, `toggle`, `set_brightness` (value 0–100 %) |
| `switches` | `switch`, `input_boolean` | `turn_on`, `turn_off`, `toggle` |
| `fans` | `fan` | `turn_on`, `turn_off`, `toggle`, `set_percentage` (0–100) |
| `climate` | `climate` | `set_temperature` (number, clamped to the entity's min_temp…max_temp), `set_hvac_mode` (must be one of the entity's `hvac_modes`), `turn_on`, `turn_off` |
| `locks` | `lock` | `lock`, `unlock` |
| `covers` | `cover` | `open`, `close`, `stop`, `set_position` (0–100) |
| `media` | `media_player` | `turn_on`, `turn_off`, `play_pause`, `next`, `previous`, `set_volume` (0–100), `mute` (bool) |
| `scenes` | `scene` | `activate` |

There is never any capability for alarm panels, cameras, scripts, automations, buttons,
sensors' raw data, or anything else.

### Presets (served by the integration so the app and web panel agree)

| id | name | capabilities | all_areas |
|---|---|---|---|
| `essentials` | Guest essentials | lights, climate, fans, media, covers, scenes | true |
| `door_and_lights` | Door & lights | lights, locks | true |
| `house_sitter` | House sitter | lights, switches, fans, climate, locks, covers, media, scenes | true |
| `lights_only` | Just the lights | lights | true |

A preset is a starting point. The app copies its capabilities into the scope, and the owner
then narrows the areas and devices.

## Pass object (owner view)

```json
{
  "id": "a1b2c3d4",
  "name": "Sam",
  "created_at": "2026-10-04T20:00:00+00:00",
  "expires_at": "2026-10-06T18:00:00+00:00",
  "active": true,
  "status": "active",
  "last_seen": "2026-10-04T21:13:00+00:00",
  "scope": { },
  "entity_count": 12,
  "token": "<secret>",
  "link": "http://192.168.1.10:8123/hearth-guest#t=<secret>",
  "remote": false,
  "remote_link": null
}
```

- `remote_link` is set only for remote passes when HA has an external URL (Nabu Casa first):
  `https://<external>/hearth-guest#t=<secret>`. It works at home too, so clients show it
  instead of `link` for remote passes.

- `expires_at` is `null` when the pass is permanent.
- `status` is `"active"`, `"paused"` (`active` is false) or `"expired"`.
- `last_seen` is `null` until the guest first uses the pass.

The token is stored server-side, the way HA stores its own refresh tokens, so the owner can
show the QR again. `link` uses the base URL passed to the command, else HA's internal URL.

## Owner API: Home Assistant WebSocket (admin only)

All commands need an admin user (`websocket_api.require_admin`).

| type | payload | result |
|---|---|---|
| `hearth_guests/info` | none | `{api_version, version, lan_only, guest_base_url, remote_base_url, features: ["remote_passes"], apk: {version_name, version_code, size, sha256, uploaded_at} \| null, presets: [{id, name, description, capabilities, all_areas}], active_count}` |
| `hearth_guests/passes/list` | `{base_url?}` | `{passes: [Pass]}`, newest first |
| `hearth_guests/passes/create` | `{name, scope, expires_at? (ISO or null), duration_minutes?, remote? (default false), base_url?}` | `{pass}` |
| `hearth_guests/passes/update` | `{pass_id, name?, scope?, expires_at? (ISO or null = permanent), active?, remote?, base_url?}` | `{pass}`. Turning `remote` off closes the pass's open streams; they reconnect and the LAN check applies. |
| `hearth_guests/passes/extend` | `{pass_id, minutes (may be negative), base_url?}` | `{pass}` |
| `hearth_guests/passes/rotate` | `{pass_id, base_url?}` | `{pass}` with a new token; the old QR stops working |
| `hearth_guests/passes/delete` | `{pass_id}` | `{}` |
| `hearth_guests/preview` | `{scope}` | `{entities: [GuestEntity]}`, showing what a guest with this scope would see |
| `hearth_guests/activity` | `{pass_id?, limit?}` | `{activity: [{at, pass_id, pass_name, entity_id, action, value}]}`, newest first, max 200 kept |

Notes on these commands:

- **`create`**: give at most one of `expires_at` or `duration_minutes`. If neither is given,
  the pass is permanent. `expires_at` must be in the future; a time without an offset is UTC.
  Names are 1–64 characters. Invalid input fails with error code `invalid_format`; an unknown
  `pass_id` with `not_found`.
- **`extend`**: adds the minutes to `max(now, expires_at)`. If the result is in the past, the
  pass expires now. A permanent pass stays permanent.

## Guest API: HTTP on Home Assistant's own port

Send the guest token in an `X-Hearth-Guest: <token>` header. Do not use `Authorization`, so
HA's own auth never sees it. Without a valid token, the endpoint returns 401
`{"error": "invalid_pass"}`. For an expired or paused pass, it returns 403
`{"error": "expired" | "paused", "name": "…"}`. A request from off the LAN gets 403
`{"error": "lan_only"}`. Ten bad tokens from one IP in ten minutes block that IP for fifteen
minutes with 429 `{"error": "rate_limited"}`. If the integration is not loaded, guest endpoints
return 503 `{"error": "not_loaded"}`.

| method & path | auth | description |
|---|---|---|
| `GET /hearth-guest` | none | The web guest panel (HTML). It reads the token from the URL fragment `#t=…` (never sent to the server), stores it in localStorage, and strips the fragment from the address bar. |
| `GET /api/hearth_guests/guest/session` | token | `{api_version, pass: {name, expires_at, permanent}, server_time, home_name, areas: [{area_id, name}], entities: [GuestEntity]}` |
| `GET /api/hearth_guests/guest/events` | token | Server-sent events (`text/event-stream`), described below |
| `POST /api/hearth_guests/guest/action` | token | Body `{entity_id, action, value?}`. Returns `{ok: true}`. Returns 403 `{error: "not_allowed"}` when the action is outside the scope, and 400 `{error: "bad_value"}` for an invalid value or a malformed body (413 when the body is over 4 KiB). Returns 502 `{error: "failed"}` when Home Assistant rejects or fails the service call. |
| `GET /api/hearth_guests/app.json` | none (LAN) | `{version_name, version_code, size, sha256}`, or 404 when no APK has been shared |
| `GET /api/hearth_guests/app.apk` | none (LAN) | The Hearth APK the owner shared (`application/vnd.android.package-archive`, `Content-Disposition: attachment; filename="hearth.apk"`) |
| `POST /api/hearth_guests/owner/apk` | HA bearer, admin | Chunked upload. Query `offset`, `total`, `version_code`, `version_name`; the body is raw bytes, at most 4 MiB per chunk. Returns `{received}`, and on the final chunk `{received, done: true, sha256}`. The final file must start with `PK` and be at most 200 MiB. Errors: 409 `{error: "bad_offset", received}` (resume from `received`), 400 `{error: "not_apk" \| "too_large" \| "bad_request"}`, 413 `{error: "chunk_too_large"}`, 403 `{error: "admin_required"}`. `offset=0` starts over. |

### Events stream

The stream sends these events:

- `event: state`, `data: GuestEntity` for every in-scope entity right after connecting (so
  nothing is missed between `/session` and the stream), then whenever one changes.
- `event: session`, `data: {pass: {name, expires_at, permanent}}` when the owner renames or
  extends the pass. A scope change sends `session` and then a `state` for every entity; the
  client should simply re-fetch `/session`.
- `event: ended`, `data: {reason: "expired" | "paused" | "deleted"}`, after which the stream
  closes.
- `: ping` comment lines every 25 s.

Rotating the token closes the stream without an `ended` event; reconnecting with the old token
gets 401.

### GuestEntity

```json
{
  "entity_id": "climate.living_room",
  "name": "Living room AC",
  "domain": "climate",
  "area_id": "living_room",
  "state": "cool",
  "attributes": { "current_temperature": 77, "temperature": 74, "min_temp": 60, "max_temp": 86,
                  "target_temp_step": 1, "hvac_modes": ["off", "cool", "fan_only"],
                  "temperature_unit": "°F" },
  "actions": ["set_temperature", "set_hvac_mode", "turn_on", "turn_off"]
}
```

Attributes are whitelisted:

- `brightness` (converted to 0–100 % as `brightness_pct`);
- climate: `current_temperature`, `temperature`, `target_temp_low`, `target_temp_high`,
  `min_temp`, `max_temp`, `target_temp_step`, `hvac_modes`, `hvac_action`;
- `temperature_unit` (from HA's unit system);
- `percentage`;
- `current_position`;
- media: `media_title`, `media_artist`, `volume_level` (0–100), `is_volume_muted`;
- `icon`.

`actions` lists only what the entity supports (for example no `set_brightness` for an on/off
light, no `set_position` for a cover without positioning), based on its supported features;
an action that is not listed is refused with `not_allowed`.

Nothing else is passed through. In particular there are no entity pictures, no tokens and
no user data.

## Deep link and QR

- The QR encodes the landing URL: `http://<lan-host>:8123/hearth-guest#t=<token>`. Scanned
  with a phone camera, it opens the web panel, which works on any phone (iPhone included).
  The panel also offers "Get the Android app" (when an APK is shared) and "Open in Hearth".
- "Open in Hearth" is `hearth://guest?b=<urlencoded base url>&t=<token>`.
- The Hearth app's guest scanner accepts either form. From the landing URL, the base URL is
  scheme + host + port.

## Features

`hearth_guests/info` lists additive features so newer clients can tell what an older
integration supports without bumping `API_VERSION`:

| feature | meaning |
|---|---|
| `remote_passes` | passes accept `remote`; `remote_link` and `remote_base_url` are reported |

## Bus events (for automations)

| event | data |
|---|---|
| `hearth_guests_pass_created` | `{pass_id, name}` |
| `hearth_guests_pass_expired` | `{pass_id, name}` |
| `hearth_guests_action` | `{pass_id, name, entity_id, action, value}` |

The integration also creates the entity `sensor.hearth_guests_active`. Its state is the number
of active passes, and its `guests` attribute lists their names and expiry times.
