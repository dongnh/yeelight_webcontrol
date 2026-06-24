"""Yeelight Web Controller — federation-compatible REST bridge.

Implements the same API contract that matter_webcontrol v0.25.0
LogicalBridgeClient expects, so a Matter server can register this
service via `/api/bridge?ip=&port=&api_key=` and federate Yeelight
bulbs alongside its native Matter devices.
"""

import argparse
import ipaddress
import json
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from yeelight import Bulb, Flow, LightType, PowerMode, discover_bulbs
try:
    from yeelight.transitions import TemperatureTransition, SleepTransition
except ImportError:  # older layouts expose them from yeelight.flow
    from yeelight.flow import TemperatureTransition, SleepTransition

try:  # models with a physical night-light (moonlight) channel
    from yeelight.main import _MODEL_SPECS as _YEE_MODEL_SPECS
    _MOONLIGHT_MODELS = frozenset(
        m for m, s in _YEE_MODEL_SPECS.items() if s.get("night_light")
    )
except Exception:  # pragma: no cover - defensive: upstream layout changed
    logging.warning("yeelight._MODEL_SPECS unavailable; moonlight disabled for all models")
    _MOONLIGHT_MODELS = frozenset()

CACHE_FILE = "cache.json"
NAMES_FILE = "names.json"

MIRED_MIN, MIRED_MAX = 153, 500  # Matter ColorControl spec range
DEFAULT_PORT = 9800
MOONLIGHT_THRESHOLD = 0.01  # brightness < 1% triggers moonlight where supported
YEELIGHT_PORT = 55443  # Yeelight LAN protocol TCP port
PROBE_TIMEOUT = 0.4    # per-host TCP connect timeout during subnet scan
PROBE_WORKERS = 64     # parallel scan workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

app = FastAPI(title="Yeelight Web Controller")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Read error {path}: {e}")
        return {}


def save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logging.error(f"Write error {path}: {e}")


def load_names() -> dict[str, list[str]]:
    """Load aliases. Migrates legacy `id -> str` to `id -> [str]`."""
    raw = load_json(NAMES_FILE)
    migrated: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            migrated[k] = [str(n) for n in v]
        elif v:
            migrated[k] = [str(v)]
    return migrated


# ---------------------------------------------------------------------------
# Yeelight helpers
# ---------------------------------------------------------------------------

def _kelvin_to_mireds(kelvin: int) -> int:
    return int(1_000_000 / kelvin) if kelvin > 0 else 0


def _pct_to_raw(pct: float) -> int:
    return max(0, min(254, int(round(pct / 100.0 * 254))))


def _raw_to_pct(raw: int) -> int:
    return max(0, min(100, int(round(raw / 254.0 * 100))))


def _read_props(ip: str) -> Optional[dict]:
    try:
        return Bulb(ip).get_properties(
            ["power", "bright", "ct", "active_mode"]
        )
    except Exception:
        return None


def _build_device_entry(ip: str, bulb_id: str, model: str,
                        props: dict, names: list[str]) -> dict:
    bright_pct = int(props.get("bright") or 0)
    ct_kelvin = int(props.get("ct") or 0)
    on = (props.get("power") == "on")
    # active_mode == "1" -> the bulb is on its night-light (moonlight) channel,
    # whose real level lives in nl_br, not bright. Report "on at the floor" so the
    # feed never shows the stale daylight brightness, and drop colour temperature
    # (moonlight is a fixed warm white).
    moonlit = on and props.get("active_mode") == "1"

    states: dict[str, Any] = {
        "on_off": on,
        "brightness_raw": 1 if moonlit else (_pct_to_raw(bright_pct) if on else 0),
    }
    if ct_kelvin > 0 and not moonlit:
        states["color_temp_mireds"] = _kelvin_to_mireds(ct_kelvin)

    return {
        "id": bulb_id,
        "endpoint_id": 1,
        "ip": ip,
        "model": model,
        "names": names,
        "states": states,
    }


