# Yeelight Web Controller

A REST bridge that puts your Yeelight LAN bulbs on the same wire as the rest of your home.

## Overview

Yeelight Web Controller is a small Python service that wraps the Yeelight LAN protocol in plain HTTP. It speaks the contract a `matter_webcontrol` logical bridge expects, so your bulbs federate cleanly into a wider Matter setup, and it exposes a colour-flow API that drives on-device animations — used as the target for `light_programmer`'s rain effect and by HomeKit bridges that sit one hop further out.

Everything is REST. No embedded scripts. No `exec`. One header for auth.

## How it works

The server keeps every bulb warm. Each bulb gets one long-lived LAN connection, reused across commands, so a control call or a state read rides an already-open socket instead of paying a fresh TCP handshake every time. Access to each bulb is serialised, so the main channel, the night-light channel, and a colour-flow never collide on the one socket the bulb allows.

Reads are answered from memory. The federation feed — level, colour temperature in mireds, on/off, names, `bridge.api_version: "2"` — is served from an in-memory snapshot that a background refresh keeps current, so a poll from a logical bridge returns in about a millisecond and never blocks on the network. SSDP, which takes a fixed two seconds and goes silent on long-running bulbs, is kept off that path: it runs only when a bulb is still unidentified, or on a long throttle.

The bulb roster is sticky. Each bulb's identity — hardware ID, model, IP — is resolved once by asking the bulb directly (a unicast capability probe that answers in milliseconds, even when broadcast discovery hears nothing), and then it stays. A bulb that misses a single read because of a transient stall is kept on the roster, marked unreachable, with its last-known state — not dropped. A long-running ceiling light can't quietly fall out of the federation because of one timeout, and a seeded bulb is known by its permanent hex ID from the first request rather than the `yeelight_<ip>` placeholder.

Every bulb has one true name: its hardware ID, a hex string the bulb reports. It belongs to the bulb. It survives restarts, DHCP changes, and the bulb moving to a new IP. Store it, pin to it, address bulbs by it.

`yeelight_<ip>` is a placeholder, not an address — used only in the rare case where a seeded bulb never answers the capability probe. Once the hardware ID is known it replaces the placeholder permanently and is persisted, so it survives restarts. Aliases set with `/api/name` are for people, not lookups; the server only resolves commands against the canonical ID.

A factory reset is the one thing that mints a new hardware ID. Reset a bulb, update what you've pinned.

## Colour flow

`/api/flow` offloads animation to the bulb itself. The caller supplies a `base` brightness, a `peak`, a `kelvin`, and a `lightning` flag. The server builds an overcast-sky flow on the main white channel — a slow wobble around `base`, with optional cool-temperature flashes up toward `peak` followed by a dark pause. Brightness in Yeelight flows is absolute, so the caller is expected to pre-compute `base` and `peak` against whatever schedule it owns. `/api/flow/stop` releases the bulb back to direct control.

This is how `light_programmer`'s rain `"effect": "flow"` drives the bulb: the schedule loop hands off, the bulb animates on its own, and the loop steps out of the level-control path until the rain clears.

## Moonlight

Some Yeelights carry a second, physical light source — a dim warm night-light. Ceiling lights have it, and so do Bedside Lamp 2 and 3. `/api/moonlight` switches a bulb onto it: send `on` with a `level` from 1 to 100 and the bulb drops to its night-light channel at that brightness; send `on: false` and it returns to the normal channel. The server gates on the bulb's model, so a bulb without the channel quietly falls back to the lowest normal brightness rather than failing — the caller always gets a dim light. A running colour-flow is stopped first, so the power-mode switch is never ambiguous.

`/api/level` reaches the same channel through a reserved value: raw `1` enters moonlight (same model gate, same fallback). This is how `light_programmer` renders the bottom of its brightness range — a sub-one schedule level is sent as raw `1` over the normal level path, so an artificial skylight glows like moonlight overnight instead of going dark.

## Soft on/off

Switching a light is a gentle fade, not an instant snap. Every bulb is given a smooth default transition, so turning it off dims the light down to zero and *then* cuts power, and turning it on lifts the brightness back up to the level you asked for. This applies to every on/off path uniformly — a HomeKit tap, a schedule change, or a kill-all all fade the same way.

The on and off fades are set independently, so a light can rise gently — a two-second sunrise — yet still switch off briskly. `YEELIGHT_SOFT_ON_MS` sets the turn-on fade and `YEELIGHT_SOFT_OFF_MS` the turn-off fade (milliseconds); each falls back to `YEELIGHT_SOFT_MS` (default `800`) when unset, so one variable still tunes both at once. Drop a value to something small like `50` to make that direction effectively instant. Brightness and colour-temperature changes keep their own transition timing.

### Bridged fade

For bulbs with a night-light channel (ceiling lights, Bedside Lamp 2/3), an opt-in mode gets much closer to a Casambi fixture's eased hardware fade. Set `YEELIGHT_BRIDGED_FADE=1` and, instead of the firmware's linear transition, the bridge drives a perceptual (gamma) brightness *and* colour-temperature ramp on the white channel, then bridges through the moonlight channel as a sub-1% extension so the descent to black stays continuous — the white channel's 1% floor no longer cuts off hard. Turning off, the light dims and warms down the white channel, hands over to the moonlight channel at its dimmest, then powers off; turning on reverses it. The handoff is placed at the dimmest, warmest point where it is least visible. It runs in the background so callers are never blocked, and because a real light only switches on or off every few minutes it stays well under Yeelight's per-minute command quota.

