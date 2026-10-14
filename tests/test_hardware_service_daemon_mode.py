"""HardwareService in daemon mode reads sensors and heater/fan state from the
control daemon's state file instead of touching the lines."""

import json

from app.services.gpio_service import WEB_OUTPUTS
from app.services.hardware_service import HardwareService
from tests.test_gpio_service import _svc as make_gpio


class _Cam:
    def capture(self):
        return {"ok": True}


def _state(path, **over):
    base = {"ts": 1_000_000_000.0, "online": True, "temperature_c": 37.7, "humidity_pct": 54.0,
            "heater_on": True, "fan_on": False, "sensor_fault": ""}
    base.update(over)
    path.write_text(json.dumps(base))


def test_reads_come_from_daemon_state(tmp_path, monkeypatch):
    import app.control.bridge as bridge

    monkeypatch.setattr(bridge.time, "time", lambda: 1_000_000_005.0)
    state = tmp_path / "state.json"
    _state(state)
    gpio = make_gpio(owned_outputs=WEB_OUTPUTS, sensor=None)
    hw = HardwareService(gpio, _Cam(), daemon_state_path=str(state))
    assert hw.daemon_mode is True
    env = hw.read_environment()
    assert env["ok"] is True and env["temperature_c"] == 37.7 and env["source"] == "control-daemon"
    assert hw.read_temp() == {"ok": True, "temperature_c": 37.7, "mock": False}
    assert hw.read_humidity() == {"ok": True, "humidity_pct": 54.0, "mock": False}
    st = hw.get_state()
    assert st["heater"] is True and st["fan"] is False and st["source"] == "control-daemon"
    assert st["alarm"] is False                     # web-owned outputs still from the web process


def test_stale_daemon_state_is_offline_and_unknown(tmp_path, monkeypatch):
    import app.control.bridge as bridge

    monkeypatch.setattr(bridge.time, "time", lambda: 1_000_000_000.0 + 10_000)
    state = tmp_path / "state.json"
    _state(state)
    hw = HardwareService(make_gpio(owned_outputs=WEB_OUTPUTS, sensor=None), _Cam(), daemon_state_path=str(state))
    assert hw.read_temp()["ok"] is False
    st = hw.get_state()
    assert st["heater"] is None and st["fan"] is None and st["source"] == "control-daemon-stale"


def test_non_daemon_mode_reads_sensor_directly():
    hw = HardwareService(make_gpio(), _Cam())
    assert hw.daemon_mode is False
    r = hw.read_environment()
    assert r["ok"] is True and r["mock"] is True
    assert hw.get_state()["heater"] is False


def test_daemon_mode_web_cannot_drive_heater(tmp_path):
    hw = HardwareService(make_gpio(owned_outputs=WEB_OUTPUTS, sensor=None), _Cam(), daemon_state_path=str(tmp_path / "s.json"))
    assert hw.set_heater(True)["error"] == "not_owned"
    assert hw.move_motor(200)["error"] == "not_owned"
    assert hw.set_candle(True)["ok"] is True
