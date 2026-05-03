"""Real-device integration tests.

These tests drive a real Yeelight bulb via the FastAPI app. They verify both
the local control behavior and the wire contract that matter_webcontrol
v0.25.0 LogicalBridgeClient relies on.
"""

import time

import pytest


def _wait(seconds: float = 1.5) -> None:
    """Yeelight transitions are async — give the bulb time to settle."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Discovery / federation contract
# ---------------------------------------------------------------------------

def test_devices_endpoint_lists_bulb(client, bulb_id):
    r = client.get("/api/devices")
    assert r.status_code == 200
    devices = r.json()
    assert isinstance(devices, list)
    ids = [d["id"] for d in devices]
    assert bulb_id in ids, f"{bulb_id} not in {ids}"

    target = next(d for d in devices if d["id"] == bulb_id)
    # Federation contract: each device has id, states dict, names list
    assert "states" in target and isinstance(target["states"], dict)
    assert "names" in target and isinstance(target["names"], list)
    assert "on_off" in target["states"]


def test_metadata_v2_schema(client, bulb_id):
    r = client.get("/api/metadata")
    assert r.status_code == 200
    body = r.json()
    assert body["bridge"]["api_version"] == "2"
    assert "devices" in body
    target = next((d for d in body["devices"] if d["id"] == bulb_id), None)
    assert target is not None
    # No embedded scripts in v2
    assert "events" not in target
    assert "capabilities" in target
    assert "states" in target


# ---------------------------------------------------------------------------
# Level control (Matter raw 0-254)
# ---------------------------------------------------------------------------

def test_level_post_turns_on_and_dims(client, bulb_id, raw_bulb):
    r = client.post("/api/level", json={"id": bulb_id, "level": 200})
    assert r.status_code == 200, r.text
    assert r.json()["level"] == 200
    _wait()

    props = raw_bulb.get_properties(["power", "bright"])
    assert props["power"] == "on"
    assert int(props["bright"]) > 50  # ~78%


def test_level_zero_turns_off(client, bulb_id, raw_bulb):
    raw_bulb.turn_on()
    _wait(0.5)
    r = client.post("/api/level", json={"id": bulb_id, "level": 0})
    assert r.status_code == 200
    _wait()
    assert raw_bulb.get_properties(["power"])["power"] == "off"


def test_level_get_reads_current_level(client, bulb_id, raw_bulb):
    raw_bulb.turn_on()
    raw_bulb.set_brightness(50, duration=200)
    _wait()
    r = client.get(f"/api/level?id={bulb_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == bulb_id
    # 50% ≈ 127 raw, allow tolerance for async state
    assert 100 <= body["level"] <= 160


def test_level_clamped_to_matter_range(client, bulb_id):
    r = client.post("/api/level", json={"id": bulb_id, "level": 99999})
    assert r.status_code == 200
    assert r.json()["level"] == 254


# ---------------------------------------------------------------------------
# Color temperature (mireds, 153-500)
# ---------------------------------------------------------------------------

def test_mired_post_sets_color_temp(client, bulb_id, raw_bulb):
    # 250 mired ≈ 4000 K
    r = client.post("/api/mired", json={"id": bulb_id, "mireds": 250})
    assert r.status_code == 200
    assert r.json()["mireds"] == 250
    _wait()
    kelvin = int(raw_bulb.get_properties(["ct"])["ct"])
    assert 3800 <= kelvin <= 4200


def test_mired_clamped_to_spec_range(client, bulb_id):
    r = client.post("/api/mired", json={"id": bulb_id, "mireds": 50})
    assert r.json()["mireds"] == 153
    r = client.post("/api/mired", json={"id": bulb_id, "mireds": 9000})
    assert r.json()["mireds"] == 500


def test_mired_get_reads_current(client, bulb_id):
    r = client.get(f"/api/mired?id={bulb_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == bulb_id
    assert isinstance(body["mireds"], int)


# ---------------------------------------------------------------------------
# Combo /api/set (brightness 0.0-1.0 float)
# ---------------------------------------------------------------------------

def test_set_brightness_float_normalized(client, bulb_id, raw_bulb):
    r = client.post("/api/set", json={"id": bulb_id, "brightness": 0.75})
    assert r.status_code == 200
    _wait()
    props = raw_bulb.get_properties(["power", "bright"])
    assert props["power"] == "on"
    assert 65 <= int(props["bright"]) <= 85


def test_set_brightness_zero_off(client, bulb_id, raw_bulb):
    raw_bulb.turn_on()
    _wait(0.5)
    r = client.post("/api/set", json={"id": bulb_id, "brightness": 0.0})
    assert r.status_code == 200
    _wait()
    assert raw_bulb.get_properties(["power"])["power"] == "off"


def test_set_with_temperature_kelvin(client, bulb_id, raw_bulb):
    r = client.post("/api/set", json={"id": bulb_id, "brightness": 0.5, "temperature": 5000})
    assert r.status_code == 200
    _wait()
    kelvin = int(raw_bulb.get_properties(["ct"])["ct"])
    assert 4700 <= kelvin <= 5300


# ---------------------------------------------------------------------------
# Aliases (multi-name list)
# ---------------------------------------------------------------------------

def test_name_add_then_remove(client, bulb_id):
    add = client.post("/api/name", json={"id": bulb_id, "name": "test_alias"})
    assert add.status_code == 200
    assert "test_alias" in add.json()["names"]

    rm = client.get(f"/api/name/remove?id={bulb_id}&name=test_alias")
    assert rm.status_code == 200
    assert "test_alias" not in rm.json()["names"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_api_key_required_when_set(client, bulb_id):
    from cli import server as srv
    srv.app.state.api_key = "secret"
    try:
        r = client.get("/api/devices")
        assert r.status_code == 401

        r = client.get("/api/devices", headers={"X-API-Key": "secret"})
        assert r.status_code == 200
    finally:
        srv.app.state.api_key = None


# ---------------------------------------------------------------------------
# End-to-end via real socket (matches LogicalBridgeClient HTTP path)
# ---------------------------------------------------------------------------

def test_logical_bridge_client_compatibility(live_server, bulb_id, raw_bulb):
    """Use the actual matter_webcontrol client shape against a live socket."""
    import json
    import urllib.request

    headers = {"Content-Type": "application/json"}

    # GET /api/devices (refresh equivalent)
    req = urllib.request.Request(f"{live_server}/api/devices", headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        devices = json.loads(r.read())
    assert any(d["id"] == bulb_id for d in devices)

    # POST /api/level
    body = json.dumps({"id": bulb_id, "level": 100}).encode()
    req = urllib.request.Request(f"{live_server}/api/level", data=body,
                                  headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    _wait()
    assert int(raw_bulb.get_properties(["bright"])["bright"]) > 20

    # POST /api/mired
    body = json.dumps({"id": bulb_id, "mireds": 300}).encode()
    req = urllib.request.Request(f"{live_server}/api/mired", data=body,
                                  headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
