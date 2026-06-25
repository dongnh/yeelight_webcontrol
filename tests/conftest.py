"""Pytest fixtures for real-device integration tests.

Live Yeelight bulb(s) are required for the real-device suites. Configure via
environment variables:

    YEELIGHT_TEST_IPS  Comma-separated bulb IPs (preferred, multi-bulb).
                       e.g. YEELIGHT_TEST_IPS=192.168.1.7,192.168.1.236
    YEELIGHT_TEST_IP   Single bulb IP (legacy; used if YEELIGHT_TEST_IPS unset).
    YEELIGHT_TEST_ID   Bulb id for the single-bulb case. If unset, discovered.
    YEELIGHT_TEST_KEY  Optional X-API-Key for the spawned server.

Each bulb's canonical hardware id + model are resolved at session start via the
bridge's own unicast get_capabilities probe (the same primitive the server uses
for seeded bulbs), so no broadcast SSDP is needed even for long-running bulbs.

Tests are skipped when neither YEELIGHT_TEST_IPS nor YEELIGHT_TEST_IP is set.
"""

import os
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from yeelight import Bulb

from cli import server as srv


def _configured_ips() -> list[str]:
    multi = os.environ.get("YEELIGHT_TEST_IPS", "")
    ips = [s.strip() for s in multi.split(",") if s.strip()]
    if ips:
        return ips
    single = os.environ.get("YEELIGHT_TEST_IP")
    return [single] if single else []


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def bulb_ips() -> list[str]:
    ips = _configured_ips()
    if not ips:
        pytest.skip("YEELIGHT_TEST_IPS / YEELIGHT_TEST_IP not set — skipping real-device tests")
    return ips


@pytest.fixture(scope="session")
def bulbs(bulb_ips) -> list[dict]:
    """Resolve every configured bulb to {ip, id, model, raw}. Skips unreachable ones."""
    explicit_id = os.environ.get("YEELIGHT_TEST_ID")
    out = []
    for i, ip in enumerate(bulb_ips):
        bid, model = srv._resolve_identity(ip)
        if not bid and i == 0 and explicit_id:
            bid, model = explicit_id, "unknown"
        if not bid:
            continue
        out.append({"ip": ip, "id": bid, "model": model or "unknown", "raw": Bulb(ip)})
    if not out:
        pytest.skip("No configured bulb answered get_capabilities")
    return out


@pytest.fixture(scope="session")
def bulb_ip(bulb_ips) -> str:
    return bulb_ips[0]


@pytest.fixture(scope="session")
def bulb_id(bulbs) -> str:
    return bulbs[0]["id"]


@pytest.fixture(scope="session")
def raw_bulb(bulb_ip) -> Bulb:
    return Bulb(bulb_ip)


@pytest.fixture(scope="session")
def isolated_workdir(tmp_path_factory, bulbs):
    """Run the server with its own cache/names files in a tmp dir, seeded with
    every configured bulb under its real hardware id + model."""
    work = tmp_path_factory.mktemp("yeelight_srv")
    cwd = os.getcwd()
    os.chdir(work)
    seed = {
        b["ip"]: {
            "id": b["id"],
            "endpoint_id": 1,
            "ip": b["ip"],
            "model": b["model"],
            "names": [],
            "states": {"on_off": False, "brightness_raw": 0},
        }
        for b in bulbs
    }
    srv.save_json(srv.CACHE_FILE, seed)
    srv._publish_snapshot(seed)
    # Don't spawn the perpetual background-refresh daemon during the test session;
    # tests drive refreshes explicitly. (Mirrors the hardware-free suite.)
    srv._bg_started = True
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
def restore_bulb(request):
    """Leave every configured bulb on at a sane state after a MUTATING test only.

    Read-only tests must never touch the lights, so restoration is gated on the
    `mutating` marker. Resolved lazily so hardware-free tests aren't skipped by
    depending on the real-device fixtures."""
    yield
    if "mutating" not in request.keywords:
        return
    for ip in _configured_ips():
        try:
            bulb = Bulb(ip)
            bulb.turn_on()
            bulb.set_brightness(50, duration=200)
        except Exception:
            pass
