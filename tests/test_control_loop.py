"""Control daemon — pure decision logic + one mock iteration (no hardware)."""

import json

from app.control.loop import (
    ControlDaemon,
    failsafe_outputs,
    humidity_decision,
    thermostat_decision,
    turn_due,
)


class FakeGPIO:
    def __init__(self, temp=37.5, hum=55.0, ok=True):
        self.temp, self.hum, self.ok = temp, hum, ok
        self.heater = None
        self.fan = None
        self.turns = 0

    def read_temperature_humidity(self):
        return {"ok": self.ok, "temperature_c": self.temp, "humidity_pct": self.hum}

    def set_heater(self, on):
        self.heater = on
        return {"ok": True}

    def set_fan(self, on):
        self.fan = on
        return {"ok": True}

    def move_turner(self, steps, direction):
        self.turns += 1
        return {"ok": True, "steps": steps}


def _settings(**over):
    base = {
        "target_temp_c": "37.5",
        "target_humidity_pct": "55",
        "alert_humidity_tolerance_pct": "10",
        "turn_interval_hours": "3",
        "turner_enabled": "true",
        "heater_enabled": "true",
    }
    base.update(over)
    return base


# ── pure logic ───────────────────────────────────────────────────────────

def test_thermostat_hysteresis_band():
    assert thermostat_decision(36.9, 37.5, 0.4) is True    # below band → heat
    assert thermostat_decision(38.1, 37.5, 0.4) is False   # above band → stop
    assert thermostat_decision(37.5, 37.5, 0.4) is None     # inside → hold
    assert thermostat_decision(None, 37.5, 0.4) is None      # no reading → hold (caller fail-safes)


def test_humidity_only_acts_in_fan_mode():
    assert humidity_decision(70, 55, 10, "fan") is True     # too humid → vent
    assert humidity_decision(40, 55, 10, "fan") is False    # too dry → stop venting
    assert humidity_decision(70, 55, 10, "off") is None      # monitor-only
    assert humidity_decision(None, 55, 10, "fan") is None


def test_turn_due():
    assert turn_due(10_000, None, 3) is True                 # never turned → due
    assert turn_due(10_000, 10_000 - 3 * 3600, 3) is True    # exactly interval
    assert turn_due(10_000, 10_000 - 100, 3) is False        # too soon
    assert turn_due(10_000, 0, 0) is False                   # disabled


def test_turn_due_clamps_backward_clock_jump():
    # Clock-sanity: a power cut on an RTC-less unit can boot the clock BEHIND
    # last_turn_ts. Without the clamp the negative delta would defer the turn
    # for hours; we treat it as due instead so eggs never miss a turn.
    assert turn_due(5_000, 9_000, 3) is True                 # now < last_turn → due
    assert turn_due(9_000 - 1, 9_000, 3) is True             # just behind → due


def test_failsafe_is_heater_off():
    assert failsafe_outputs()["heater"] is False


# ── one iteration with a mock GPIO ────────────────────────────────────────

def _daemon(gpio, tmp_path, settings=None, **kw):
    snapshot = _settings(**(settings or {}))
    return ControlDaemon(
        gpio,
        settings_provider=lambda: snapshot,
        state_path=str(tmp_path / "state.json"),
        command_path=str(tmp_path / "cmd.jsonl"),
        **kw,
    )


def test_cold_reading_turns_heater_on_and_writes_state(tmp_path):
    gpio = FakeGPIO(temp=36.5)
    d = _daemon(gpio, tmp_path)
    state = d.loop_once(now_ts=1000.0)
    assert gpio.heater is True
    assert state["heater_on"] is True
    assert gpio.turns == 1  # first iteration: never-turned → due
    written = json.loads((tmp_path / "state.json").read_text())
    assert written["temperature_c"] == 36.5 and written["online"] is True


def test_shipped_defaults_enable_auto_temperature_control():
    """Safety default: a flashed unit must regulate temperature out of the box.

    Both gates have to be on — the control daemon (env) AND heater_enabled
    (seeded setting) — or the appliance would monitor and alarm but never
    actually hold target temperature.
    """
    import pathlib

    from app.settings_store import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["heater_enabled"] == "true"

    env = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "incubator.env.example"
    assert "CONTROL_DAEMON_ENABLED=true" in env.read_text()


def test_sensor_failure_fails_safe_heater_off(tmp_path):
    gpio = FakeGPIO(ok=False)
    d = _daemon(gpio, tmp_path)
    # Pretend the heater was on from a prior good reading.
    d._heater_on = True
    state = d.loop_once(now_ts=2000.0)
    assert gpio.heater is False and state["heater_on"] is False


def test_heater_disabled_forces_off(tmp_path):
    gpio = FakeGPIO(temp=30.0)  # very cold, but heater disabled
    d = _daemon(gpio, tmp_path, settings={"heater_enabled": "false"})
    d._heater_on = True  # was running → disabling must drive it off
    d.loop_once(now_ts=3000.0)
    assert gpio.heater is False


