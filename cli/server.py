"""Yeelight Web Controller — federation-compatible REST bridge.

Implements the same API contract that matter_webcontrol v0.25.0
LogicalBridgeClient expects, so a Matter server can register this
service via `/api/bridge?ip=&port=&api_key=` and federate Yeelight
bulbs alongside its native Matter devices.

Connection model (v0.8.0)
-------------------------
The bridge keeps every bulb warm instead of reconnecting per request:

  * Bulb pool          one persistent `yeelight.Bulb` per IP, so the LAN
                       socket is reused across commands (python-yeelight
                       reconnects it automatically on error). A per-IP
                       re-entrant lock serialises ALL access to that socket.
  * Identity cache     `cache.json` is a STICKY id/model store keyed by IP.
                       A transient read failure never drops a known bulb, so
                       a long-running ceiling light cannot vanish from the
                       roster. Identity is resolved at seed time via a unicast
                       `get_capabilities` probe (~150 ms) so a seeded bulb gets
                       its hardware id + real model immediately, without waiting
                       for broadcast SSDP (which long-running bulbs ignore).
  * State snapshot     an in-memory `(states, timestamp)` snapshot serves the
                       federation read path. Polls within `STATE_TTL` are
                       answered from memory (~0 ms); a background thread keeps
                       it warm. Broadcast SSDP only runs when a bulb is still
                       unidentified or on a long throttle — never on every poll.
"""

import argparse
import ipaddress
import json
import logging
import os
import socket
import tempfile
import threading
import time
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

try:  # models with a physical night-light (moonlight) channel + CT-capable models
    from yeelight.main import _MODEL_SPECS as _YEE_MODEL_SPECS
    _MOONLIGHT_MODELS = frozenset(
        m for m, s in _YEE_MODEL_SPECS.items() if s.get("night_light")
    )
    _CT_MODELS = frozenset(
        m for m, s in _YEE_MODEL_SPECS.items() if s.get("color_temp")
    )
except Exception:  # pragma: no cover - defensive: upstream layout changed
    logging.warning("yeelight._MODEL_SPECS unavailable; moonlight disabled for all models")
    _MOONLIGHT_MODELS = frozenset()
    _CT_MODELS = frozenset()

CACHE_FILE = "cache.json"
NAMES_FILE = "names.json"

MIRED_MIN, MIRED_MAX = 153, 500  # Matter ColorControl spec range
DEFAULT_PORT = 9800
MOONLIGHT_THRESHOLD = 0.01  # brightness < 1% triggers moonlight where supported
YEELIGHT_PORT = 55443  # Yeelight LAN protocol TCP port
PROBE_TIMEOUT = 0.4    # per-host TCP connect timeout during subnet scan
PROBE_WORKERS = 64     # parallel scan workers
PROBE_MIN_PREFIX = 22  # reject subnet scans broader than a /22 (anti-abuse)

# Cache / connection tuning (overridable via env for tests).
STATE_TTL = float(os.environ.get("YEELIGHT_STATE_TTL", "10"))       # serve memory snapshot up to this age
SSDP_THROTTLE = float(os.environ.get("YEELIGHT_SSDP_THROTTLE", "300"))  # min seconds between broadcast SSDP sweeps
CAPS_TIMEOUT = float(os.environ.get("YEELIGHT_CAPS_TIMEOUT", "2"))  # unicast get_capabilities timeout
POOL_READ_TIMEOUT = float(os.environ.get("YEELIGHT_POOL_READ_TIMEOUT", "3"))  # warm-socket read timeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

app = FastAPI(title="Yeelight Web Controller")


# ---------------------------------------------------------------------------
# Persistence  (atomic, race-safe)
# ---------------------------------------------------------------------------

_io_guard = threading.Lock()   # serialises cache.json / names.json read-modify-write


