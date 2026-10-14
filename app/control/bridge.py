"""Bridge between the web app and the control daemon.

When the control daemon owns the heater/fan/turner/sensor (CONTROL_DAEMON_ENABLED),
the web app must not touch those pins. Instead it reads sensor state from the
daemon's state file and routes heater/fan/turner commands to the daemon's
command file. These helpers are pure/file-only so they're easy to test.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Web-app action name → (daemon command action, value). Other actions
# (candle/lock/door/alarm) stay with the web app — no pin contention.
_CONTROL_ACTIONS: dict[str, tuple[str, object]] = {
    "heater_on": ("heater", True),
    "heater_off": ("heater", False),
    "fan_on": ("fan", True),
    "fan_off": ("fan", False),
}


def control_command_for(action: str, value: object = None) -> tuple[str, object] | None:
    """Map a web-app hardware action to a daemon command, or None if the web
    app should handle it directly. A negative ``move_motor`` value is passed
    through unchanged: the daemon reads its sign as the direction."""
    if action in _CONTROL_ACTIONS:
        return _CONTROL_ACTIONS[action]
    if action == "move_motor":
        return ("turn", value if value is not None else 200)
    return None


def enqueue_command(path: str, action: str, value: object = None) -> None:
    """Append one command line. O_APPEND makes each write atomic for small
    lines, and creates a fresh file when the daemon has just claimed the old
    one by rename — nothing is ever lost between the two processes."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({"action": action, "value": value}) + "\n")


def read_control_state(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def daemon_reading(path: str, *, stale_after: float = 120.0, now: float | None = None) -> dict:
    """Adapt the daemon's published state into a sensor-reading dict shaped like
    GPIOService.read_temperature_humidity(), so the alert engine is unchanged.

    A missing or stale state file reads as offline (ok=False) — that is exactly
    the signal the alert engine needs if the control daemon stops publishing.
    The daemon's actuator state (``heater_on``/``fan_on``) and ``sensor_fault``
    ride along so the heater-current integrity check and the UI see what the
    daemon is actually doing.
    """
    now = time.time() if now is None else now
    state = read_control_state(path)
    if not state:
        return {
            "ok": False, "temperature_c": None, "humidity_pct": None, "source": "control-daemon",
            "heater_on": None, "fan_on": None, "sensor_fault": "", "mock": False, "fresh": False,
        }
    fresh = (now - float(state.get("ts", 0))) <= stale_after
    return {
        "ok": bool(state.get("online")) and fresh,
        "temperature_c": state.get("temperature_c"),
        "humidity_pct": state.get("humidity_pct"),
        "source": "control-daemon",
        "heater_on": bool(state.get("heater_on")) if fresh else None,
        "fan_on": bool(state.get("fan_on")) if fresh else None,
        "sensor_fault": str(state.get("sensor_fault") or ""),
        "mock": bool(state.get("mock", False)),
        "last_turn_ts": state.get("last_turn_ts"),
        "fresh": fresh,
    }