Tuning knobs (env): `YEELIGHT_FADE_GAMMA` (dark-dwell, default `2.2`), `YEELIGHT_FADE_MAIN_MS` (white ramp, `1400`), `YEELIGHT_FADE_MOON_MS` (moonlight ramp, `700`), `YEELIGHT_FADE_MOON_BRIDGE` (night-light level at the crossover, `15`), `YEELIGHT_FADE_CT_WARM` (dim-end colour temp, `2000`), `YEELIGHT_FADE_HANDOFF_MS` (channel-switch fade, `150`).

## API surface

Read: `GET /api/devices`, `GET /api/lights`, `GET /api/metadata`, `GET /api/level`, `GET /api/mired`.

Control: `POST /api/set`, `POST /api/level` (raw `1` enters moonlight on capable models), `POST /api/mired`.

Animation: `POST /api/flow`, `POST /api/flow/stop`.

Night-light: `POST /api/moonlight`.

Discovery and aliases: `GET /api/refresh`, `GET /api/seed`, `GET /api/probe`, `POST /api/name`, `GET /api/name/remove`.

Every endpoint takes `X-API-Key` when the server is started with a key. Errors map cleanly: 404 for unknown device, 400 for bad parameters, 401 for auth, 500 for everything else.

## Installation

Requires Python 3.12 or later, and LAN Control switched on for each bulb in the Yeelight Classic app.

Install from PyPI as `yeelight-web-controller`, or editable from a checkout. Start with `yeelight-srv`, bind to a host and port, and provide an API key when exposing the service on the LAN. `--seed-ip` and `--probe-subnet` reach bulbs when SSDP is silent; `--auto-probe` scans the local /24 on the first request when discovery and cache come back empty.

A seeded bulb is identified up front: at seed time the server asks the bulb for its hardware ID and model directly, so it joins the roster under its permanent hex ID — no waiting for broadcast SSDP. Tuning knobs (env): `YEELIGHT_STATE_TTL` (snapshot freshness, default 10s), `YEELIGHT_SSDP_THROTTLE` (min seconds between broadcast sweeps, default 300s), `YEELIGHT_SOFT_ON_MS` / `YEELIGHT_SOFT_OFF_MS` (soft turn-on / turn-off fade, each default `YEELIGHT_SOFT_MS` = 800ms).

## Federation

Pair the service with a running `matter-srv` and your Yeelight bulbs join your Matter devices in one place. The matter side calls `/api/devices`, `/api/level`, `/api/mired`, and `/api/set` directly over HTTP — no script execution, no event blobs. Registering a bridge also refreshes its device list, which is how to recover after a placeholder ID upgrades to a hardware ID.

## Known limits

SSDP goes quiet on long-running bulbs. Yeelight broadcasts `NOTIFY` only for a short window after power-on; after that, broadcast discovery returns nothing even though TCP/55443 still answers. This no longer hurts identity — a seeded bulb is identified by a unicast capability probe to its own IP, which keeps answering — but a brand-new bulb you haven't pinned still needs `--seed-ip`, `--probe-subnet`, or `/api/probe` to be found.

Bulbs occasionally drop off the LAN, roughly every two weeks. The only known recovery is a hardware power-cycle — the LAN protocol exposes no reboot command, so this needs a smart plug upstream or someone at the wall switch. While a bulb is gone it stays on the roster marked unreachable (`reachable: false` in `/api/lights`) with its last-known state, rather than vanishing.

The probe scans one /24 per local interface. Multi-VLAN setups need an explicit `--probe-subnet` per network.

Only colour-temperature and dimmable LAN-control bulbs are tested. RGB-only models aren't exercised. The Yeelight LAN protocol is unauthenticated on the wire — the API key guards the REST front, not port 55443 on the bulb.

## Tests

Two layers. Hardware-free unit tests always run and need no bulb: they cover the moonlight model gate, night-light state reporting, and the whole caching layer — sticky identity (a failed read never drops a known bulb, the hex ID never degrades), the SSDP-skip decision, the in-memory snapshot TTL, atomic/corruption-safe persistence, scan-input validation, and the optimistic state patch.

The real-device suites drive the actual bulbs and are skipped until you point them at one or more over `YEELIGHT_TEST_IPS` (comma-separated, e.g. `YEELIGHT_TEST_IPS=192.168.1.7,192.168.1.236`; `YEELIGHT_TEST_IP` still works for a single bulb). They split into read-only checks — identity resolution, the federation device list, stable metadata classification, warm-snapshot poll latency, connection-pool reuse — which never change what the lights are doing, and `@pytest.mark.mutating` checks that actually drive level, colour temperature, and the moonlight channel on every configured bulb. Run everything safe with `-m "not mutating"`; run the light-changing ones with `-m mutating`.

```bash
pytest -m "not mutating"          # hardware-free + read-only real-device
YEELIGHT_TEST_IPS=192.168.1.7,192.168.1.236 pytest -m mutating   # drives the bulbs
```

## Related projects

- [`matter_webcontrol`](https://github.com/dongnh/matter_webcontrol) — the Matter-side server this bridge federates into.
- [`light_programmer`](https://github.com/dongnh/light_programmer) — schedule engine that drives sub-one schedule levels as the reserved `/api/level` raw `1` (moonlight).