def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        # A corrupt file (e.g. an interrupted write) must NOT be silently
        # overwritten — move it aside so it is recoverable, then start clean.
        try:
            os.replace(path, path + ".corrupt")
            logging.error(f"Corrupt {path} moved to {path}.corrupt: {e}")
        except Exception as mv:
            logging.error(f"Corrupt {path} could not be quarantined: {mv}")
        return {}
    except Exception as e:
        logging.error(f"Read error {path}: {e}")
        return {}


def save_json(path: str, data: dict) -> None:
    """Atomically replace `path`. A unique temp file per writer avoids the
    shared-`.tmp` clobber when two threads persist the same target at once."""
    dirn = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=os.path.basename(path) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic on the same filesystem
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
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


def _is_provisional(bulb_id: Optional[str]) -> bool:
    return (not bulb_id) or str(bulb_id).startswith("yeelight_")


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


def _device_class(model: Optional[str], states: dict) -> tuple[str, list[str]]:
    """Stable hardware_type + capability list.

    Derived from the bulb MODEL when the model is known, so a CT light does not
    reclassify itself to a plain dimmable light the moment it enters moonlight
    (which transiently drops color_temp_mireds from its reported states). Falls
    back to the live states only for an unidentified model.
    """
    caps = []
    if "on_off" in states or model:
        caps.append("on_off")
    if "brightness_raw" in states or model:
        caps.append("brightness")

    if model in _CT_MODELS or "color_temp_mireds" in states:
        caps.append("color_temperature")
        return "color_temperature_light", caps
    if "brightness_raw" in states or model:
        return "dimmable_light", caps
    return "on_off_light", caps


# ---------------------------------------------------------------------------
# Bulb connection pool — one warm socket per IP, serialised per IP
# ---------------------------------------------------------------------------

_bulbs: dict[str, Bulb] = {}
_bulbs_guard = threading.Lock()
_bulb_locks: dict[str, threading.RLock] = {}
_bulb_locks_guard = threading.Lock()


def _lock_for(ip: str) -> threading.RLock:
    """Per-bulb RLock: base, moonlight, flow, AND reads share one socket on 55443.

    Re-entrant so a control handler that already holds the lock can call a
    capability probe (which re-acquires it) without deadlocking.
    """
    with _bulb_locks_guard:
        lock = _bulb_locks.get(ip)
        if lock is None:
            lock = threading.RLock()
            _bulb_locks[ip] = lock
        return lock


def _bulb_obj(ip: str) -> Bulb:
    """Return the process-wide persistent Bulb for `ip`, creating it once.

    python-yeelight's Bulb lazily opens its TCP socket and reuses it across
    commands, reconnecting on socket error — so keeping one instance per IP
    keeps the connection warm and avoids a cold handshake on every request.
    """
    with _bulbs_guard:
        b = _bulbs.get(ip)
        if b is None:
            b = Bulb(ip, auto_on=False)
            _bulbs[ip] = b
        return b


def _tighten_socket(bulb: Bulb) -> None:
    """Lower the warm socket's read timeout so a stalled read can't pin the per-IP
    lock — and thus block a control write to the same bulb — for python-yeelight's
    default 5 s. Touches the already-open socket only (no reconnect)."""
    try:
        sock = getattr(bulb, "_Bulb__socket", None)
        if sock is not None:
            sock.settimeout(POOL_READ_TIMEOUT)
    except Exception:
        pass


def _read_props(ip: str) -> Optional[dict]:
    try:
        with _lock_for(ip):
            b = _bulb_obj(ip)
            props = b.get_properties(["power", "bright", "ct", "active_mode"])
            _tighten_socket(b)
            return props
    except Exception:
        return None


