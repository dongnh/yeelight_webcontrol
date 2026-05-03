"""Pytest fixtures for real-device integration tests.

A live Yeelight bulb is required. Configure via environment variables:

    YEELIGHT_TEST_IP   IP of the bulb under test (required)
    YEELIGHT_TEST_ID   Bulb id (capabilities.id). If unset, discovered.
    YEELIGHT_TEST_KEY  Optional X-API-Key for the spawned server

Tests are skipped when YEELIGHT_TEST_IP is not set.
"""

import os
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from yeelight import Bulb, discover_bulbs

from cli import server as srv


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def bulb_ip() -> str:
    ip = os.environ.get("YEELIGHT_TEST_IP")
    if not ip:
        pytest.skip("YEELIGHT_TEST_IP not set — skipping real-device tests")
    return ip


@pytest.fixture(scope="session")
def bulb_id(bulb_ip) -> str:
    explicit = os.environ.get("YEELIGHT_TEST_ID")
    if explicit:
        return explicit
    for b in discover_bulbs(timeout=2):
        if b.get("ip") == bulb_ip:
            cap_id = (b.get("capabilities") or {}).get("id")
            if cap_id:
                return cap_id
    pytest.skip(f"Could not discover capabilities.id for {bulb_ip}; set YEELIGHT_TEST_ID")


@pytest.fixture(scope="session")
def raw_bulb(bulb_ip) -> Bulb:
    return Bulb(bulb_ip)


@pytest.fixture(scope="session")
def isolated_workdir(tmp_path_factory, bulb_ip, bulb_id):
    """Run the server with its own cache/names files in a tmp dir."""
    work = tmp_path_factory.mktemp("yeelight_srv")
    cwd = os.getcwd()
    os.chdir(work)
    # Pre-seed cache so endpoints can resolve the bulb without a discover round.
    seed = {
        bulb_ip: {
            "id": bulb_id,
            "endpoint_id": 1,
            "ip": bulb_ip,
            "model": "test",
            "names": [],
            "states": {"on_off": False, "brightness_raw": 0},
        }
    }
    srv.save_json(srv.CACHE_FILE, seed)
    yield work
    os.chdir(cwd)


@pytest.fixture(scope="session")
def client(isolated_workdir) -> TestClient:
    """In-process FastAPI test client (no auth)."""
    srv.app.state.api_key = None
    return TestClient(srv.app)


@pytest.fixture(scope="session")
def live_server(isolated_workdir):
    """Background uvicorn instance — for tests that need a real socket."""
    port = _free_port()
    srv.app.state.api_key = os.environ.get("YEELIGHT_TEST_KEY")
    config = uvicorn.Config(srv.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("uvicorn failed to start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=3)


@pytest.fixture(autouse=True)
def restore_bulb(raw_bulb):
    """Leave the bulb on at a sane state after each test."""
    yield
    try:
        raw_bulb.turn_on()
        raw_bulb.set_brightness(50, duration=200)
    except Exception:
        pass
