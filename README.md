# Yeelight Web Controller

A REST bridge that puts your Yeelight LAN bulbs on the same wire as the rest of your home.

## Overview

Yeelight Web Controller is a small Python service that wraps the Yeelight LAN protocol in plain HTTP. It speaks the contract a `matter_webcontrol` logical bridge expects, so your bulbs federate cleanly into a wider Matter setup, and it exposes a colour-flow API that drives on-device animations — used as the target for `light_programmer`'s rain effect and by HomeKit bridges that sit one hop further out.

Everything is REST. No embedded scripts. No `exec`. One header for auth.

## How it works

The server keeps a small cache of every bulb it knows — hardware ID, IP, model, last-seen state. SSDP populates it while bulbs are broadcasting. A TCP probe on port 55443 fills the gaps when they aren't. The federation feed mirrors the schema a logical bridge consumes: level, color temperature in mireds, on/off, names, and `bridge.api_version: "2"`.

Every bulb has one true name: its hardware ID, a hex string the bulb reports over SSDP. It belongs to the bulb. It survives restarts, DHCP changes, and the bulb moving to a new IP. Store it, pin to it, address bulbs by it.

`yeelight_<ip>` is a placeholder, not an address. The server uses it when you register a bulb by IP before SSDP has identified it. The first time SSDP sees that bulb, the placeholder is replaced by the hardware ID, permanently. Downstream peers that registered during the placeholder window will need to refresh — on a federated `matter_webcontrol`, re-register the bridge to pick up the change. Aliases set with `/api/name` are for people, not lookups; the server only resolves commands against the canonical ID.

A factory reset is the one thing that mints a new hardware ID. Reset a bulb, update what you've pinned.

## Colour flow

`/api/flow` offloads animation to the bulb itself. The caller supplies a `base` brightness, a `peak`, a `kelvin`, and a `lightning` flag. The server builds an overcast-sky flow on the main white channel — a slow wobble around `base`, with optional cool-temperature flashes up toward `peak` followed by a dark pause. Brightness in Yeelight flows is absolute, so the caller is expected to pre-compute `base` and `peak` against whatever schedule it owns. `/api/flow/stop` releases the bulb back to direct control.

This is how `light_programmer`'s rain `"effect": "flow"` drives the bulb: the schedule loop hands off, the bulb animates on its own, and the loop steps out of the level-control path until the rain clears.

## Moonlight

Some Yeelights carry a second, physical light source — a dim warm night-light. Ceiling lights have it, and so do Bedside Lamp 2 and 3. `/api/moonlight` switches a bulb onto it: send `on` with a `level` from 1 to 100 and the bulb drops to its night-light channel at that brightness; send `on: false` and it returns to the normal channel. The server gates on the bulb's model, so a bulb without the channel quietly falls back to the lowest normal brightness rather than failing — the caller always gets a dim light. A running colour-flow is stopped first, so the power-mode switch is never ambiguous.

`/api/level` reaches the same channel through a reserved value: raw `1` enters moonlight (same model gate, same fallback). This is how `light_programmer` renders the bottom of its brightness range — a sub-one schedule level is sent as raw `1` over the normal level path, so an artificial skylight glows like moonlight overnight instead of going dark.

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

Seeding is about reach, not identity. Once SSDP sees a seeded bulb, the hardware ID takes over.

## Federation

Pair the service with a running `matter-srv` and your Yeelight bulbs join your Matter devices in one place. The matter side calls `/api/devices`, `/api/level`, `/api/mired`, and `/api/set` directly over HTTP — no script execution, no event blobs. Registering a bridge also refreshes its device list, which is how to recover after a placeholder ID upgrades to a hardware ID.

## Known limits

SSDP goes quiet on long-running bulbs. Yeelight broadcasts `NOTIFY` only for a short window after power-on; after that, discovery returns nothing even though TCP/55443 still answers. Reach them with `--auto-probe`, `--seed-ip`, or `/api/probe`.

Bulbs occasionally drop off the LAN, roughly every two weeks. The only known recovery is a hardware power-cycle — the LAN protocol exposes no reboot command, so this needs a smart plug upstream or someone at the wall switch.

The probe scans one /24 per local interface. Multi-VLAN setups need an explicit `--probe-subnet` per network.

Only colour-temperature and dimmable LAN-control bulbs are tested. RGB-only models aren't exercised. The Yeelight LAN protocol is unauthenticated on the wire — the API key guards the REST front, not port 55443 on the bulb.

## Tests

The suite drives a real bulb and is skipped by default until you provide one over environment variables. It covers the federation feed, the v2 metadata schema, level and mireds set/get/clamp, float-brightness control, alias add/remove, header auth, and an end-to-end pass that mirrors the calls a logical bridge makes. The moonlight model gate and night-light state reporting are checked by hardware-free unit tests that always run.

## Related projects

- [`matter_webcontrol`](https://github.com/dongnh/matter_webcontrol) — the Matter-side server this bridge federates into.
- [`light_programmer`](https://github.com/dongnh/light_programmer) — schedule engine that drives sub-one schedule levels as the reserved `/api/level` raw `1` (moonlight).