def _resolve_identity(ip: str) -> tuple[Optional[str], Optional[str]]:
    """Learn a bulb's (hardware id, model) via a unicast get_capabilities probe.

    Unlike broadcast `discover_bulbs`, this M-SEARCHes the bulb's own IP and
    returns in ~150 ms even for long-running bulbs that no longer emit SSDP
    NOTIFY frames — so a seeded-by-IP bulb gets its canonical hex id and real
    model immediately, eliminating the `yeelight_<ip>` / model="unknown" window.
    """
    try:
        with _lock_for(ip):
            caps = _bulb_obj(ip).get_capabilities(timeout=CAPS_TIMEOUT) or {}
        return caps.get("id"), caps.get("model")
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# In-memory state snapshot + background refresh
# ---------------------------------------------------------------------------

_state_cache: dict[str, dict] = {}     # ip -> device entry (with live states)
_state_ts: float = 0.0                  # monotonic timestamp of the snapshot
_state_guard = threading.Lock()
_last_ssdp: float = 0.0                  # monotonic time of the last broadcast sweep

_bg_started = False
_bg_guard = threading.Lock()


def _publish_snapshot(active: dict[str, dict]) -> None:
    """Replace the whole snapshot (used by a full refresh)."""
    global _state_ts
    with _state_guard:
        _state_cache.clear()
        _state_cache.update({ip: dict(e) for ip, e in active.items()})
        _state_ts = time.monotonic()


def _merge_snapshot(entries: dict[str, dict]) -> None:
    """Merge entries into the snapshot without clearing it (used by seed) so a
    runtime seed can't wipe the live states of bulbs it didn't touch."""
    global _state_ts
    with _state_guard:
        for ip, e in entries.items():
            _state_cache[ip] = dict(e)
        _state_ts = time.monotonic()


def _patch_state(ip: str, **state_fields) -> None:
    """Optimistically reflect a just-confirmed control write in the snapshot so a
    federation poll within STATE_TTL sees the new value; the next refresh reconciles."""
    with _state_guard:
        entry = _state_cache.get(ip)
        if not entry:
            return
        entry = dict(entry)
        states = dict(entry.get("states", {}))
        for k, v in state_fields.items():
            if v is None:
                states.pop(k, None)
            else:
                states[k] = v
        entry["states"] = states
        _state_cache[ip] = entry


def _identity_changed(prev: dict, entry: dict, ip: str) -> bool:
    return (
        prev.get("id") != entry.get("id")
        or prev.get("model") != entry.get("model")
        or prev.get("ip") != ip
        or prev.get("names") != entry.get("names")
    )


_refresh_lock = threading.Lock()  # single-flight: coalesce concurrent refreshes


def _identity_only(entry: dict) -> dict:
    """Project an entry to its persistent identity fields (no volatile state)."""
    return {k: entry[k] for k in ("id", "endpoint_id", "ip", "model", "names") if k in entry}


def _dedupe_by_id(active: dict[str, dict]) -> dict[str, dict]:
    """After a DHCP move a bulb can briefly appear under its old and new IP with the
    same hardware id. Keep only the reachable copy so _find_by_id never resolves to
    the dead address."""
    chosen: dict[str, str] = {}  # id -> winning ip
    for ip, e in active.items():
        bid = e.get("id")
        if not bid:
            continue
        cur = chosen.get(bid)
        if cur is None or (e.get("reachable") and not active[cur].get("reachable")):
            chosen[bid] = ip
    keep = set(chosen.values()) | {ip for ip, e in active.items() if not e.get("id")}
    return {ip: e for ip, e in active.items() if ip in keep}