def _local_ipv4_subnets() -> list[str]:
    """Best-effort enumerate /24 subnets of the local IPv4 interfaces.

    Falls back to whichever IP a UDP probe socket picks for an external dest.
    """
    nets: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        net = ipaddress.ip_interface(f"{ip}/24").network
        nets.add(str(net))
    except Exception:
        pass

    # Also try parsing `ifconfig` output for additional private interfaces.
    try:
        import subprocess
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("inet "):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            ip = parts[1]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.is_loopback or not addr.is_private or addr.version != 4:
                continue
            nets.add(str(ipaddress.ip_interface(f"{ip}/24").network))
    except Exception:
        pass

    return sorted(nets)


def _probe_host(ip: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """TCP probe a single host on the Yeelight LAN port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, YEELIGHT_PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def probe_subnet(subnet: str) -> list[str]:
    """Parallel TCP scan of `subnet` for hosts answering on port 55443."""
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError as e:
        raise ValueError(f"Invalid subnet '{subnet}': {e}")

    hosts = [str(h) for h in net.hosts()]
    found: list[str] = []
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for ip, ok in zip(hosts, pool.map(_probe_host, hosts)):
            if ok:
                found.append(ip)
    return found


def probe_and_seed(subnets: Optional[list[str]] = None) -> dict[str, dict]:
    """Probe local /24 subnet(s) for Yeelight bulbs and seed any found.

    Used as a fallback when SSDP multicast discovery returns nothing
    (bulbs only emit SSDP NOTIFY for a short window after power-on).
    """
    targets = subnets or _local_ipv4_subnets()
    if not targets:
        logging.warning("Probe: could not determine a local subnet")
        return {}
    discovered: list[str] = []
    for subnet in targets:
        logging.info(f"Probe: scanning {subnet} for port {YEELIGHT_PORT}")
        try:
            discovered.extend(probe_subnet(subnet))
        except ValueError as e:
            logging.error(str(e))
    if not discovered:
        logging.info("Probe: no Yeelight devices answering on port 55443")
        return {}
    return seed_ips(discovered)


def seed_ips(ips: list[str]) -> dict[str, dict]:
    """Probe each given IP directly and add it to the cache.

    Useful when SSDP multicast discovery is blocked or bulbs have been
    powered on too long to broadcast their SSDP NOTIFY frames.
    """
    cache = load_json(CACHE_FILE)
    names = load_names()
    socket.setdefaulttimeout(3)
    added = {}
    for ip in ips:
        props = _read_props(ip)
        if not props:
            logging.warning(f"Seed: {ip} unreachable on port 55443")
            continue
        existing = cache.get(ip, {})
        # Keep an already-known id (a hardware id, once SSDP has identified the
        # bulb), otherwise assign the provisional `yeelight_<ip>` placeholder.
        # That placeholder is transient: the next refresh that sees the bulb
        # over SSDP replaces it with the bulb's permanent hardware id. Address
        # bulbs by the hardware id, not by this seeded value.
        bulb_id = existing.get("id") or f"yeelight_{ip}"
        model = existing.get("model") or "unknown"
        cache[ip] = _build_device_entry(ip, bulb_id, model, props, names.get(bulb_id, []))
        added[ip] = cache[ip]
        logging.info(f"Seed: registered {ip} as {bulb_id}")
    socket.setdefaulttimeout(10)
    if added:
        save_json(CACHE_FILE, cache)
    return added


def _refresh_devices(timeout: int = 2, allow_probe: bool = True) -> dict[str, dict]:
    """Discover + probe every known/discovered bulb. Persists `cache.json`.

    When SSDP discovery returns nothing AND the cache is empty, fall back to a
    TCP scan of the local /24 subnet(s) — bulbs that have been on for a while
    stop broadcasting SSDP NOTIFY frames.
    """
    known = load_json(CACHE_FILE)
    names = load_names()

    discovered = discover_bulbs(timeout=timeout)
    by_ip = {b["ip"]: b for b in discovered if b.get("ip")}

    if allow_probe and not by_ip and not known and getattr(app.state, "auto_probe", False):
        logging.info("SSDP returned 0 bulbs and cache is empty — running TCP probe")
        probe_and_seed()
        known = load_json(CACHE_FILE)

    all_ips = set(known.keys()) | set(by_ip.keys())

    socket.setdefaulttimeout(3)
    active: dict[str, dict] = {}
    for ip in all_ips:
        props = _read_props(ip)
        if not props:
            continue
        caps = by_ip.get(ip, {}).get("capabilities") or {}
        # Canonical identity, in priority order:
        #   1. SSDP capabilities.id — the bulb's permanent hardware id (hex).
        #   2. The id already in the cache (a hardware id once SSDP has seen it).
        #   3. `yeelight_<ip>` — provisional fallback before SSDP ever identifies
        #      the bulb. As soon as (1) is available it wins and is persisted, so
        #      a seeded `yeelight_<ip>` upgrades to the hardware id permanently.
        bulb_id = caps.get("id") or known.get(ip, {}).get("id") or f"yeelight_{ip}"
        model = caps.get("model") or known.get(ip, {}).get("model") or "unknown"
        active[ip] = _build_device_entry(ip, bulb_id, model, props, names.get(bulb_id, []))
    socket.setdefaulttimeout(10)

    if active != known:
        save_json(CACHE_FILE, active)
    return active


def _find_by_id(device_id: str, cache: Optional[dict] = None) -> Optional[dict]:
    """Resolve a device by its canonical `id` (the bulb's hardware id).

    Matches the canonical id only. Aliases registered via `/api/name` live in
    `names[]` for display and are deliberately not resolvable as ids.
    """
    cache = cache if cache is not None else load_json(CACHE_FILE)
    for entry in cache.values():
        if entry.get("id") == device_id:
            return entry
    return None


def _bulb_for(device_id: str) -> tuple[Bulb, dict]:
    entry = _find_by_id(device_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")
    return Bulb(entry["ip"]), entry


def _supports_moonlight(model: Optional[str]) -> bool:
    """True only for models with a night-light channel (ceiling lights, bslamp2/3).

    Gating on the cached SSDP model is the correct probe. The old
    `"active_mode" in props` check was always true — get_prop returns "" for
    unrecognised keys — so it switched non-night-light bulbs into mode 5.
    """
    return bool(model) and model in _MOONLIGHT_MODELS


def _moonlight_capable(bulb: Bulb, entry: dict) -> bool:
    """Model-based moonlight check, with a live probe for unidentified bulbs.

    A known night-light model wins immediately; a known non-night-light model is
    rejected without a round-trip. For an "unknown"/unset model (a bulb seeded by
    IP before SSDP identified it) fall back to a value-based active_mode probe: a
    non-empty active_mode ("0"/"1") proves the bulb has the night-light channel.
    """
    model = entry.get("model")
    if _supports_moonlight(model):
        return True
    if model and model != "unknown":
        return False
    try:
        props = bulb.get_properties(["active_mode"]) or {}
        return props.get("active_mode") in ("0", "1")
    except Exception:
        return False


_bulb_locks: dict[str, threading.Lock] = {}
_bulb_locks_guard = threading.Lock()


def _lock_for(ip: str) -> threading.Lock:
    """Per-bulb write lock: base, moonlight, and flow share one socket on 55443."""
    with _bulb_locks_guard:
        lock = _bulb_locks.get(ip)
        if lock is None:
            lock = threading.Lock()
            _bulb_locks[ip] = lock
        return lock


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    api_key = getattr(app.state, "api_key", None)
    if api_key and request.headers.get("X-API-Key") != api_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic payloads (federation calls POST with JSON bodies)
# ---------------------------------------------------------------------------

class ControlPayload(BaseModel):
    id: str
    brightness: Optional[float] = None
    temperature: Optional[int] = None  # kelvin


class LevelPayload(BaseModel):
    id: str
    level: Optional[int] = None


class MiredPayload(BaseModel):
    id: str
    mireds: Optional[int] = None


class FlowPayload(BaseModel):
    id: str
    base: int = 25            # overcast brightness (1-100); caller derives from schedule x scale
    peak: int = 100           # lightning-flash brightness (1-100); caller's scheduled level
    kelvin: int = 4500        # overcast colour temperature
    lightning: bool = False   # add lightning flashes (heavy/violent rain)
    flash_kelvin: int = 6000  # cool colour of the lightning flash


class NamePayload(BaseModel):
    id: Optional[str] = None
    bulb_id: Optional[str] = None  # legacy
    name: str


def _params(request: Request, payload, fields: list[str]) -> dict:
    if request.method == "POST" and payload is not None:
        return {f: getattr(payload, f, None) for f in fields}
    qp = request.query_params
    out: dict[str, Any] = {}
    for f in fields:
        out[f] = qp.get(f) if f != "id" else (qp.get("id") or qp.get("bulb_id"))
    return out


# ---------------------------------------------------------------------------
# Federation endpoints (matter_webcontrol v0.25 contract)
# ---------------------------------------------------------------------------

@app.get("/api/devices")
def get_devices() -> list[dict]:
    """Federation peers consume this. Returns full device list with states."""
    return list(_refresh_devices().values())


@app.get("/api/lights")
def get_lights() -> dict:
    """Legacy + human-friendly view (kelvin + percent)."""
    cache = _refresh_devices()
    out = []
    for entry in cache.values():
        states = entry["states"]
        mireds = states.get("color_temp_mireds", 0)
        out.append({
            "ip": entry["ip"],
            "id": entry["id"],
            "model": entry["model"],
            "name": entry["names"][0] if entry["names"] else "Unknown",
            "names": entry["names"],
            "temperature_k": int(1_000_000 / mireds) if mireds else 0,
            "brightness_pct": _raw_to_pct(states.get("brightness_raw", 0)),
            "state": states.get("on_off", False),
        })
    return {"status": "success", "data": out}


@app.get("/api/refresh")
def refresh() -> dict:
    count = len(_refresh_devices())
    return {"status": "success", "message": f"Refreshed {count} devices"}


@app.get("/api/probe")
def probe_endpoint(subnet: Optional[str] = None):
    """TCP-scan a subnet (default: local /24) for bulbs answering on 55443."""
    subnets = [subnet] if subnet else None
    added = probe_and_seed(subnets)
    return {
        "status": "success",
        "scanned": subnets or _local_ipv4_subnets(),
        "added": [{"ip": ip, "id": entry["id"]} for ip, entry in added.items()],
    }


@app.get("/api/seed")
def seed_endpoint(ips: str):
    """Add one or more comma-separated IPs to the cache without SSDP discovery."""
    ip_list = [s.strip() for s in ips.split(",") if s.strip()]
    if not ip_list:
        raise HTTPException(status_code=400, detail="No IPs provided")
    added = seed_ips(ip_list)
    return {
        "status": "success",
        "added": [{"ip": ip, "id": entry["id"]} for ip, entry in added.items()],
        "skipped": [ip for ip in ip_list if ip not in added],
    }


# -- Brightness/temperature combo (brightness is 0-1 float) -----------------

@app.api_route("/api/set", methods=["GET", "POST"])
def set_device(request: Request, payload: Optional[ControlPayload] = None):
    p = _params(request, payload, ["id", "brightness", "temperature"])
    if not p["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    bulb, entry = _bulb_for(p["id"])
    brightness = float(p["brightness"]) if p["brightness"] is not None else None
    temperature = int(p["temperature"]) if p["temperature"] is not None else None

    try:
        if brightness is not None:
            brightness = max(0.0, min(1.0, brightness))
            if brightness == 0.0:
                bulb.turn_off()
            elif brightness < MOONLIGHT_THRESHOLD:
                # Moonlight where supported, otherwise the lowest normal level.
                # Gate on the cached model — not a get_prop probe (see
                # _supports_moonlight). Mode BEFORE brightness so set_brightness's
                # ensure_on() doesn't re-power in NORMAL and write `bright`.
                with _lock_for(entry["ip"]):
                    bulb.turn_on()
                    if _supports_moonlight(entry.get("model")):
                        bulb.set_power_mode(PowerMode.MOONLIGHT)
                    bulb.set_brightness(max(1, int(brightness * 100)), duration=1000)
            else:
                bulb.turn_on()
                if _supports_moonlight(entry.get("model")):
                    bulb.set_power_mode(PowerMode.NORMAL)
                bulb.set_brightness(int(brightness * 100), duration=1000)

        if temperature is not None and temperature > 0:
            bulb.turn_on()
            # Clamp via mireds spec range, then convert back.
            mireds = max(MIRED_MIN, min(MIRED_MAX, _kelvin_to_mireds(temperature)))
            kelvin = int(1_000_000 / mireds)
            bulb.set_color_temp(kelvin, duration=1000)

        return {"status": "success", "id": p["id"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Level (0-254 raw) ------------------------------------------------------

@app.api_route("/api/level", methods=["GET", "POST"])
def level(request: Request, payload: Optional[LevelPayload] = None):
    p = _params(request, payload, ["id", "level"])
    if not p["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    bulb, entry = _bulb_for(p["id"])

    if p["level"] is None:
        try:
            props = bulb.get_properties(["bright", "power"]) or {}
            on = props.get("power") == "on"
            bright_pct = int(props.get("bright") or 0)
            return {"id": p["id"], "level": _pct_to_raw(bright_pct) if on else 0}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raw = max(0, min(254, int(p["level"])))
    try:
        if raw == 0:
            bulb.turn_off()
        else:
            with _lock_for(entry["ip"]):
                bulb.turn_on()
                # Driving the main channel must leave moonlight; the raw path has
                # no other way to clear mode 5 on a night-light-capable bulb.
                if _supports_moonlight(entry.get("model")):
                    bulb.set_power_mode(PowerMode.NORMAL)
                bulb.set_brightness(max(1, _raw_to_pct(raw)), duration=1000)
        return {"status": "success", "id": p["id"], "level": raw}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Mireds (153-500, plural key per v0.25) ---------------------------------

@app.api_route("/api/mired", methods=["GET", "POST"])
def mired(request: Request, payload: Optional[MiredPayload] = None):
    p = _params(request, payload, ["id", "mireds"])
    # Backward compat: also accept singular `mired` query param
    if not p["mireds"] and request.method == "GET":
        p["mireds"] = request.query_params.get("mired")

    if not p["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    bulb, _ = _bulb_for(p["id"])

    if p["mireds"] is None:
        try:
            props = bulb.get_properties(["ct"]) or {}
            kelvin = int(props.get("ct") or 0)
            return {"id": p["id"], "mireds": _kelvin_to_mireds(kelvin)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    mireds_val = max(MIRED_MIN, min(MIRED_MAX, int(p["mireds"])))
    try:
        kelvin = int(1_000_000 / mireds_val)
        bulb.turn_on()
        bulb.set_color_temp(kelvin, duration=1000)
        return {"status": "success", "id": p["id"], "mireds": mireds_val}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Moonlight (night-light channel; ceiling lights + Bedside Lamp 2/3) ------

class MoonlightPayload(BaseModel):
    id: str
    on: bool = True
    level: Optional[int] = None  # nl_br 1-100 (night-light brightness)


@app.api_route("/api/moonlight", methods=["POST"])
def moonlight(request: Request, payload: Optional[MoonlightPayload] = None):
    """Enter or leave the bulb's moonlight (night-light) channel.

    light_programmer drives this directly (like /api/flow) for schedule points
    in the sub-1 band (0 < level < 1 on the 0-100 scale), mapped linearly to
    nl_br: level 0.1 -> 10%, 0.9 -> 90% (the `level` field here IS that nl_br,
    1-100). Only ceiling lights and Bedside Lamp 2/3 have the channel; other
    models fall back to the lowest normal brightness so the caller still gets a
    dim light.
    """
    p = _params(request, payload, ["id", "on", "level"])
    if not p["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")

    on = p["on"]
    if isinstance(on, str):
        on = on.strip().lower() not in ("0", "false", "off", "no", "")
    on = True if on is None else bool(on)

    bulb, entry = _bulb_for(p["id"])
    supported = _moonlight_capable(bulb, entry)
    try:
        with _lock_for(entry["ip"]):
            # A running main-channel colour-flow owns the bulb; stop it before
            # switching power mode or the bulb is left in an undefined state.
            try:
                bulb.stop_flow(light_type=LightType.Main)
            except Exception:
                pass
            if not on:
                bulb.turn_on()
                bulb.set_power_mode(PowerMode.NORMAL)
                return {"status": "success", "id": p["id"], "moonlight": False}
            nl = 1 if p["level"] is None else max(1, min(100, int(p["level"])))
            eff = nl if supported else 1  # no night-light channel -> lowest normal
            bulb.turn_on()
            if supported:
                # Mode BEFORE brightness (ensure_on would re-power in NORMAL).
                bulb.set_power_mode(PowerMode.MOONLIGHT)
            bulb.set_brightness(eff, duration=1000)
        return {"status": "success", "id": p["id"],
                "moonlight": supported, "level": eff}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Colour flow (on-device animation, e.g. rain/overcast/lightning) ---------

FLOW_K_MIN, FLOW_K_MAX = 1700, 6500  # Yeelight tunable-white range (Kelvin)


def _clampb(v: int) -> int:
    return max(1, min(100, int(round(v))))


def _clampk(v: int) -> int:
    return max(FLOW_K_MIN, min(FLOW_K_MAX, int(v)))


def _build_rain_flow(base: int, peak: int, kelvin: int,
                     lightning: bool, flash_kelvin: int) -> Flow:
    """An overcast-sky animation on the main (white) channel.

    Brightness is absolute (Yeelight flows can't scale), so the CALLER passes
    `base` (= scheduled level x rain intensity_scale) and `peak` (= the scheduled
    level) — both already synced to light_programmer's circadian schedule. The
    flow shivers quickly and irregularly around `base` like rain on an overcast
    skylight — many short steps, biased downward (mostly dimmer than `base`, with
    brief lifts) — and, when `lightning`, flashes up toward `peak` in a cool
    colour, then settles back with a dark pause. Runs forever (count=0) until
    stopped.
    """
    b = _clampb(base)
    lo = _clampb(b * 0.55)

    def step(factor: float, ms: int) -> TemperatureTransition:
        return TemperatureTransition(kelvin, duration=ms, brightness=_clampb(b * factor))

    # Short, uneven steps -> the light "rains" rather than slowly breathing.
    # Factors stay mostly < 1 (overcast is dim) with the odd brief brightening;
    # ~6 s irregular loop with 8 brightness changes (vs 3 over ~9.5 s before).
    t = [
        step(0.72, 800),
        step(1.08, 520),
        step(0.58, 900),
        step(0.90, 480),
        step(0.66, 820),
        step(1.02, 560),
        step(0.55, 880),
        step(0.84, 640),
    ]
    if lightning:
        p = _clampb(peak)
        t += [
            TemperatureTransition(flash_kelvin, duration=70,  brightness=p),
            TemperatureTransition(flash_kelvin, duration=110, brightness=_clampb(b * 1.3)),
            TemperatureTransition(flash_kelvin, duration=55,  brightness=_clampb(p * 0.85)),
            TemperatureTransition(kelvin,       duration=300, brightness=lo),
            SleepTransition(duration=2800),
        ]
    return Flow(count=0, action=Flow.actions.stay, transitions=t)


@app.api_route("/api/flow", methods=["POST"])
def start_flow(request: Request, payload: Optional[FlowPayload] = None):
    p = _params(request, payload, ["id", "base", "peak", "kelvin", "lightning", "flash_kelvin"])
    if not p["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")
    bulb, entry = _bulb_for(p["id"])
    flow = _build_rain_flow(
        base=int(p.get("base") or 25),
        peak=int(p.get("peak") or 100),
        kelvin=_clampk(int(p.get("kelvin") or 4500)),
        lightning=bool(p.get("lightning")),
        flash_kelvin=_clampk(int(p.get("flash_kelvin") or 6000)),
    )
    try:
        with _lock_for(entry["ip"]):
            bulb.turn_on()
            bulb.start_flow(flow, light_type=LightType.Main)
        return {"status": "success", "id": p["id"], "flowing": True,
                "base": int(p.get("base") or 25), "lightning": bool(p.get("lightning"))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.api_route("/api/flow/stop", methods=["POST"])
def stop_flow(request: Request, payload: Optional[FlowPayload] = None):
    p = _params(request, payload, ["id"])
    if not p["id"]:
        raise HTTPException(status_code=400, detail="Missing device id")
    bulb, entry = _bulb_for(p["id"])
    try:
        with _lock_for(entry["ip"]):
            bulb.stop_flow(light_type=LightType.Main)
        return {"status": "success", "id": p["id"], "flowing": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Aliases ----------------------------------------------------------------

@app.api_route("/api/name", methods=["GET", "POST"])
def set_name(request: Request, payload: Optional[NamePayload] = None):
    p = _params(request, payload, ["id", "name"])
    device_id = p["id"]
    name = p["name"]
    if not device_id or not name:
        raise HTTPException(status_code=400, detail="Missing id or name")

    names = load_names()
    aliases = names.get(device_id, [])
    if name not in aliases:
        aliases.append(name)
    names[device_id] = aliases
    save_json(NAMES_FILE, names)
    return {"status": "success", "id": device_id, "names": aliases}


@app.get("/api/name/remove")
def remove_name(id: str, name: str):
    names = load_names()
    aliases = names.get(id, [])
    if name not in aliases:
        raise HTTPException(status_code=404, detail=f"Alias '{name}' not on {id}")
    aliases.remove(name)
    if aliases:
        names[id] = aliases
    else:
        names.pop(id, None)
    save_json(NAMES_FILE, names)
    return {"status": "success", "id": id, "names": aliases}


# -- Metadata (v2 schema, no embedded scripts) ------------------------------

@app.get("/api/metadata")
def metadata(request: Request) -> dict:
    host = request.url.hostname or "127.0.0.1"
    port = request.url.port or DEFAULT_PORT

    cache = load_json(CACHE_FILE) or _refresh_devices()
    devices = []
    for entry in cache.values():
        states = entry.get("states", {})
        capabilities = []
        if "on_off" in states:
            capabilities.append("on_off")
        if "brightness_raw" in states:
            capabilities.append("brightness")
        if "color_temp_mireds" in states:
            capabilities.append("color_temperature")

        if "color_temp_mireds" in states:
            hw_type = "color_temperature_light"
        elif "brightness_raw" in states:
            hw_type = "dimmable_light"
        else:
            hw_type = "on_off_light"

        names = entry.get("names", [])
        devices.append({
            "id": entry["id"],
            "name": names[0] if names else entry["id"],
            "names": names,
            "hardware_type": hw_type,
            "capabilities": capabilities,
            "states": states,
        })

    return {
        "bridge": {
            "id": "yeelight_bridge_http",
            "type": "lighting_controller",
            "network_host": host,
            "network_port": port,
            "api_version": "2",
        },
        "devices": devices,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Yeelight Local Control API")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", type=str, default="127.0.0.1",
                        help="Bind address (default 127.0.0.1; use 0.0.0.0 to expose on LAN)")
    parser.add_argument("--api-key", type=str, default=os.environ.get("YEELIGHT_SRV_KEY"),
                        help="Require X-API-Key header (or set YEELIGHT_SRV_KEY)")
    parser.add_argument("--seed-ip", action="append", default=[],
                        help="Pre-register a bulb by IP without SSDP discovery. "
                             "Repeatable, or pass a comma-separated list. "
                             "Also reads YEELIGHT_SEED_IPS env var.")
    parser.add_argument("--probe-subnet", action="append", default=[],
                        help="On startup, TCP-scan this subnet (e.g. 192.168.1.0/24) "
                             "for bulbs on port 55443. Repeatable. If omitted but "
                             "--auto-probe is set, the local /24 is scanned.")
    parser.add_argument("--auto-probe", action="store_true",
                        help="When SSDP returns no bulbs and the cache is empty, "
                             "automatically TCP-scan the local /24 subnet.")
    args = parser.parse_args()

    seeds: list[str] = []
    for entry in args.seed_ip:
        seeds.extend(s.strip() for s in entry.split(",") if s.strip())
    env_seeds = os.environ.get("YEELIGHT_SEED_IPS", "")
    seeds.extend(s.strip() for s in env_seeds.split(",") if s.strip())
    if seeds:
        added = seed_ips(seeds)
        logging.info(f"Seeded {len(added)}/{len(seeds)} bulbs from --seed-ip")

    if args.probe_subnet:
        added = probe_and_seed(args.probe_subnet)
        logging.info(f"Probe: registered {len(added)} bulb(s) from --probe-subnet")

    app.state.auto_probe = args.auto_probe

    if args.host != "127.0.0.1" and not args.api_key:
        logging.warning(
            "Bound to %s without --api-key. Anyone on the LAN can control your bulbs. "
            "Set YEELIGHT_SRV_KEY or pass --api-key.", args.host,
        )

    app.state.api_key = args.api_key
    logging.info(f"Starting service on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
