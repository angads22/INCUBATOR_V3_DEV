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


def test_command_file_is_drained_and_applied(tmp_path):
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path)
    (tmp_path / "cmd.jsonl").write_text(json.dumps({"action": "heater", "value": True}) + "\n")
    d.loop_once(now_ts=4000.0)
    assert gpio.heater is True
    # command file is truncated after draining
    assert (tmp_path / "cmd.jsonl").read_text() == ""