def _do_refresh(timeout: int = 2, allow_probe: bool = True,
                force_ssdp: bool = False) -> dict[str, dict]:
    global _last_ssdp
    with _io_guard:
        known = load_json(CACHE_FILE)
    names = load_names()
    with _state_guard:
        live = {ip: dict(e) for ip, e in _state_cache.items()}

    now = time.monotonic()
    unidentified = (not known) or any(
        _is_provisional(e.get("id")) or e.get("model") in (None, "", "unknown")
        for e in known.values()
    )
    do_ssdp = force_ssdp or unidentified or (now - _last_ssdp >= SSDP_THROTTLE)

    by_ip: dict[str, dict] = {}
    if do_ssdp:
        _last_ssdp = now
        try:
            for b in discover_bulbs(timeout=timeout):
                if b.get("ip"):
                    by_ip[b["ip"]] = b
        except Exception as e:
            logging.warning(f"discover_bulbs failed: {e}")

    # One-shot subnet probe fallback (only when truly empty), unchanged contract.
    if allow_probe and not by_ip and not known and getattr(app.state, "auto_probe", False):
        logging.info("SSDP returned 0 bulbs and cache is empty — running TCP probe")
        probe_and_seed()
        with _io_guard:
            known = load_json(CACHE_FILE)

    all_ips = set(known.keys()) | set(by_ip.keys())

    active: dict[str, dict] = {}
    identity_changed = False
    for ip in all_ips:
        prev = known.get(ip, {})
        caps = (by_ip.get(ip, {}) or {}).get("capabilities") or {}
        bulb_id = caps.get("id") or prev.get("id") or f"yeelight_{ip}"
        model = caps.get("model") or prev.get("model") or "unknown"

        # Still provisional after SSDP/cache? Upgrade via a cheap unicast probe.
        if _is_provisional(bulb_id) or model in (None, "", "unknown"):
            rid, rmodel = _resolve_identity(ip)
            if rid:
                bulb_id = rid
            if rmodel:
                model = rmodel

        props = _read_props(ip)
        if props is not None:
            entry = _build_device_entry(ip, bulb_id, model, props, names.get(bulb_id, []))
            entry["reachable"] = True
        elif prev or live.get(ip):
            # STICKY: keep the known bulb, preserving the LAST-KNOWN LIVE states from
            # the in-memory snapshot (incl. optimistic writes) — NOT the disk entry,
            # which is identity-only. A transient read failure must not make a
            # physically-on ceiling light report OFF on the federation feed.
            base = dict(live.get(ip) or prev)
            base["id"] = bulb_id
            base["model"] = model
            base["ip"] = ip
            base["endpoint_id"] = prev.get("endpoint_id", base.get("endpoint_id", 1))
            base["names"] = names.get(bulb_id, prev.get("names", base.get("names", [])))
            base.setdefault("states", {"on_off": False, "brightness_raw": 0})
            base["reachable"] = False
            entry = base
        else:
            continue  # brand-new IP that does not answer — nothing to keep

        if _identity_changed(prev, entry, ip):
            identity_changed = True
        active[ip] = entry

    active = _dedupe_by_id(active)

    # Persist IDENTITY ONLY — volatile states live in the snapshot, not on disk, so a
    # restart never resurrects stale brightness and the hot path never churns disk.
    if identity_changed or set(active) != set(known):
        with _io_guard:
            disk = load_json(CACHE_FILE)
            disk.update({ip: _identity_only(e) for ip, e in active.items()})
            save_json(CACHE_FILE, disk)

    _publish_snapshot(active)
    return active


def _refresh_devices(timeout: int = 2, allow_probe: bool = True,
                     force_ssdp: bool = False,
                     max_age: Optional[float] = None) -> dict[str, dict]:
    """Single-flight wrapper around _do_refresh. Concurrent callers coalesce: a
    caller whose `max_age` the snapshot already satisfies (because another thread
    refreshed while it waited) returns that snapshot instead of refreshing again."""
    with _refresh_lock:
        if max_age is not None:
            with _state_guard:
                if _state_cache and (time.monotonic() - _state_ts) < max_age:
                    return {ip: dict(e) for ip, e in _state_cache.items()}
        return _do_refresh(timeout=timeout, allow_probe=allow_probe, force_ssdp=force_ssdp)


