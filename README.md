# Yeelight Web Controller

A Python REST bridge that exposes Yeelight LAN-control bulbs through the same wire contract as a [`matter_webcontrol`](https://github.com/dongnh/matter_webcontrol) v0.25 logical bridge. A Matter server can register this service and federate Yeelight bulbs alongside its native Matter devices — no embedded scripts, no `exec()`.

- **REST** — control bulbs by IP-derived ID, get / set Matter-style level (0-254) and mireds (153-500)
- **Federation-ready** — `/api/devices` and `/api/metadata` (api_version `"2"`) match `LogicalBridgeClient`
- **Discovery resilient** — TCP probe fallback when SSDP multicast is blocked or bulbs stop broadcasting

---

## Quick start

```bash
pip install yeelight-web-controller   # or: pip install -e .

# Generate an API key (used by all clients via X-API-Key header)
export YEELIGHT_SRV_KEY=$(openssl rand -hex 32)

# Start the server (auto-probe LAN /24 if SSDP returns nothing)
yeelight-srv --host 0.0.0.0 --port 9800 --auto-probe

# In another terminal — talk to it
curl -H "X-API-Key: $YEELIGHT_SRV_KEY" http://127.0.0.1:9800/api/devices
```

Requires Python 3.12+. Bulbs must have **LAN Control** enabled in the Yeelight Classic app.

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

- Bulbs are identified by `capabilities.id` from SSDP, or `yeelight_<ip>` when SSDP is unavailable. Both forms are stable across server restarts via `cache.json`.
- `/api/devices` and `/api/metadata` mirror the schema that `matter_webcontrol`'s `LogicalBridgeClient` consumes (`states.{on_off, brightness_raw, color_temp_mireds}`, `names: list[str]`, `bridge.api_version: "2"`).
- Authentication is a single `X-API-Key` header. Same key used by federation peers when they register this server via `/api/bridge?…&api_key=…` on the matter side.

---

## CLI options

| Flag | Default | Description |
|---|---|---|
| `--port` | `9800` | REST port |
| `--host` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on LAN (api-key strongly recommended) |
| `--api-key` | `$YEELIGHT_SRV_KEY` | Required header value. If unset and `--host 0.0.0.0`, a warning is logged |
| `--seed-ip` | _(none)_ | Pre-register a bulb by IP at startup. Repeatable, or comma-separated. Also reads `$YEELIGHT_SEED_IPS` |
| `--probe-subnet` | _(none)_ | TCP-scan this CIDR (e.g. `192.168.1.0/24`) for bulbs at startup. Repeatable |
| `--auto-probe` | off | When SSDP returns no bulbs **and** the cache is empty, lazily TCP-scan the local /24 on first request |

---

## REST API

All endpoints require `X-API-Key: $YEELIGHT_SRV_KEY` (when `--api-key` is set).

Devices are addressed by their `id` field (the SSDP `capabilities.id`, or `yeelight_<ip>` if seeded without SSDP). Aliases set via `/api/name` are display-only and **not** accepted as IDs.

Error mapping: `404` (device unknown), `400` (bad parameters), `401` (auth), `500` (other).

### Read

| Method & Path | Description |
|---|---|
| `GET /api/devices` | Federation feed — every device with `id`, `endpoint_id`, `states`, `names` |
| `GET /api/lights` | Human view: kelvin + percent, includes IP and primary alias |
| `GET /api/level?id=…` | Read raw brightness (0-254). Add `&level=N` (or POST) to set |
| `GET /api/mired?id=…` | Read color temperature in mireds. Add `&mireds=N` (or POST) to set |
| `GET /api/metadata` | Declarative bridge info (`bridge.api_version: "2"`, capabilities + states) |

### Control

| Method & Path | Body / Params |
|---|---|
| `POST /api/set` | `{"id":"…","brightness":0.0–1.0,"temperature":Kelvin}` — both fields optional |
| `POST /api/level` | `{"id":"…","level":0–254}` |
| `POST /api/mired` | `{"id":"…","mireds":153–500}` (clamped to Matter spec) |

```bash
# 80 % warm white on a bulb
curl -H "X-API-Key: $YEELIGHT_SRV_KEY" \
  -X POST -H "Content-Type: application/json" \
  -d '{"id":"yeelight_192.168.1.7","brightness":0.8,"temperature":2700}' \
  http://127.0.0.1:9800/api/set
```

`/api/set` brightness `< 0.01` activates Yeelight Moonlight where the bulb supports it.

### Management

| Method & Path | Params |
|---|---|
| `POST /api/name` | `{"id":"…","name":"…"}` — append alias (multiple per device) |
| `GET /api/name/remove?id=&name=` | Remove a single alias |
| `GET /api/refresh` | Re-probe every cached IP |
| `GET /api/seed?ips=192.168.1.7,192.168.1.236` | Add IPs to cache without SSDP |
| `GET /api/probe?subnet=192.168.1.0/24` | TCP-scan a CIDR (subnet optional → local /24) |

---

## Federation with matter_webcontrol

Pair this server with a running `matter-srv` so Yeelight bulbs appear alongside native Matter devices:

```bash
# On the Yeelight bridge host (e.g. 10.0.0.20)
export YEELIGHT_SRV_KEY=keyY
yeelight-srv --host 0.0.0.0 --port 9800 --auto-probe

# On the matter-srv host — register this bridge as a logical peer
curl -H "X-API-Key: $MATTER_SRV_KEY" \
  "http://127.0.0.1:8080/api/bridge?ip=10.0.0.20&port=9800&api_key=keyY"

# matter-srv now exposes the Yeelight bulbs through its own endpoints:
curl -H "X-API-Key: $MATTER_SRV_KEY" http://127.0.0.1:8080/api/lights
```

`matter-srv`'s `LogicalBridgeClient` calls `/api/devices`, `/api/level`, `/api/mired`, and `/api/set` directly over plain HTTP — no script execution, no `events` blobs.

---

## Tests

The test suite drives a real bulb. Skipped by default unless an IP is provided.

```bash
pip install -e '.[test]'

export YEELIGHT_TEST_IP=192.168.1.7         # required
export YEELIGHT_TEST_ID=0x000000002ce4355f  # optional; auto-discovered if unset
export YEELIGHT_TEST_KEY=optional-secret    # optional; used by the live-server auth test

pytest -v
```

Coverage: `/api/devices`, metadata v2 schema, level (set / get / clamp), mireds (set / get / clamp), `/api/set` float brightness, alias add / remove, `X-API-Key` auth, plus an end-to-end test that mirrors the HTTP calls `LogicalBridgeClient` issues.

---

## Known issues

- **SSDP discovery returns empty for long-running bulbs.** Yeelight bulbs only emit SSDP `NOTIFY` frames for a short window after power-on. After that `discover_bulbs()` returns `[]` even though TCP/55443 still works. Use `--auto-probe`, `--seed-ip`, or `/api/probe` to bypass SSDP.
- **Bulbs occasionally drop off the LAN (~every 2 weeks).** Symptom: the bulb stops answering on TCP/55443 and ignores commands. The only known recovery is a hardware power-cycle (cut mains for ~10 s, then restore). The Yeelight LAN protocol does **not** expose a reboot / reset command, so this cannot be triggered from the web interface — recovery requires either a smart plug upstream of the bulb or someone flipping the wall switch.
- **One subnet per host.** `--auto-probe` and `/api/probe` (without `subnet=`) scan only the /24 of each local interface. Multi-VLAN setups should pass `--probe-subnet` explicitly per network.

---

## Limitations

- Only color-temperature and dimmable LAN-control Yeelight bulbs are tested. RGB-only models are not exercised by the current control mappings.
- The Yeelight LAN protocol is unauthenticated on the wire — `--api-key` only gates the REST front, not the bulb-side TCP. Anyone on the same L2 segment as a bulb can still control it directly on port 55443.
