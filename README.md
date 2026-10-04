# Hearth Guests

A Home Assistant custom integration that gives guests temporary, scoped control of your home.
You create a **pass** (a name, an end time or none, and what the guest may use), then show its
QR code. The guest scans it with their phone camera and gets a simple control panel in the
browser (iPhone included), or opens it in the Hearth Android app's guest mode. Guests don't
need a Home Assistant login. When the time is up, access stops.

Everything runs on the Home Assistant box. There is no cloud service and nothing else to host.

The contract between the integration, the Hearth app and the web panel is in
[`API.md`](API.md).

## Security model

- **Scoped gatekeeper.** Guests never get a Home Assistant account or token. Each guest request
  is checked against the pass's scope (areas, devices, entities, capabilities) and turned into
  one whitelisted service call, with its value validated and clamped. The scope is resolved
  again on every request. Alarm panels, cameras, scripts, automations, buttons and raw sensor
  data are never reachable.
- **Tokens.** Each pass has a random 256-bit token (`secrets.token_urlsafe(32)`). Lookups
  compare every pass with `hmac.compare_digest`, and tokens are never logged. Rotating a pass
  invalidates the old QR code at once.
- **Token in the URL fragment.** The QR link is `…/hearth-guest#t=<token>`. Browsers never send
  the fragment to the server, so the token stays out of server and proxy logs. The panel moves
  it to local storage and strips it from the address bar. API calls send it in an
  `X-Hearth-Guest` header, never in `Authorization`.
- **Rate limit.** Ten bad tokens from one IP within ten minutes block that IP for fifteen
  minutes.
- **Expiry.** Every request re-checks expiry and pause. A timer also closes open live-update
  streams the moment a pass expires and fires `hearth_guests_pass_expired`. Expired passes stay
  listed for 30 days so you can renew them, then they are deleted.
- **LAN only** (default). Guest endpoints, the panel and the APK download only answer private,
  link-local and loopback addresses. Requests that arrive through Home Assistant Cloud (Nabu
  Casa remote UI) are refused even though they come from a local socket. You can switch this
  off under the integration's options, but you shouldn't.
- **Away from home, per pass.** A pass marked "works away from home" (`remote`) also works
  through your external URL (Nabu Casa first), for a sitter who arrives before joining your
  Wi-Fi. Every other pass stays LAN only. While no usable remote pass exists, off-LAN requests
  are refused before any token is checked and the panel isn't served off the LAN at all.
- The panel is a single file with a strict Content-Security-Policy (`connect-src 'self'`, no
  external requests) and `Referrer-Policy: no-referrer`.

## Install

### With HACS (recommended)

1. In HACS, open the menu (⋮) → **Custom repositories**, add
   `https://github.com/xattribution/hearth-guests` with type **Integration**.
2. Find **Hearth Guests** in HACS, download it, and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Hearth Guests**.

HACS then offers each new release as an update inside Home Assistant.

### By hand

1. Copy `custom_components/hearth_guests` into your Home Assistant config folder at
   `/config/custom_components/hearth_guests`. Use the Samba, SSH or File editor add-on, or
   `scp`:

   ```sh
   scp -r custom_components/hearth_guests root@homeassistant.local:/config/custom_components/
   ```

2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Hearth Guests**.

You need Home Assistant 2025.3 or newer (tested against 2026.2).

## Using it

### From Hearth

In the Hearth app, open **Settings → Guests**. Pick a preset (*Guest essentials*,
*Door & lights*, *House sitter*, *Just the lights*), narrow it to the rooms and devices you
want, set an end time, and show the QR code. From the same screen you can pause, extend,
rotate or delete a pass, and see what guests did. Only Home Assistant admins can manage passes.

### The web panel

Scanning the QR code opens `http://<ha-lan-address>:8123/hearth-guest`. It shows "Hi <name>",
the time left, and tiles grouped by room:

- lights, switches and fans toggle with a tap; lights and fans get a level slider;
- locks need a second tap to unlock;
- thermostats have −/+ and mode chips;
- blinds have open, stop and close; media players have play/pause, skip and volume;
- scenes have a Run button.

It updates live and falls back to polling if streaming isn't possible. When the pass ends it
says so: "Your access has ended — ask your host for a new code."

The QR link uses the base URL Hearth sends when it asks for the pass, or else Home
Assistant's internal URL. If the link points somewhere the guest's phone can't reach, set the
local network URL under **Settings → System → Network** in Home Assistant.

### Sharing the Android app

Hearth can upload its own APK to the integration from its guest settings (an admin-only,
chunked upload; see `API.md`). Android guests then see **Get the Android app** and **Open in Hearth** buttons in the web panel. The APK
is stored under `/config/.storage/hearth_guests/` and served only on the LAN.

## Automations

Bus events:

| event | data |
|---|---|
| `hearth_guests_pass_created` | `pass_id`, `name` |
| `hearth_guests_pass_expired` | `pass_id`, `name` |
| `hearth_guests_action` | `pass_id`, `name`, `entity_id`, `action`, `value` |

```yaml
automation:
  - alias: "Lock up when a guest pass expires"
    triggers:
      - trigger: event
        event_type: hearth_guests_pass_expired
    actions:
      - action: lock.lock
        target:
          entity_id: lock.front_door
      - action: notify.mobile_app_my_phone
        data:
          message: "{{ trigger.event.data.name }}'s guest pass has ended."

  - alias: "Tell me when a guest unlocks the door"
    triggers:
      - trigger: event
        event_type: hearth_guests_action
        event_data:
          action: unlock
    actions:
      - action: notify.mobile_app_my_phone
        data:
          message: "{{ trigger.event.data.name }} unlocked {{ trigger.event.data.entity_id }}"
```

### Sensor

`sensor.hearth_guests_active` is the number of active passes (not paused, not expired). Its
`guests` attribute lists `{name, expires_at}` for each one, so you can do things like
`{{ state_attr('sensor.hearth_guests_active', 'guests') | map(attribute='name') | join(', ') }}`.

## Limitations

- **LAN only by default.** Guests have to be on your home network unless you mark a pass as
  working away from home, which needs an external URL (Nabu Casa or your own) and should go
  with an end date. The APK download is always LAN only.
- The panel uses plain HTTP if your Home Assistant does. Anyone on your Wi-Fi who can sniff
  traffic could see a token, so treat your guest network the way you'd treat a spare key.
- Guests act through Home Assistant without a user, so the logbook shows their actions with no
  user. Use the activity list in Hearth or the `hearth_guests_action` event to see who did what.

## Development

```
custom_components/hearth_guests/
  passes.py, scope.py, ratelimit.py, netcheck.py, apk.py   pure Python core, no HA imports
  hub.py        runtime: storage, expiry timer, streams, activity
  views.py      guest HTTP API, landing page, APK sharing
  websocket.py  owner API (admin websocket commands)
  sensor.py, config_flow.py, const.py
  www/guest.html  the web panel (one file, no dependencies)
tests/          pure tests (plain pytest, Python 3.11+)
tests/ha/       tests against a real Home Assistant (pytest-homeassistant-custom-component)
```

```sh
pip install pytest && pytest                         # pure tests; tests/ha is skipped
pip install pytest-homeassistant-custom-component && pytest   # everything (Python 3.13+)
```

CI runs both, plus hassfest and HACS validation (`.github/workflows/`).

This repository is published automatically from the `hearth-guests/` folder of the private
Hearth monorepo, where the Android app is developed against the same [`API.md`](API.md).
Issues and pull requests are welcome here; changes are carried back by hand. Releases are
cut automatically when `manifest.json`'s version changes.
