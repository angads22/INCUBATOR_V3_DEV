"""``python -m app.hal --safe-outputs``: boot-safe actuator levels on any board."""

import json
from types import SimpleNamespace

from app.hal import __main__ as hal_cli


def _settings(**over):
    base = dict(
        gpio_backend="mock", gpio_chip="", gpio_mock=False, gpio_relay_active_low=True,
        gpio_heater_pin="17", gpio_fan_pin="27", gpio_lock_pin="12", gpio_door_pin="13",
        gpio_turner_pin="22", gpio_turner_dir_pin="23", gpio_candle_pin="24", gpio_alarm_pin="25",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_safe_outputs_drives_relays_high_and_actuators_low():
    report = hal_cli.safe_outputs(_settings())
    assert report["backend"] == "mock" and report["ok"] is True
    lines = report["lines"]
    assert set(lines) == {"heater", "fan", "lock", "door", "turner", "turner_dir", "candle", "alarm"}
    for relay in ("heater", "fan", "lock", "door"):
        assert lines[relay] == {"pin": lines[relay]["pin"], "level": "high", "ok": True}
    for plain in ("turner", "turner_dir", "candle", "alarm"):
        assert lines[plain]["level"] == "low" and lines[plain]["ok"] is True


def test_safe_outputs_active_high_relays_go_low():
    report = hal_cli.safe_outputs(_settings(gpio_relay_active_low=False))
    assert all(report["lines"][r]["level"] == "low" for r in ("heater", "fan", "lock", "door"))


def test_safe_outputs_never_raises_on_bad_pin():
    report = hal_cli.safe_outputs(_settings(gpio_heater_pin="not a pin!!"))
    assert report["ok"] is False
    assert report["lines"]["heater"]["ok"] is False and "error" in report["lines"]["heater"]
    assert report["lines"]["fan"]["ok"] is True   # the rest still ran


def test_cli_safe_outputs_flag_exits_zero_and_prints_json(capsys):
    assert hal_cli.main(["--safe-outputs"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "lines" in out and "backend" in out