def _ensure_background_refresh() -> None:
    """Start a daemon that keeps the snapshot warm so federation polls never block."""
    global _bg_started
    with _bg_guard:
        if _bg_started:
            return
        _bg_started = True

    def _loop():
        while True:
            try:
                _refresh_devices()
            except Exception as e:  # pragma: no cover - defensive
                logging.warning(f"background refresh failed: {e}")
            # Refresh well within the serve TTL so the snapshot is republished before
            # it lapses — a poll at the boundary then never blocks on a sync refresh.
            time.sleep(max(1.0, STATE_TTL / 2))

    threading.Thread(target=_loop, daemon=True, name="yeelight-refresh").start()


def _devices_snapshot(max_age: Optional[float] = None) -> dict[str, dict]:
    """Serve the federation read path from the in-memory snapshot when fresh,
    otherwise refresh (single-flight). Starts the background warmer on first use."""
    _ensure_background_refresh()
    ttl = STATE_TTL if max_age is None else max_age
    with _state_guard:
        snap = {ip: dict(e) for ip, e in _state_cache.items()} if _state_cache else None
        age = (time.monotonic() - _state_ts) if _state_cache else None
    if snap is not None and age is not None and age < ttl:
        return snap
    return _refresh_devices(max_age=ttl)


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

    Resolves each bulb's canonical hardware id + model up front via a unicast
    get_capabilities probe, so a seeded bulb is registered under its permanent
    hex id (not the transient `yeelight_<ip>` placeholder) and its moonlight
    capability is known immediately — even when broadcast SSDP is silent.
    """
    with _io_guard:
        cache = load_json(CACHE_FILE)
    names = load_names()
    added = {}
    identity_changed = False
    for ip in ips:
        props = _read_props(ip)
        if not props:
            logging.warning(f"Seed: {ip} unreachable on port 55443")
            continue
        existing = cache.get(ip, {})
        bulb_id = existing.get("id")
        model = existing.get("model")
        if _is_provisional(bulb_id) or model in (None, "", "unknown"):
            rid, rmodel = _resolve_identity(ip)
            bulb_id = rid or bulb_id or f"yeelight_{ip}"
            model = rmodel or model or "unknown"
        entry = _build_device_entry(ip, bulb_id, model, props, names.get(bulb_id, []))
        entry["reachable"] = True
        if _identity_changed(existing, entry, ip):
            identity_changed = True
        added[ip] = entry
        logging.info(f"Seed: registered {ip} as {bulb_id} (model {model})")
    if added and identity_changed:
        # Merge ONLY the freshly-resolved entries (identity only) so a concurrent
        # refresh updating other IPs isn't lost.
        with _io_guard:
            disk = load_json(CACHE_FILE)
            disk.update({ip: _identity_only(e) for ip, e in added.items()})
            save_json(CACHE_FILE, disk)
    if added:
        _merge_snapshot(added)
    return added


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
    """Parallel TCP scan of `subnet` for hosts answering on port 55443.

    Restricted to PRIVATE ranges no broader than /22 so the endpoint can't be
    abused as a wide-area / public scan primitive.
    """
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError as e:
        raise ValueError(f"Invalid subnet '{subnet}': {e}")
    if not net.is_private:
        raise ValueError(f"Refusing to scan non-private subnet '{subnet}'")
    if net.prefixlen < PROBE_MIN_PREFIX:
        raise ValueError(
            f"Subnet '{subnet}' too large (min /{PROBE_MIN_PREFIX})")

    hosts = [str(h) for h in net.hosts()]
    found: list[str] = []
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        for ip, ok in zip(hosts, pool.map(_probe_host, hosts)):
            if ok:
                found.append(ip)
    return found


def _find_by_id(device_id: str, cache: Optional[dict] = None) -> Optional[dict]:
    """Resolve a device by its canonical `id` (the bulb's hardware id).

    Matches the canonical id only. Aliases registered via `/api/name` live in
    `names[]` for display and are deliberately not resolvable as ids. Looks at
    the in-memory snapshot first, then the on-disk identity cache.
    """
    if cache is None:
        with _state_guard:
            cache = {ip: dict(e) for ip, e in _state_cache.items()}
        if not cache:
            with _io_guard:
                cache = load_json(CACHE_FILE)
    match = None
    for entry in cache.values():
        if entry.get("id") == device_id:
            # Prefer a reachable copy if the same id appears under two IPs
            # (e.g. mid-DHCP-move); fall back to whatever matched.
            if entry.get("reachable", True):
                return entry
            match = match or entry
    return match


def _bulb_for(device_id: str) -> tuple[Bulb, dict]:
    entry = _find_by_id(device_id)
    if not entry:
        # The snapshot may be cold for a just-restarted process; force a lookup
        # against the on-disk identity cache before giving up.
        with _io_guard:
            entry = _find_by_id(device_id, load_json(CACHE_FILE))
    if not entry:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")
    return _bulb_obj(entry["ip"]), entry


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
        with _lock_for(entry.get("ip", "")):
            props = bulb.get_properties(["active_mode"]) or {}
        return props.get("active_mode") in ("0", "1")
    except Exception:
        return False


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


def _coerce_int(value, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be an integer")


def _coerce_float(value, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")


# ---------------------------------------------------------------------------
# Federation endpoints (matter_webcontrol v0.25 contract)
# ---------------------------------------------------------------------------

@app.get("/api/devices")
def get_devices() -> list[dict]:
    """Federation peers consume this. Returns full device list with states."""
    return list(_devices_snapshot().values())


@app.get("/api/lights")
def get_lights() -> dict:
    """Legacy + human-friendly view (kelvin + percent)."""
    cache = _devices_snapshot()
    out = []
    for entry in cache.values():
        states = entry["states"]
        mireds = states.get("color_temp_mireds", 0)
        raw = states.get("brightness_raw", 0)
        # raw 1 is the reserved moonlight sentinel — report a non-zero floor so a
        # moonlit bulb is not shown as 0% while powered on.
        pct = 1 if raw == 1 else _raw_to_pct(raw)
        out.append({
            "ip": entry["ip"],
            "id": entry["id"],
            "model": entry["model"],
            "name": entry["names"][0] if entry["names"] else "Unknown",
            "names": entry["names"],
            "temperature_k": int(1_000_000 / mireds) if mireds else 0,
            "brightness_pct": pct,
            "state": states.get("on_off", False),
            "reachable": entry.get("reachable", True),
        })
    return {"status": "success", "data": out}


@app.get("/api/refresh")
def refresh() -> dict:
    count = len(_refresh_devices(force_ssdp=True))
    return {"status": "success", "message": f"Refreshed {count} devices"}


@app.get("/api/probe")
def probe_endpoint(subnet: Optional[str] = None):
    """TCP-scan a subnet (default: local /24) for bulbs answering on 55443."""
    subnets = [subnet] if subnet else None
    try:
        added = probe_and_seed(subnets)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "status": "success",
        "scanned": subnets or _local_ipv4_subnets(),
        "added": [{"ip": ip, "id": entry["id"]} for ip, entry in added.items()],
    }


@app.get("/api/seed")
def seed_endpoint(ips: str):
    """Add one or more comma-separated PRIVATE IPs to the cache (no SSDP)."""
    ip_list = [s.strip() for s in ips.split(",") if s.strip()]
    if not ip_list:
        raise HTTPException(status_code=400, detail="No IPs provided")
    for tok in ip_list:
        try:
            addr = ipaddress.ip_address(tok)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid IP '{tok}'")
        if not addr.is_private:
            raise HTTPException(status_code=400, detail=f"Refusing non-private IP '{tok}'")
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
    brightness = _coerce_float(p["brightness"], "brightness") if p["brightness"] is not None else None
    temperature = _coerce_int(p["temperature"], "temperature") if p["temperature"] is not None else None

    # Probe-aware capability (handles a seeded ceiling19 still cached as "unknown").
    # Computed once before the lock; for a known model it short-circuits with no probe.
    moon_capable = _moonlight_capable(bulb, entry)

    entered_moonlight = False
    new_states: dict[str, Any] = {}
    try:
        with _lock_for(entry["ip"]):
            if brightness is not None:
                brightness = max(0.0, min(1.0, brightness))
                if brightness == 0.0:
                    bulb.turn_off()
                    new_states["on_off"] = False
                    new_states["brightness_raw"] = 0
                elif brightness < MOONLIGHT_THRESHOLD:
                    # Moonlight where supported, otherwise the lowest normal level.
                    # set_power_mode powers the bulb on in MOONLIGHT, so no extra
                    # turn_on() is needed (it would send a duplicate `set_power on`).
                    if moon_capable:
                        bulb.set_power_mode(PowerMode.MOONLIGHT)
                        bulb.set_brightness(max(1, int(brightness * 100)), duration=1000)
                        entered_moonlight = True
                        new_states["on_off"] = True
                        new_states["brightness_raw"] = 1
                    else:
                        bulb.turn_on()
                        bulb.set_brightness(max(1, int(brightness * 100)), duration=1000)
                        new_states["on_off"] = True
                        new_states["brightness_raw"] = _pct_to_raw(max(1, int(brightness * 100)))
                else:
                    if moon_capable:
                        bulb.set_power_mode(PowerMode.NORMAL)
                    else:
                        bulb.turn_on()
                    bulb.set_brightness(int(brightness * 100), duration=1000)
                    new_states["on_off"] = True
                    new_states["brightness_raw"] = _pct_to_raw(int(brightness * 100))

            # Skip the CT write when this same call just selected moonlight —
            # set_color_temp would force the bulb off the night-light channel.
            if temperature is not None and temperature > 0 and not entered_moonlight:
                bulb.turn_on()
                mireds = max(MIRED_MIN, min(MIRED_MAX, _kelvin_to_mireds(temperature)))
                kelvin = int(1_000_000 / mireds)
                bulb.set_color_temp(kelvin, duration=1000)
                new_states["on_off"] = True
                new_states["color_temp_mireds"] = mireds

        if entered_moonlight:
            new_states["color_temp_mireds"] = None  # moonlight clears CT
        _patch_state(entry["ip"], **new_states)
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
            with _lock_for(entry["ip"]):
                props = bulb.get_properties(["bright", "power", "active_mode"]) or {}
            on = props.get("power") == "on"
            if on and props.get("active_mode") == "1":
                # Moonlit: report the reserved sentinel, matching /api/devices.
                return {"id": p["id"], "level": 1}
            bright_pct = int(props.get("bright") or 0)
            return {"id": p["id"], "level": _pct_to_raw(bright_pct) if on else 0}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raw = max(0, min(254, _coerce_int(p["level"], "level")))
    # Probe-aware: a seeded ceiling19 still cached as model "unknown" must still
    # reach its night-light channel via raw 1 (the model gate alone would miss it).
    moon_capable = _moonlight_capable(bulb, entry)
    try:
        new_states: dict[str, Any] = {}
        with _lock_for(entry["ip"]):
            if raw == 0:
                bulb.turn_off()
                new_states = {"on_off": False, "brightness_raw": 0}
            elif raw == 1 and moon_capable:
                # Reserved sentinel: raw 1 -> the physical night-light (moonlight)
                # channel. light_programmer encodes a sub-1 schedule level as raw 1 so
                # moonlight rides the normal level path (no direct /api/moonlight call).
                # set_power_mode powers the bulb on in MOONLIGHT and writes nl_br.
                bulb.set_power_mode(PowerMode.MOONLIGHT)
                bulb.set_brightness(100, duration=1000)
                new_states = {"on_off": True, "brightness_raw": 1, "color_temp_mireds": None}
            else:
                # Driving the main channel must leave moonlight; clear mode 5 on a
                # night-light-capable bulb (probe-aware so an unknown-model ceiling
                # light is handled too).
                if moon_capable:
                    bulb.set_power_mode(PowerMode.NORMAL)
                else:
                    bulb.turn_on()
                pct = max(1, _raw_to_pct(raw))
                bulb.set_brightness(pct, duration=1000)
                # Record the raw a read-back would yield (never the moonlight
                # sentinel 1, even for raw==1 on a non-night-light model).
                new_states = {"on_off": True, "brightness_raw": _pct_to_raw(pct)}
        _patch_state(entry["ip"], **new_states)
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

    bulb, entry = _bulb_for(p["id"])

    if p["mireds"] is None:
        try:
            with _lock_for(entry["ip"]):
                props = bulb.get_properties(["ct"]) or {}
            kelvin = int(props.get("ct") or 0)
            return {"id": p["id"], "mireds": _kelvin_to_mireds(kelvin)}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    mireds_val = max(MIRED_MIN, min(MIRED_MAX, _coerce_int(p["mireds"], "mireds")))
    try:
        kelvin = int(1_000_000 / mireds_val)
        with _lock_for(entry["ip"]):
            bulb.turn_on()
            bulb.set_color_temp(kelvin, duration=1000)
        _patch_state(entry["ip"], on_off=True, color_temp_mireds=mireds_val)
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
    nl = None
    if p["level"] is not None:
        nl = max(1, min(100, _coerce_int(p["level"], "level")))
    try:
        with _lock_for(entry["ip"]):
            # A running main-channel colour-flow owns the bulb; stop it before
            # switching power mode or the bulb is left in an undefined state.
            try:
                bulb.stop_flow(light_type=LightType.Main)
            except Exception:
                pass
            if not on:
                bulb.set_power_mode(PowerMode.NORMAL)
                _patch_state(entry["ip"], on_off=True)
                return {"status": "success", "id": p["id"], "moonlight": False}
            nl = 1 if nl is None else nl
            eff = nl if supported else 1  # no night-light channel -> lowest normal
            if supported:
                # set_power_mode powers on in MOONLIGHT (no separate turn_on()).
                bulb.set_power_mode(PowerMode.MOONLIGHT)
            else:
                bulb.turn_on()
            bulb.set_brightness(eff, duration=1000)
        patch = {"on_off": True}
        if supported:
            patch["brightness_raw"] = 1
            patch["color_temp_mireds"] = None  # moonlight clears CT
        else:
            patch["brightness_raw"] = _pct_to_raw(eff)  # leave CT untouched
        _patch_state(entry["ip"], **patch)
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
        base=_coerce_int(p.get("base") or 25, "base"),
        peak=_coerce_int(p.get("peak") or 100, "peak"),
        kelvin=_clampk(_coerce_int(p.get("kelvin") or 4500, "kelvin")),
        lightning=bool(p.get("lightning")),
        flash_kelvin=_clampk(_coerce_int(p.get("flash_kelvin") or 6000, "flash_kelvin")),
    )
    try:
        with _lock_for(entry["ip"]):
            bulb.turn_on()
            bulb.start_flow(flow, light_type=LightType.Main)
        _patch_state(entry["ip"], on_off=True)
        return {"status": "success", "id": p["id"], "flowing": True,
                "base": _coerce_int(p.get("base") or 25, "base"),
                "lightning": bool(p.get("lightning"))}
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

    with _io_guard:
        names = load_names()
        aliases = names.get(device_id, [])
        if name not in aliases:
            aliases.append(name)
        names[device_id] = aliases
        save_json(NAMES_FILE, names)
    return {"status": "success", "id": device_id, "names": aliases}


@app.get("/api/name/remove")
def remove_name(id: str, name: str):
    with _io_guard:
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

    cache = _devices_snapshot()
    devices = []
    for entry in cache.values():
        states = entry.get("states", {})
        hw_type, capabilities = _device_class(entry.get("model"), states)
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
    _ensure_background_refresh()  # keep the snapshot warm so polls never block
    logging.info(f"Starting service on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
