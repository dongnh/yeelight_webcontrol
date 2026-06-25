"""Real-device tests across BOTH ceiling19 Yeelight bulbs in the home.

Configure with:  YEELIGHT_TEST_IPS=192.168.1.7,192.168.1.236

Two groups:
  * Read-only  — list/identity/metadata/cache-timing. Safe to run any time; they
                 never change what the lights are doing.
  * mutating   — drive level / colour-temp / moonlight on the real ceilings.
                 Marked `@pytest.mark.mutating`; run with `-m mutating`. They
                 briefly change the lights (light_programmer reasserts its
                 schedule within ~1 s afterwards).
"""

import time

import pytest

from cli import server as srv


def _wait(seconds: float = 1.5) -> None:
    time.sleep(seconds)


# ===========================================================================
# Read-only — caching, identity, federation feed (no visible light change)
# ===========================================================================

def test_both_bulbs_resolve_to_hardware_id_and_model(bulbs):
    """The unicast get_capabilities primitive recovers each bulb's canonical hex
    id + real model — even though these are long-running, SSDP-silent ceilings."""
    assert len(bulbs) >= 2, "expected two configured bulbs"
    for b in bulbs:
        assert b["id"].startswith("0x"), f"{b['ip']} did not resolve a hex id: {b['id']}"
        assert b["model"] == "ceiling19", f"{b['ip']} model={b['model']}"
    ids = {b["id"] for b in bulbs}
    assert len(ids) == len(bulbs), "bulb ids must be distinct"


def test_devices_endpoint_lists_both_bulbs(client, bulbs):
    r = client.get("/api/devices")
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()}
    for b in bulbs:
        assert b["id"] in ids, f"{b['id']} missing from /api/devices {ids}"


def test_metadata_reports_stable_color_temperature_light(client, bulbs):
    """A ceiling19 must classify as color_temperature_light regardless of whether
    it is currently in moonlight (which drops color_temp from live states)."""
    body = client.get("/api/metadata").json()
    by_id = {d["id"]: d for d in body["devices"]}
    for b in bulbs:
        dev = by_id.get(b["id"])
        assert dev is not None
        assert dev["hardware_type"] == "color_temperature_light"
        assert "color_temperature" in dev["capabilities"]


def test_snapshot_serves_warm_poll_fast(client, bulbs):
    """Second federation poll within the TTL is served from memory — no SSDP,
    no per-bulb round-trip — so it is dramatically faster than a cold refresh."""
    client.get("/api/devices")  # warm the snapshot
    t = time.time()
    r = client.get("/api/devices")
    warm_ms = (time.time() - t) * 1000
    assert r.status_code == 200
    assert warm_ms < 250, f"warm poll took {warm_ms:.0f} ms (snapshot not serving?)"


def test_refresh_of_identified_bulbs_skips_broadcast_ssdp(client, bulbs, monkeypatch):
    """With both bulbs already identified, a refresh must not pay the 2s SSDP."""
    calls = {"n": 0}

    def counting_discover(timeout=2):
        calls["n"] += 1
        return []

    monkeypatch.setattr(srv, "discover_bulbs", counting_discover)
    monkeypatch.setattr(srv, "_last_ssdp", srv.time.monotonic(), raising=False)
    srv._refresh_devices()
    assert calls["n"] == 0


def test_pool_reuses_one_warm_bulb_per_ip(bulbs):
    """The pool hands back one Bulb per IP AND keeps its socket warm: two reads
    ride the same underlying TCP socket (fd), not a fresh connect each time."""
    for b in bulbs:
        srv._bulbs.pop(b["ip"], None)  # force the creation branch, not a pre-pooled hit
        first = srv._bulb_obj(b["ip"])
        assert srv._bulb_obj(b["ip"]) is first  # one instance per IP

        assert srv._read_props(b["ip"]) is not None
        sock = getattr(first, "_Bulb__socket", None)
        assert sock is not None, "pooled bulb should hold an open socket after a read"
        fd1 = sock.fileno()
        assert srv._read_props(b["ip"]) is not None
        fd2 = getattr(first, "_Bulb__socket").fileno()
        assert fd1 == fd2, "second read must reuse the same warm socket, not reconnect"