def test_build_daemon_wires_from_settings():
    # Guards against config drift: build_daemon() reads every control_* setting,
    # so a missing field (e.g. dropped in a merge) fails here instead of only at
    # runtime on the device.
    from app.control.loop import build_daemon

    daemon = build_daemon()
    assert daemon._interval > 0
    assert daemon._hysteresis >= 0
    assert daemon._state_path and daemon._command_path


def test_command_file_is_drained_and_applied(tmp_path):
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path)
    (tmp_path / "cmd.jsonl").write_text(json.dumps({"action": "heater", "value": True}) + "\n")
    d.loop_once(now_ts=4000.0)
    assert gpio.heater is True
    # command file is truncated after draining
    assert (tmp_path / "cmd.jsonl").read_text() == ""


# ── Sensor integrity: an "ok" reading is not automatically true ──────────────

def test_implausible_reading_fails_safe_not_forever_heat(tmp_path):
    """A glitched-LOW reading must NOT command permanent heating.

    -40 C reads as 'always below target' to a naive thermostat — the single
    most dangerous sensor failure mode for a bang-bang heater.
    """
    gpio = FakeGPIO(temp=-40.0)
    d = _daemon(gpio, tmp_path)
    d._heater_on = True
    state = d.loop_once(now_ts=1000.0)
    assert gpio.heater is False
    assert state["sensor_fault"] == "implausible"
    assert state["online"] is False       # the UI must not show it as healthy


def test_implausibly_high_reading_also_rejected(tmp_path):
    gpio = FakeGPIO(temp=95.0)
    d = _daemon(gpio, tmp_path)
    d._heater_on = True                       # was heating on a prior good read
    state = d.loop_once(now_ts=1000.0)
    assert gpio.heater is False and state["sensor_fault"] == "implausible"


def test_latched_sensor_detected_and_fails_safe(tmp_path):
    """A sensor frozen at a plausible value is treated as failed.

    While 'stuck at 36.0' looks fine, the loop is flying blind: it would hold
    the heater ON indefinitely against a number that no longer tracks reality.
    """
    gpio = FakeGPIO(temp=36.0)
    d = _daemon(gpio, tmp_path)
    # Same value every iteration for 15 minutes.
    for t in range(0, 900, 60):
        state = d.loop_once(now_ts=1000.0 + t)
        assert state["sensor_fault"] == ""       # not yet — still plausible
    state = d.loop_once(now_ts=1000.0 + 900)
    assert state["sensor_fault"] == "stuck"
    assert gpio.heater is False and state["online"] is False


def test_normal_jitter_never_looks_stuck(tmp_path):
    """Real readings vary — a healthy sensor must never be flagged."""
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path)
    for i, t in enumerate(range(0, 3600, 60)):
        gpio.temp = 37.5 + (0.1 if i % 2 else 0.0)   # ±0.1 C jitter at target
        state = d.loop_once(now_ts=1000.0 + t)
        assert state["sensor_fault"] == ""
    assert state["online"] is True


def test_heat_runaway_forces_heater_off(tmp_path):
    """45 min of heat with no rise = sensor misplaced or heater failing."""
    gpio = FakeGPIO(temp=25.0)
    d = _daemon(gpio, tmp_path)
    d.loop_once(now_ts=1000.0)                # cold → heater ON
    assert gpio.heater is True
    # Time passes with the temperature refusing to rise. Nudge the reading each
    # iteration so the latch detector isn't what trips first.
    state = None
    for i, t in enumerate(range(60, 3000, 60)):
        gpio.temp = 25.0 + (0.05 if i % 2 else 0.0)
        state = d.loop_once(now_ts=1000.0 + t)
    assert state["sensor_fault"] == "heat_runaway"
    assert gpio.heater is False               # latched off, not still cooking


def test_normal_warmup_is_not_flagged_as_runaway(tmp_path):
    """A chamber that IS warming must keep its heat — no false trip."""
    gpio = FakeGPIO(temp=22.0)
    d = _daemon(gpio, tmp_path)
    d.loop_once(now_ts=1000.0)
    state = None
    for i, t in enumerate(range(60, 3000, 60)):
        gpio.temp = 22.0 + 0.05 * i          # realistic PTC ramp
        state = d.loop_once(now_ts=1000.0 + t)
    assert state["sensor_fault"] == ""
    assert gpio.heater is True


def test_mock_sensor_never_flagged_as_stuck(tmp_path):
    """The mock sensor returns a fixed value BY DESIGN.

    Without this exemption every dev box, demo unit, and sensor-less build
    would raise a sensor fault (and buzz) once the latch window elapsed.
    """
    class MockGPIO(FakeGPIO):
        def read_temperature_humidity(self):
            return {"ok": True, "mock": True, "temperature_c": 37.4, "humidity_pct": 55.0}

    d = _daemon(MockGPIO(), tmp_path)
    state = None
    for t in range(0, 7200, 60):          # two hours of identical readings
        state = d.loop_once(now_ts=1000.0 + t)
    assert state["sensor_fault"] == "" and state["online"] is True
