"""GPIOService on the HAL: pin ownership, fail-loud sensors, mock fallback,
turner clamping and the setup button — all on the in-memory mock backend."""

import logging

import pytest

from app.hal.gpio import MockGpioBackend, parse_pin
from app.hal.sensors import MockSensor, Sensor, reading
from app.services.gpio_service import (
    DAEMON_OUTPUTS,
    OUTPUT_NAMES,
    TURNER_MAX_STEPS,
    WEB_OUTPUTS,
    GPIOService,
    clamp_turner_steps,
)

PINS = dict(
    dht_pin="4", heater_pin="17", fan_pin="27", turner_pin="22", turner_dir_pin="23",
    candle_pin="24", alarm_pin="25", lock_pin="12", door_pin="13", setup_button_pin="18",
)


def _svc(**kw):
    args = dict(PINS)
    args.setdefault("mock", True)
    args.update(kw)
    return GPIOService(**args)


class _DeadSensor(Sensor):
    name = "dead"

    def read(self):
        return reading(self.name, ok=False, error="checksum")


class _FixedSensor(Sensor):
    name = "fixed"

    def read(self):
        return reading(self.name, ok=True, temperature_c=36.8, humidity_pct=52.0)


# ── ownership / line requests ────────────────────────────────────────────────

def test_setup_requests_all_outputs_at_off_level_plus_button():
    svc = _svc()
    svc.setup()
    be = svc._backend
    assert isinstance(be, MockGpioBackend)
    assert set(svc._lines) == set(OUTPUT_NAMES)
    # Relays are active-LOW: OFF = HIGH. Logic lines: OFF = LOW.
    assert be.lines["17"].value is True and be.lines["27"].value is True
    assert be.lines["12"].value is True and be.lines["13"].value is True
    assert be.lines["24"].value is False and be.lines["25"].value is False
    assert be.lines["18"].direction == "in" and be.lines["18"].pull == "up"
    assert svc.backend_name == "mock" and svc.is_mock is True and svc.mock_fallback is False


def test_relay_active_high_option():
    svc = _svc(relay_active_low=False)
    svc.setup()
    assert svc._backend.lines["17"].value is False
    svc.set_heater(True)
    assert svc._backend.lines["17"].value is True


def test_set_heater_drives_relay_level_and_state():
    svc = _svc()
    svc.set_heater(True)
    assert svc._backend.lines["17"].value is False      # active-LOW: ON = LOW
    assert svc.get_state()["heater"] is True
    svc.set_heater(False)
    assert svc._backend.lines["17"].value is True
    assert svc.get_state()["heater"] is False


def test_web_instance_refuses_daemon_owned_outputs(caplog):
    svc = _svc(owned_outputs=WEB_OUTPUTS, sensor=None)
    svc.setup()
    assert set(svc._lines) == set(WEB_OUTPUTS)
    assert "17" not in svc._backend.lines                 # heater line never requested
    with caplog.at_level(logging.WARNING):
        r1 = svc.set_heater(True)
        r2 = svc.set_heater(True)
        r3 = svc.move_turner(200, 1)
    assert r1 == {"ok": False, "error": "not_owned", "output": "heater"}
    assert r2["error"] == "not_owned" and r3["error"] == "not_owned"
    assert sum("owned by another process" in rec.message and "'heater'" in rec.message for rec in caplog.records) == 1
    # Web-owned outputs still work.
    assert svc.set_alarm(True)["ok"] is True and svc.set_candle(True)["ok"] is True
    assert svc.set_lock(True)["ok"] is True and svc.set_door(True)["ok"] is True


def test_daemon_instance_owns_only_climate_lines():
    svc = _svc(owned_outputs=DAEMON_OUTPUTS)
    svc.setup()
    assert set(svc._lines) == set(DAEMON_OUTPUTS)
    assert svc.set_candle(True)["error"] == "not_owned"
    assert svc.set_heater(True)["ok"] is True
    assert svc.move_turner(200, 1)["ok"] is True


def test_unknown_owned_output_name_is_rejected():
    with pytest.raises(ValueError):
        _svc(owned_outputs={"heater", "laser"})


def test_cleanup_releases_lines():
    svc = _svc()
    svc.setup()
    svc.cleanup()
    assert svc._backend.lines == {} and svc._lines == {}


# ── sensors: never fabricate a reading ───────────────────────────────────────

def test_mock_mode_reads_mock_sensor():
    svc = _svc()
    r = svc.read_temperature_humidity()
    assert r["ok"] is True and r["mock"] is True
    assert abs(r["temperature_c"] - 37.4) < 0.5 and r["sensor"] == "mock"
    assert svc.sensor_name == "mock"


def test_sensor_none_reports_no_sensor():
    svc = _svc(sensor=None)
    r = svc.read_temperature_humidity()
    assert r == {"ok": False, "error": "no_sensor", "temperature_c": None, "humidity_pct": None, "mock": False, "sensor": None}
    assert svc.sensor_name is None


def test_injected_sensor_is_used():
    svc = _svc(sensor=_FixedSensor())
    r = svc.read_temperature_humidity()
    assert r["ok"] is True and r["mock"] is False and r["temperature_c"] == 36.8 and r["sensor"] == "fixed"


def test_failed_sensor_is_ok_false_never_mock_values(monkeypatch):
    monkeypatch.setattr("app.services.gpio_service.SENSOR_RETRY_SLEEP", 0.0)
    svc = _svc(sensor=_DeadSensor())
    r = svc.read_temperature_humidity()
    assert r["ok"] is False and r["temperature_c"] is None and r["humidity_pct"] is None
    assert r["mock"] is False and r["error"] == "checksum"


