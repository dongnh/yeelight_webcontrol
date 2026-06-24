"""Hardware-free unit tests for moonlight (night-light) handling.

These exercise pure functions (`_supports_moonlight`, `_build_device_entry`)
and need no live bulb, so they run without YEELIGHT_TEST_IP.
"""

from cli import server as srv


def test_supports_moonlight_by_model():
    # Ceiling lights and Bedside Lamp 2/3 have a night-light channel.
    assert srv._supports_moonlight("ceiling1")
    assert srv._supports_moonlight("bslamp2")
    # Plain colour / CT bulbs, light strips, and unknown/None do not.
    assert not srv._supports_moonlight("color")
    assert not srv._supports_moonlight("mono")
    assert not srv._supports_moonlight("strip1")
    assert not srv._supports_moonlight("unknown")
    assert not srv._supports_moonlight("")
    assert not srv._supports_moonlight(None)


def test_build_entry_reports_moonlight_floor_not_stale_bright():
    # active_mode == "1": bulb is in moonlight; `bright`/`ct` are stale daylight
    # values and must not leak into the reported state.
    props = {"power": "on", "bright": "50", "ct": "4000",
             "active_mode": "1", "nl_br": "30"}
    states = srv._build_device_entry("1.2.3.4", "id1", "ceiling1", props, [])["states"]
    assert states["on_off"] is True
    assert states["brightness_raw"] == 1          # floor, not raw(50)
    assert "color_temp_mireds" not in states      # moonlight is fixed warm white


def test_build_entry_normal_mode_uses_bright_and_ct():
    props = {"power": "on", "bright": "50", "ct": "4000",
             "active_mode": "0", "nl_br": ""}
    states = srv._build_device_entry("1.2.3.4", "id1", "ceiling1", props, [])["states"]
    assert states["on_off"] is True
    assert states["brightness_raw"] == srv._pct_to_raw(50)
    assert states["color_temp_mireds"] == srv._kelvin_to_mireds(4000)


def test_build_entry_off_is_dark():
    props = {"power": "off", "bright": "50", "ct": "4000",
             "active_mode": "", "nl_br": ""}
    states = srv._build_device_entry("1.2.3.4", "id1", "ceiling1", props, [])["states"]
    assert states["on_off"] is False
    assert states["brightness_raw"] == 0


class _NoProbeBulb:
    """A known model must be decided from the table — never probed over the LAN."""
    def get_properties(self, keys):
        raise AssertionError("known model should not trigger a live probe")


class _ProbeBulb:
    def __init__(self, active_mode):
        self._am = active_mode

    def get_properties(self, keys):
        return {"active_mode": self._am}


def test_moonlight_capable_known_models_skip_probe():
    assert srv._moonlight_capable(_NoProbeBulb(), {"model": "ceiling1", "ip": "x"})
    assert srv._moonlight_capable(_NoProbeBulb(), {"model": "bslamp2", "ip": "x"})
    assert not srv._moonlight_capable(_NoProbeBulb(), {"model": "color", "ip": "x"})
    assert not srv._moonlight_capable(_NoProbeBulb(), {"model": "strip1", "ip": "x"})


def test_moonlight_capable_unknown_model_probes_active_mode():
    # Unknown/seeded model: a non-empty active_mode proves the channel exists.
    assert srv._moonlight_capable(_ProbeBulb("1"), {"model": "unknown", "ip": "x"})
    assert srv._moonlight_capable(_ProbeBulb("0"), {"model": "unknown", "ip": "x"})
    assert not srv._moonlight_capable(_ProbeBulb(""), {"model": "unknown", "ip": "x"})
    assert not srv._moonlight_capable(_ProbeBulb(""), {"model": "", "ip": "x"})
