"""Hardware-free unit tests for the v0.8.0 caching / connection layer.

These exercise the cache, snapshot, identity-stickiness, SSDP throttle, atomic
persistence and validation logic with the bulb I/O monkeypatched out, so they
run without a live bulb (no YEELIGHT_TEST_IP needed).
"""

import json
import os
import threading

import pytest

from cli import server as srv

BULB_A = "192.168.1.7"
BULB_B = "192.168.1.236"
ID_A = "0x000000003415e584"
ID_B = "0x000000002ce4355f"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Fresh module cache state + an isolated working dir for cache/names files."""
    monkeypatch.chdir(tmp_path)
    # Reset all process-global caches between tests.
    monkeypatch.setattr(srv, "_state_cache", {}, raising=False)
    monkeypatch.setattr(srv, "_state_ts", 0.0, raising=False)
    monkeypatch.setattr(srv, "_last_ssdp", 0.0, raising=False)
    monkeypatch.setattr(srv, "_bg_started", True, raising=False)  # don't spawn the daemon
    monkeypatch.setattr(srv, "_bulbs", {}, raising=False)
    monkeypatch.setattr(srv, "_bulb_locks", {}, raising=False)
    # Never touch the network from these tests unless a test opts in.
    monkeypatch.setattr(srv, "discover_bulbs", lambda timeout=2: [])
    monkeypatch.setattr(srv, "_resolve_identity", lambda ip: (None, None))
    return tmp_path


def _seed_cache(model_a="ceiling19", model_b="ceiling19"):
    cache = {
        BULB_A: {"id": ID_A, "endpoint_id": 1, "ip": BULB_A, "model": model_a,
                 "names": [], "states": {"on_off": True, "brightness_raw": 254,
                                         "color_temp_mireds": 205}},
        BULB_B: {"id": ID_B, "endpoint_id": 1, "ip": BULB_B, "model": model_b,
                 "names": [], "states": {"on_off": True, "brightness_raw": 254,
                                         "color_temp_mireds": 205}},
    }
    srv.save_json(srv.CACHE_FILE, cache)
    return cache


# ---------------------------------------------------------------------------
# Atomic, race-safe persistence
# ---------------------------------------------------------------------------

def test_save_json_atomic_roundtrip(isolated):
    srv.save_json(srv.CACHE_FILE, {"a": 1})
    assert srv.load_json(srv.CACHE_FILE) == {"a": 1}
    # No leftover temp files in the dir.
    assert not [f for f in os.listdir(".") if f.endswith(".tmp")]


def test_save_json_concurrent_writers_never_corrupt(isolated):
    """Many threads writing the same target must never leave a corrupt file."""
    errors = []

    def writer(n):
        try:
            for _ in range(20):
                srv.save_json(srv.CACHE_FILE, {"writer": n, "payload": list(range(50))})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # The surviving file must be valid JSON (no interleaved/truncated blob).
    data = json.load(open(srv.CACHE_FILE))
    assert set(data) == {"writer", "payload"}
    assert not [f for f in os.listdir(".") if f.endswith(".tmp")]


def test_load_json_quarantines_corrupt_file(isolated):
    with open(srv.CACHE_FILE, "w") as f:
        f.write("{not valid json")
    assert srv.load_json(srv.CACHE_FILE) == {}
    # Corrupt content moved aside, not silently overwritten.
    assert os.path.exists(srv.CACHE_FILE + ".corrupt")


# ---------------------------------------------------------------------------
# Sticky cache — a transient read failure must NOT drop a known bulb
# ---------------------------------------------------------------------------

def test_refresh_keeps_bulb_when_read_fails(isolated, monkeypatch):
    _seed_cache()
    # Both reads fail (a transient stall) and SSDP is silent.
    monkeypatch.setattr(srv, "_read_props", lambda ip: None)

    active = srv._refresh_devices()

    assert set(active) == {BULB_A, BULB_B}, "known bulbs must survive a failed read"
    assert active[BULB_A]["id"] == ID_A
    assert active[BULB_B]["id"] == ID_B
    assert active[BULB_A]["reachable"] is False
    # Last-known states are preserved, not blanked.
    assert active[BULB_A]["states"]["brightness_raw"] == 254


def test_refresh_does_not_revert_hex_id_on_failure(isolated, monkeypatch):
    """The canonical hex id must never degrade back to yeelight_<ip>."""
    _seed_cache()
    monkeypatch.setattr(srv, "_read_props", lambda ip: None)
    active = srv._refresh_devices()
    for ip, eid in [(BULB_A, ID_A), (BULB_B, ID_B)]:
        assert active[ip]["id"] == eid
        assert not active[ip]["id"].startswith("yeelight_")


def test_refresh_recovers_reachability(isolated, monkeypatch):
    _seed_cache()
    state = {"fail": True}

    def reader(ip):
        if state["fail"]:
            return None
        return {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"}

    monkeypatch.setattr(srv, "_read_props", reader)
    srv._refresh_devices()  # both unreachable, kept sticky
    state["fail"] = False
    active = srv._refresh_devices()  # back online
    assert active[BULB_A]["reachable"] is True
    assert active[BULB_A]["states"]["on_off"] is True
    # The live read must SUPERSEDE the sticky last-known value (80% -> raw 203),
    # not carry over the stale 254.
    assert active[BULB_A]["states"]["brightness_raw"] == srv._pct_to_raw(80)


def test_sticky_preserves_live_state_not_disk_seed(isolated, monkeypatch):
    """REGRESSION: a transient read failure must preserve the last-known LIVE state
    (in-memory snapshot), not revert to the frozen identity-only disk seed. A
    physically-on ceiling light must never report OFF on the federation feed."""
    # Disk seed says OFF/0 (the identity-era state).
    srv.save_json(srv.CACHE_FILE, {BULB_A: {"id": ID_A, "endpoint_id": 1, "ip": BULB_A,
        "model": "ceiling19", "names": [], "states": {"on_off": False, "brightness_raw": 0}}})
    monkeypatch.setattr(srv, "_last_ssdp", srv.time.monotonic(), raising=False)
    # A successful read flips it to ON / 80% in the live snapshot.
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"})
    srv._refresh_devices()
    # Now reads start failing.
    monkeypatch.setattr(srv, "_read_props", lambda ip: None)
    active = srv._refresh_devices()
    assert active[BULB_A]["reachable"] is False
    assert active[BULB_A]["states"]["on_off"] is True, "must keep LIVE on_off, not disk OFF"
    assert active[BULB_A]["states"]["brightness_raw"] == srv._pct_to_raw(80)


def test_dedupe_by_id_keeps_reachable_after_dhcp_move(isolated):
    active = {
        "192.168.1.7": {"id": ID_A, "ip": "192.168.1.7", "model": "ceiling19",
                        "names": [], "states": {"on_off": True}, "reachable": False},
        "192.168.1.99": {"id": ID_A, "ip": "192.168.1.99", "model": "ceiling19",
                         "names": [], "states": {"on_off": True}, "reachable": True},
    }
    deduped = srv._dedupe_by_id(active)
    assert len(deduped) == 1
    assert "192.168.1.99" in deduped  # the reachable (new) IP wins
    assert srv._find_by_id(ID_A, deduped)["ip"] == "192.168.1.99"


# ---------------------------------------------------------------------------
# SSDP throttle — the 2s broadcast must be skipped when bulbs are identified
# ---------------------------------------------------------------------------

def test_ssdp_skipped_when_all_identified(isolated, monkeypatch):
    _seed_cache()
    calls = {"n": 0}

    def fake_discover(timeout=2):
        calls["n"] += 1
        return []

    monkeypatch.setattr(srv, "discover_bulbs", fake_discover)
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"})
    monkeypatch.setattr(srv, "_last_ssdp", srv.time.monotonic(), raising=False)

    srv._refresh_devices()
    assert calls["n"] == 0, "fully-identified bulbs must not trigger broadcast SSDP"


def test_ssdp_runs_when_a_bulb_is_provisional(isolated, monkeypatch):
    # One bulb still carries a yeelight_<ip> placeholder -> SSDP must run.
    cache = {
        BULB_A: {"id": f"yeelight_{BULB_A}", "ip": BULB_A, "model": "unknown",
                 "names": [], "states": {"on_off": False, "brightness_raw": 0}},
    }
    srv.save_json(srv.CACHE_FILE, cache)
    calls = {"n": 0}
    monkeypatch.setattr(srv, "discover_bulbs",
                        lambda timeout=2: calls.__setitem__("n", calls["n"] + 1) or [])
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"})
    srv._refresh_devices()
    assert calls["n"] == 1, "a provisional id must force an SSDP sweep"


def test_ssdp_upgrades_provisional_via_identity_probe(isolated, monkeypatch):
    cache = {BULB_A: {"id": f"yeelight_{BULB_A}", "ip": BULB_A, "model": "unknown",
                      "names": [], "states": {"on_off": False, "brightness_raw": 0}}}
    srv.save_json(srv.CACHE_FILE, cache)
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"})
    monkeypatch.setattr(srv, "_resolve_identity", lambda ip: (ID_A, "ceiling19"))
    active = srv._refresh_devices()
    assert active[BULB_A]["id"] == ID_A
    assert active[BULB_A]["model"] == "ceiling19"


# ---------------------------------------------------------------------------
# In-memory snapshot + TTL
# ---------------------------------------------------------------------------

def test_snapshot_served_within_ttl_without_refresh(isolated, monkeypatch):
    _seed_cache()
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"})
    monkeypatch.setattr(srv, "_ensure_background_refresh", lambda: None)

    refreshes = {"n": 0}
    real_refresh = srv._refresh_devices

    def counting_refresh(*a, **k):
        refreshes["n"] += 1
        return real_refresh(*a, **k)

    monkeypatch.setattr(srv, "_refresh_devices", counting_refresh)

    srv._devices_snapshot()  # cold -> 1 refresh
    srv._devices_snapshot()  # warm within TTL -> no refresh
    srv._devices_snapshot()
    assert refreshes["n"] == 1


def test_snapshot_refreshes_after_ttl(isolated, monkeypatch):
    _seed_cache()
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": "80", "ct": "4000", "active_mode": "0"})
    monkeypatch.setattr(srv, "_ensure_background_refresh", lambda: None)
    monkeypatch.setattr(srv, "STATE_TTL", 0.0, raising=False)  # everything is "stale"

    refreshes = {"n": 0}
    real_refresh = srv._refresh_devices

    def counting_refresh(*a, **k):
        refreshes["n"] += 1
        return real_refresh(*a, **k)

    monkeypatch.setattr(srv, "_refresh_devices", counting_refresh)
    srv._devices_snapshot()
    srv._devices_snapshot()
    assert refreshes["n"] == 2


def test_patch_state_optimistic_update_and_clear(isolated):
    srv._publish_snapshot({BULB_A: {"id": ID_A, "ip": BULB_A, "model": "ceiling19",
                                    "names": [], "states": {"on_off": True,
                                                            "brightness_raw": 254,
                                                            "color_temp_mireds": 205}}})
    srv._patch_state(BULB_A, brightness_raw=1, color_temp_mireds=None)
    with srv._state_guard:
        states = srv._state_cache[BULB_A]["states"]
    assert states["brightness_raw"] == 1
    assert "color_temp_mireds" not in states  # None clears the field (moonlight)


# ---------------------------------------------------------------------------
# Disk churn — volatile state changes must NOT rewrite cache.json
# ---------------------------------------------------------------------------

def test_identity_persisted_then_volatile_changes_skip_disk(isolated, monkeypatch):
    """An identity upgrade DOES write disk (non-vacuous base); a later volatile
    state change must NOT, and the persisted entry must be identity-only."""
    # Start provisional so the first refresh upgrades identity -> a real write.
    srv.save_json(srv.CACHE_FILE, {BULB_A: {"id": f"yeelight_{BULB_A}", "ip": BULB_A,
        "model": "unknown", "names": [], "states": {"on_off": False, "brightness_raw": 0}}})
    bright = {"v": "80"}
    monkeypatch.setattr(srv, "_read_props",
                        lambda ip: {"power": "on", "bright": bright["v"], "ct": "4000",
                                    "active_mode": "0"})
    monkeypatch.setattr(srv, "_resolve_identity", lambda ip: (ID_A, "ceiling19"))

    writes = {"n": 0}
    real_save = srv.save_json

    def counting_save(path, data):
        if path == srv.CACHE_FILE:
            writes["n"] += 1
        return real_save(path, data)

    monkeypatch.setattr(srv, "save_json", counting_save)

    srv._refresh_devices()                  # identity yeelight_->hex upgrade -> a write
    assert writes["n"] >= 1, "an identity change must persist (test would be vacuous otherwise)"
    base = writes["n"]

    # Persisted content is identity-only — no volatile states / reachable flag.
    disk = json.load(open(srv.CACHE_FILE))
    assert disk[BULB_A]["id"] == ID_A
    assert "states" not in disk[BULB_A]
    assert "reachable" not in disk[BULB_A]

    bright["v"] = "30"                       # volatile change only, identity stable
    srv._refresh_devices()
    assert writes["n"] == base, "a brightness change must not rewrite the identity cache"


# ---------------------------------------------------------------------------
# Scan-primitive validation
# ---------------------------------------------------------------------------

def test_probe_subnet_rejects_non_private():
    with pytest.raises(ValueError):
        srv.probe_subnet("8.8.8.0/24")


def test_probe_subnet_rejects_too_broad():
    with pytest.raises(ValueError):
        srv.probe_subnet("10.0.0.0/8")


def test_coerce_helpers_raise_400():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        srv._coerce_int("abc", "level")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException):
        srv._coerce_float("xyz", "brightness")


# ---------------------------------------------------------------------------
# get_lights moonlight sentinel round-trip
# ---------------------------------------------------------------------------

def test_get_lights_reports_moonlight_floor(isolated, monkeypatch):
    srv._publish_snapshot({BULB_A: {"id": ID_A, "ip": BULB_A, "model": "ceiling19",
                                    "names": ["sky"], "states": {"on_off": True,
                                                                 "brightness_raw": 1}}})
    monkeypatch.setattr(srv, "_ensure_background_refresh", lambda: None)
    monkeypatch.setattr(srv, "STATE_TTL", 9999, raising=False)
    body = srv.get_lights()
    row = body["data"][0]
    assert row["brightness_pct"] == 1  # moonlit bulb is not shown as 0%
    assert row["state"] is True
