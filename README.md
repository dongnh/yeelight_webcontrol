# Yeelight Web Controller

Your Yeelight bulbs, on the same wire as everything else.

Yeelight Web Controller is a small Python REST bridge that speaks the exact contract a [`matter_webcontrol`](https://github.com/dongnh/matter_webcontrol) logical bridge expects. Point a Matter server at it and your LAN-control Yeelight bulbs show up right beside your native Matter devices — controlled the same way, addressed the same way. No embedded scripts. No `exec()`. Just HTTP.

- **REST, end to end.** Read and set Matter-style level (0–254) and color temperature (153–500 mireds) over plain HTTP.
- **Built to federate.** `/api/devices` and `/api/metadata` (api_version `"2"`) match `LogicalBridgeClient` field for field.
- **Resilient discovery.** When SSDP multicast goes quiet, a TCP probe finds your bulbs anyway.

---

## Quick start

```bash
pip install yeelight-web-controller   # or: pip install -e .

# Create an API key. Every client sends it as an X-API-Key header.
export YEELIGHT_SRV_KEY=$(openssl rand -hex 32)

# Start the server. It scans the local /24 if SSDP finds nothing.
yeelight-srv --host 0.0.0.0 --port 9800 --auto-probe

# From another terminal, talk to it.
curl -H "X-API-Key: $YEELIGHT_SRV_KEY" http://127.0.0.1:9800/api/devices
```

You’ll need Python 3.12 or later, and **LAN Control** switched on for each bulb in the Yeelight Classic app.

---

## How devices are identified

This is the part worth understanding, because it’s where integrations usually trip.

**Every bulb has one true name: its hardware ID.** When the server discovers a bulb over SSDP, the bulb reports a permanent hardware identifier — a hex string like `0x000000003415e584`. That ID belongs to the bulb itself. It never changes. It survives server restarts, DHCP lease changes, and a bulb moving to a new IP address. **This is the ID you should store and address bulbs by.**

**`yeelight_<ip>` is a placeholder, not an address.** If you register a bulb by IP — `--seed-ip`, `/api/seed`, or a TCP probe — before SSDP has ever identified it, the server gives it a provisional ID derived from its address, like `yeelight_192.168.1.7`. The moment SSDP sees that bulb for the first time, the provisional ID is **replaced by the hardware ID**, permanently. From then on, the hardware ID is what the cache holds and what the API answers to.

So the rule is simple:

> Discover the hardware ID from `/api/devices` or `/api/metadata`, then pin to it. Treat `yeelight_<ip>` as a transient bootstrap value that can upgrade out from under you.

A few consequences worth keeping in mind:

- **Don’t cache the placeholder downstream.** A federation peer (for example, a `matter_webcontrol` logical bridge) that registers a bulb during the `yeelight_<ip>` window will hold a stale ID once the bulb upgrades to its hardware ID. Its calls then 404. The fix is to re-pull this server’s device list — on the matter side, re-register the bridge with `/api/bridge?ip=…&port=…`, which refreshes the cache.
- **Aliases are for people, not for lookups.** Names you add with `/api/name` show up in `names[]` for display. They are **not** accepted as IDs — the server resolves commands against the canonical ID only.
- **A factory reset is the one thing that changes a hardware ID.** Resetting a bulb gives it a new identifier. If you reset one, update whatever you’ve pinned and refresh your peers.

---

## How it works

```
┌─────────────────────┐                ┌──────────────────────┐
│  matter-srv         │                │  REST clients        │
│  (federation peer)  │                │  curl / app          │
└──────────┬──────────┘                └──────────┬───────────┘
           │ X-API-Key                            │ X-API-Key
           ▼                                      ▼
        ┌────────────────────────────────────────────────┐
        │  yeelight-srv (FastAPI)                        │
        │  ┌─────────────┐    ┌────────────────────┐     │
        │  │ SSDP cache  │    │ TCP/55443 probe    │     │
        │  └──────┬──────┘    └──────────┬─────────┘     │
        └─────────┼──────────────────────┼───────────────┘
                  ▼                      ▼
             yeelight bulbs         /24 subnet scan
              (port 55443)              (fallback)
```

The server keeps a small `cache.json` of every bulb it knows — hardware ID, IP, model, and last-seen state. SSDP populates it when bulbs are broadcasting; a TCP probe fills the gaps when they aren’t. Either way, the federation feed (`/api/devices`, `/api/metadata`) mirrors the schema `matter_webcontrol`’s `LogicalBridgeClient` consumes: `states.{on_off, brightness_raw, color_temp_mireds}`, `names: list[str]`, and `bridge.api_version: "2"`.

One header handles authentication. The same `X-API-Key` you set here is the key a federation peer sends when it registers this server with `/api/bridge?…&api_key=…`.

---

## CLI options

| Flag | Default | What it does |
|---|---|---|
| `--port` | `9800` | REST port |
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on the LAN — set an API key when you do |
| `--api-key` | `$YEELIGHT_SRV_KEY` | Required header value. If it’s unset while bound to `0.0.0.0`, the server logs a warning |
| `--seed-ip` | _(none)_ | Register a bulb by IP at startup. Repeatable, or comma-separated. Also reads `$YEELIGHT_SEED_IPS` |
| `--probe-subnet` | _(none)_ | TCP-scan a CIDR (e.g. `192.168.1.0/24`) at startup. Repeatable |
| `--auto-probe` | off | When SSDP returns no bulbs **and** the cache is empty, TCP-scan the local /24 on the first request |

> **Seeding doesn’t pin the ID.** `--seed-ip` and `--probe-subnet` are about *reaching* a bulb when SSDP is silent — they get it into the cache. They don’t change the rule above: once SSDP identifies the bulb, its hardware ID takes over. See [How devices are identified](#how-devices-are-identified).

---

## REST API

Every endpoint takes `X-API-Key: $YEELIGHT_SRV_KEY` when `--api-key` is set.

Address a bulb by its `id` — the hardware ID from `/api/devices` or `/api/metadata`. Aliases set with `/api/name` are display-only and are not accepted as IDs.

Errors map cleanly: `404` device unknown, `400` bad parameters, `401` auth, `500` everything else.

### Read

| Method & Path | Description |
|---|---|
| `GET /api/devices` | Federation feed — every device with `id`, `endpoint_id`, `states`, `names` |
| `GET /api/lights` | The human view: kelvin and percent, with IP and primary alias |
| `GET /api/level?id=…` | Read raw brightness (0–254). Add `&level=N` (or POST) to set |
| `GET /api/mired?id=…` | Read color temperature in mireds. Add `&mireds=N` (or POST) to set |
| `GET /api/metadata` | Declarative bridge info (`bridge.api_version: "2"`, capabilities, states) |

### Control

| Method & Path | Body / Params |
|---|---|
| `POST /api/set` | `{"id":"…","brightness":0.0–1.0,"temperature":Kelvin}` — both fields optional |
| `POST /api/level` | `{"id":"…","level":0–254}` |
| `POST /api/mired` | `{"id":"…","mireds":153–500}` (clamped to the Matter range) |

```bash
# 80% warm white, addressed by hardware ID
curl -H "X-API-Key: $YEELIGHT_SRV_KEY" \
  -X POST -H "Content-Type: application/json" \
  -d '{"id":"0x000000003415e584","brightness":0.8,"temperature":2700}' \
  http://127.0.0.1:9800/api/set
```

A `/api/set` brightness below `0.01` switches on Yeelight Moonlight, where the bulb supports it.

### Management

| Method & Path | Params |
|---|---|
| `POST /api/name` | `{"id":"…","name":"…"}` — append a display alias (you can add several) |
| `GET /api/name/remove?id=&name=` | Remove a single alias |
| `GET /api/refresh` | Re-probe every cached IP |
| `GET /api/seed?ips=192.168.1.7,192.168.1.236` | Add IPs to the cache without SSDP |
| `GET /api/probe?subnet=192.168.1.0/24` | TCP-scan a CIDR (omit `subnet` to scan the local /24) |

---

## Federate with matter_webcontrol

Pair this server with a running `matter-srv`, and your Yeelight bulbs join your Matter devices in one place:

```bash
# On the Yeelight bridge host (say 10.0.0.20)
export YEELIGHT_SRV_KEY=keyY
yeelight-srv --host 0.0.0.0 --port 9800 --auto-probe

# On the matter-srv host — register this bridge as a logical peer
curl -H "X-API-Key: $MATTER_SRV_KEY" \
  "http://127.0.0.1:8080/api/bridge?ip=10.0.0.20&port=9800&api_key=keyY"

# matter-srv now serves the Yeelight bulbs through its own endpoints
curl -H "X-API-Key: $MATTER_SRV_KEY" http://127.0.0.1:8080/api/lights
```

`matter-srv`’s `LogicalBridgeClient` calls `/api/devices`, `/api/level`, `/api/mired`, and `/api/set` directly over HTTP — no script execution, no `events` blobs.

> Registering a bridge also **refreshes** its device list. If a bulb’s ID has upgraded from `yeelight_<ip>` to its hardware ID since you last registered, re-run the `/api/bridge` call to pick up the change.

---

## Tests

The suite drives a real bulb, so it’s skipped by default until you provide an IP.

```bash
pip install -e '.[test]'

export YEELIGHT_TEST_IP=192.168.1.7         # required
export YEELIGHT_TEST_ID=0x000000002ce4355f  # optional; auto-discovered if unset
export YEELIGHT_TEST_KEY=optional-secret    # optional; used by the live-server auth test

pytest -v
```

It covers `/api/devices`, the v2 metadata schema, level (set / get / clamp), mireds (set / get / clamp), `/api/set` float brightness, alias add / remove, `X-API-Key` auth, and an end-to-end pass that mirrors the exact calls `LogicalBridgeClient` makes.

---

## Known issues

- **SSDP goes quiet on long-running bulbs.** Yeelight bulbs broadcast SSDP `NOTIFY` only for a short window after power-on. After that, `discover_bulbs()` returns `[]` even though TCP/55443 still answers. Reach them with `--auto-probe`, `--seed-ip`, or `/api/probe`.
- **Bulbs occasionally drop off the LAN (roughly every two weeks).** A bulb stops answering on TCP/55443 and ignores commands. The only known recovery is a hardware power-cycle — cut mains for about 10 seconds, then restore. The Yeelight LAN protocol exposes no reboot or reset command, so this can’t be done from the web interface; it needs a smart plug upstream or someone at the wall switch.
- **One subnet per host.** `--auto-probe` and `/api/probe` (without `subnet=`) scan only the /24 of each local interface. For multi-VLAN setups, pass `--probe-subnet` explicitly per network.

---

## Limitations

- Only color-temperature and dimmable LAN-control bulbs are tested. RGB-only models aren’t exercised by the current control mappings.
- The Yeelight LAN protocol is unauthenticated on the wire. `--api-key` guards the REST front, not the bulb’s TCP port — anyone on the same L2 segment can still drive a bulb directly on port 55443.