# ===========================================================================
# Mutating — drive the real ceilings (run with `-m mutating`)
# ===========================================================================

@pytest.mark.mutating
def test_level_control_each_bulb(client, bulbs):
    for b in bulbs:
        r = client.post("/api/level", json={"id": b["id"], "level": 200})
        assert r.status_code == 200, r.text
        assert r.json()["level"] == 200
    _wait()
    for b in bulbs:
        props = b["raw"].get_properties(["power", "bright"])
        assert props["power"] == "on"
        assert int(props["bright"]) > 50, f"{b['ip']} bright={props['bright']}"


@pytest.mark.mutating
def test_mired_control_each_bulb(client, bulbs):
    for b in bulbs:
        r = client.post("/api/mired", json={"id": b["id"], "mireds": 250})
        assert r.status_code == 200
        assert r.json()["mireds"] == 250
    _wait()
    for b in bulbs:
        kelvin = int(b["raw"].get_properties(["ct"])["ct"])
        assert 3800 <= kelvin <= 4200, f"{b['ip']} kelvin={kelvin}"


@pytest.mark.mutating
def test_moonlight_via_level_raw1_each_bulb(client, bulbs):
    """The reserved /api/level raw 1 must drive each ceiling19 onto its physical
    night-light channel, and a normal level must bring it back out."""
    for b in bulbs:
        r = client.post("/api/level", json={"id": b["id"], "level": 1})
        assert r.status_code == 200
        assert r.json()["level"] == 1
    _wait(2.0)
    for b in bulbs:
        props = b["raw"].get_properties(["power", "active_mode"])
        assert props["power"] == "on"
        assert props["active_mode"] == "1", f"{b['ip']} not in moonlight: {props}"

    # Drive back to a normal daylight level — must leave the night-light channel.
    for b in bulbs:
        client.post("/api/level", json={"id": b["id"], "level": 180})
    _wait(2.0)
    for b in bulbs:
        props = b["raw"].get_properties(["power", "active_mode"])
        assert props["active_mode"] == "0", f"{b['ip']} stuck in moonlight: {props}"


@pytest.mark.mutating
def test_moonlight_endpoint_each_bulb(client, bulbs):
    for b in bulbs:
        on = client.post("/api/moonlight", json={"id": b["id"], "on": True, "level": 5})
        assert on.status_code == 200
        assert on.json()["moonlight"] is True
    _wait(2.0)
    for b in bulbs:
        assert b["raw"].get_properties(["active_mode"])["active_mode"] == "1"
    for b in bulbs:
        off = client.post("/api/moonlight", json={"id": b["id"], "on": False})
        assert off.status_code == 200
        assert off.json()["moonlight"] is False
    _wait(2.0)
    for b in bulbs:
        assert b["raw"].get_properties(["active_mode"])["active_mode"] == "0"


@pytest.mark.mutating
def test_set_moonlight_with_temperature_stays_moonlit(client, bulbs):
    """A single /api/set carrying both a sub-1% brightness AND a temperature must
    NOT be kicked out of moonlight by the colour-temp write."""
    for b in bulbs:
        r = client.post("/api/set", json={"id": b["id"], "brightness": 0.005, "temperature": 4000})
        assert r.status_code == 200
    _wait(2.0)
    for b in bulbs:
        props = b["raw"].get_properties(["power", "active_mode"])
        assert props["active_mode"] == "1", f"{b['ip']} fell out of moonlight: {props}"


@pytest.mark.mutating
def test_level_zero_turns_off_each_bulb(client, bulbs):
    for b in bulbs:
        b["raw"].turn_on()
    _wait(0.5)
    for b in bulbs:
        r = client.post("/api/level", json={"id": b["id"], "level": 0})
        assert r.status_code == 200
    _wait()
    for b in bulbs:
        assert b["raw"].get_properties(["power"])["power"] == "off"
