"""Web app ↔ control daemon bridge (file-based, pure)."""

import json

from app.control.bridge import (
    control_command_for,
    daemon_reading,
    enqueue_command,
    read_control_state,
)


def test_control_command_mapping():
    assert control_command_for("heater_on") == ("heater", True)
    assert control_command_for("heater_off") == ("heater", False)
    assert control_command_for("fan_on") == ("fan", True)
    assert control_command_for("move_motor", 400) == ("turn", 400)
    assert control_command_for("move_motor") == ("turn", 200)
    # Non-daemon actions are handled by the web app directly.
    assert control_command_for("open_door") is None
    assert control_command_for("set_candle", "on") is None


def test_enqueue_command_appends_jsonl(tmp_path):
    path = str(tmp_path / "cmd.jsonl")
    enqueue_command(path, "heater", True)
    enqueue_command(path, "turn", 200)
    lines = [json.loads(line) for line in open(path).read().splitlines()]
    assert lines == [{"action": "heater", "value": True}, {"action": "turn", "value": 200}]


def test_daemon_reading_fresh(tmp_path):
    path = str(tmp_path / "state.json")
    open(path, "w").write(json.dumps({"ts": 1000.0, "online": True, "temperature_c": 37.6, "humidity_pct": 55.0}))
    r = daemon_reading(path, now=1005.0)
    assert r["ok"] is True and r["temperature_c"] == 37.6 and r["source"] == "control-daemon"


def test_daemon_reading_stale_is_offline(tmp_path):
    path = str(tmp_path / "state.json")
    open(path, "w").write(json.dumps({"ts": 1000.0, "online": True, "temperature_c": 37.6}))
    r = daemon_reading(path, now=2000.0, stale_after=120.0)  # >120s old → offline
    assert r["ok"] is False


def test_daemon_reading_missing_file_is_offline(tmp_path):
    r = daemon_reading(str(tmp_path / "nope.json"))
    assert r["ok"] is False and r["temperature_c"] is None
    assert read_control_state(str(tmp_path / "nope.json")) is None