def test_missing_sensor_on_real_backend_is_no_sensor_not_mock(monkeypatch):
    """GPIO_MOCK=false + no detectable sensor must NOT become 37.4 degC."""
    import app.hal.gpio as hal_gpio
    import app.hal.sensors as hal_sensors

    monkeypatch.setattr(hal_gpio, "get_gpio_backend", lambda name="auto", chip="", **kw: MockGpioBackend(chip))
    monkeypatch.setattr(hal_sensors, "get_sensor", lambda *a, **kw: None)
    svc = _svc(mock=False)
    svc.setup()
    r = svc.read_temperature_humidity()
    assert r["ok"] is False and r["error"] == "no_sensor" and r["mock"] is False


def test_sensor_backend_mock_string_gives_mock_sensor():
    svc = _svc(mock=False, sensor="mock")
    svc._backend = MockGpioBackend()
    svc._sensor = svc._build_sensor()
    assert isinstance(svc._sensor, MockSensor)


# ── mock fallback must be loud and visible ───────────────────────────────────

def test_silent_mock_fallback_is_warned_and_exposed(monkeypatch, caplog):
    import app.hal.gpio as hal_gpio

    monkeypatch.setattr(hal_gpio, "get_gpio_backend", lambda name="auto", chip="", **kw: MockGpioBackend(chip))
    svc = _svc(mock=False, sensor=None)
    with caplog.at_level(logging.WARNING):
        svc.setup()
    assert svc.backend_name == "mock" and svc.is_mock is True and svc.mock_fallback is True
    assert any("MOCK backend although GPIO_MOCK is false" in rec.message for rec in caplog.records)
    assert svc.describe()["mock_fallback"] is True


def test_explicit_unavailable_backend_falls_back_to_mock_with_error(monkeypatch, caplog):
    import app.hal.gpio as hal_gpio

    def boom(name="auto", chip="", **kw):
        raise hal_gpio.GpioBackendUnavailable("no gpiochip")

    monkeypatch.setattr(hal_gpio, "get_gpio_backend", boom)
    svc = _svc(mock=False, backend="gpiod", sensor=None)
    with caplog.at_level(logging.ERROR):
        svc.setup()
    assert svc.backend_name == "mock" and svc.mock_fallback is True
    assert any("unavailable" in rec.message for rec in caplog.records)


def test_requested_mock_is_not_a_fallback():
    svc = _svc(mock=True)
    svc.setup()
    assert svc.mock_fallback is False and svc.describe()["mock"] is True


# ── turner ────────────────────────────────────────────────────────────────────

def test_clamp_turner_steps():
    assert clamp_turner_steps(200) == 200
    assert clamp_turner_steps(-200) == 200
    assert clamp_turner_steps(0) == 200
    assert clamp_turner_steps(None) == 200
    assert clamp_turner_steps("abc") == 200
    assert clamp_turner_steps(1) == 1
    assert clamp_turner_steps(100000) == TURNER_MAX_STEPS


def test_move_turner_clamps_and_sets_direction():
    svc = _svc()
    r = svc.move_turner(100000, -1)
    assert r == {"ok": True, "steps": TURNER_MAX_STEPS, "direction": -1}
    assert svc._backend.lines["23"].value is False         # reverse = dir LOW
    r = svc.move_turner(50, 1)
    assert r["direction"] == 1 and svc._backend.lines["23"].value is True
    assert svc.get_state()["turner"] is False


def test_move_turner_does_not_hold_service_lock(monkeypatch):
    """Stepping runs under the turner lock only; a heater write must not wait."""
    import threading
    import app.services.gpio_service as mod

    svc = _svc()
    svc.setup()
    svc._mock = False  # take the real stepping path (sleeps) on the mock lines
    monkeypatch.setattr(mod, "TURNER_STEP_SECONDS", 0.002)
    started = threading.Event()
    orig_set = svc._lines["turner"].set

    def slow_set(v):
        started.set()
        orig_set(v)

    svc._lines["turner"].set = slow_set
    t = threading.Thread(target=svc.move_turner, args=(400, 1), daemon=True)
    t.start()
    assert started.wait(2.0)
    import time as _t

    t0 = _t.monotonic()
    assert svc.set_heater(True)["ok"] is True
    assert _t.monotonic() - t0 < 0.5                       # not blocked behind 400 steps
    t.join(5.0)
    assert not t.is_alive()


# ── button ────────────────────────────────────────────────────────────────────

def test_read_button_via_mock_backend():
    svc = _svc()
    assert svc.read_button() is False                      # pulled up, idle
    svc.set_mock_button(True)
    assert svc.read_button() is True
    svc.set_mock_button(False)
    assert svc.read_button() is False


# ── alarm pulse ──────────────────────────────────────────────────────────────

def test_pulse_alarm_skipped_while_alarm_latched():
    svc = _svc()
    svc.set_alarm(True)
    assert svc.pulse_alarm(0.05) == {"ok": True, "pulsed": False, "alarm": True}
    svc.set_alarm(False)
    assert svc.pulse_alarm(0.05)["pulsed"] is True


def test_get_state_shape():
    svc = _svc()
    st = svc.get_state()
    assert set(OUTPUT_NAMES) <= set(st)
    assert st["backend"] == "mock" and st["mock"] is True
    assert parse_pin("17").offset == 17
