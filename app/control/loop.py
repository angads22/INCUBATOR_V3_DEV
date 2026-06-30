"""Closed-loop incubation control: thermostat, egg-turn scheduler, fail-safe.

The pure decision functions (``thermostat_decision``, ``humidity_decision``,
``turn_due``, ``failsafe_outputs``) carry the safety logic and are unit-tested
without any hardware. ``ControlDaemon`` wires them to a GPIOService, reads
targets from the settings store, drains a command file from the web app, and
publishes a state/health file the web app can read — all without the web app
needing to own the heater/turner pins.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Outputs the loop drives. The web app keeps candle/lock/door/alarm.
HEATER = "heater"
FAN = "fan"
TURNER = "turner"


# ──────────────────────────────────────────────────────────────────────────
# Pure decision logic (unit-tested, no hardware)
# ──────────────────────────────────────────────────────────────────────────

def thermostat_decision(temp_c: float | None, target_c: float, hysteresis_c: float) -> bool | None:
    """Desired heater state, or None to hold the current state.

    Bang-bang control with a hysteresis band so the relay doesn't chatter:
      * at/below target - hysteresis → heat (True)
      * at/above target + hysteresis → stop (False)
      * inside the band → None (no change)
    A missing reading returns None here; the caller applies the fail-safe.
    """
    if temp_c is None:
        return None
    if temp_c <= target_c - hysteresis_c:
        return True
    if temp_c >= target_c + hysteresis_c:
        return False
    return None


def humidity_decision(hum_pct: float | None, target_pct: float, tolerance_pct: float, mode: str) -> bool | None:
    """Desired vent-fan state for humidity, or None to hold.

    Only meaningful in ``mode == "fan"`` (vent to lower RH; a passive water tray
    raises it). Any other mode is monitor-only and returns None.
    """
    if mode != "fan" or hum_pct is None:
        return None
    if hum_pct >= target_pct + tolerance_pct:
        return True   # too humid → vent
    if hum_pct <= target_pct - tolerance_pct:
        return False  # too dry → stop venting, let the tray recover
    return None


def turn_due(now_ts: float, last_turn_ts: float | None, interval_hours: float) -> bool:
    """Whether the egg turner is due to run."""
    if interval_hours <= 0:
        return False
    if last_turn_ts is None:
        return True
    return (now_ts - last_turn_ts) >= interval_hours * 3600.0


def failsafe_outputs() -> dict[str, bool]:
    """Defined safe state for the reboot/restart window.

    Heater OFF is the safety default — a few seconds without heat is
    thermally irrelevant, but a stuck-ON heater is not. Turner idle.
    """
    return {HEATER: False, TURNER: False}


# ──────────────────────────────────────────────────────────────────────────
# Daemon
# ──────────────────────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ControlDaemon:
    def __init__(
        self,
        gpio: Any,
        *,
        settings_provider: Callable[[], dict],
        interval_seconds: float = 10.0,
        hysteresis_c: float = 0.4,
        turn_interval_hours: float = 3.0,
        humidity_mode: str = "off",
        state_path: str = "/run/incubator/control-state.json",
        command_path: str = "/run/incubator/control-commands.jsonl",
    ) -> None:
        self._gpio = gpio
        self._settings_provider = settings_provider
        self._interval = interval_seconds
        self._hysteresis = hysteresis_c
        self._turn_interval_hours = turn_interval_hours
        self._humidity_mode = humidity_mode
        self._state_path = state_path
        self._command_path = command_path
        self._stop = threading.Event()
        self._last_turn_ts: float | None = None
        self._heater_on = False
        self._fan_on = False

    # -- settings helpers -------------------------------------------------
    @staticmethod
    def _f(settings: dict, key: str, default: float) -> float:
        try:
            return float(settings.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _b(settings: dict, key: str, default: bool) -> bool:
        return str(settings.get(key, "true" if default else "false")).lower() == "true"

    # -- one control iteration (testable) ---------------------------------
    def loop_once(self, now_ts: float | None = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        settings = self._settings_provider() or {}
        reading = self._gpio.read_temperature_humidity()
        temp = reading.get("temperature_c") if reading.get("ok") else None
        hum = reading.get("humidity_pct") if reading.get("ok") else None

        target_t = self._f(settings, "target_temp_c", 37.5)
        target_h = self._f(settings, "target_humidity_pct", 55.0)
        hum_tol = self._f(settings, "alert_humidity_tolerance_pct", 10.0)
        turn_interval = self._f(settings, "turn_interval_hours", self._turn_interval_hours)
        turner_enabled = self._b(settings, "turner_enabled", True)
        heater_enabled = self._b(settings, "heater_enabled", True)

        # Heater: if we can't read the sensor, fail safe (heater OFF) rather
        # than holding a possibly-stale ON state.
        if not heater_enabled:
            want_heat: bool | None = False
        elif temp is None:
            want_heat = failsafe_outputs()[HEATER]
        else:
            want_heat = thermostat_decision(temp, target_t, self._hysteresis)
        if want_heat is not None and want_heat != self._heater_on:
            self._gpio.set_heater(want_heat)
            self._heater_on = want_heat

        # Humidity (optional vent-fan mode).
        want_fan = humidity_decision(hum, target_h, hum_tol, self._humidity_mode)
        if want_fan is not None and want_fan != self._fan_on:
            self._gpio.set_fan(want_fan)
            self._fan_on = want_fan

        # Egg turning on schedule.
        turned = False
        if turner_enabled and turn_due(now_ts, self._last_turn_ts, turn_interval):
            self._gpio.move_turner(200, 1)
            self._last_turn_ts = now_ts
            turned = True

        self._drain_commands()

        state = {
            "ok": True,
            "ts": now_ts,
            "temperature_c": temp,
            "humidity_pct": hum,
            "target_temp_c": target_t,
            "heater_on": self._heater_on,
            "fan_on": self._fan_on,
            "turned": turned,
            "last_turn_ts": self._last_turn_ts,
            "online": bool(reading.get("ok")),
        }
        self._publish_state(state)
        return state

    # -- command intake (web app → daemon) --------------------------------
    def _drain_commands(self) -> None:
        path = self._command_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r+") as fh:
                lines = fh.readlines()
                fh.seek(0)
                fh.truncate()
        except OSError as exc:
            logger.debug("command drain read error: %s", exc)
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
                self._apply_command(cmd.get("action"), cmd.get("value"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("bad control command %r: %s", line, exc)

    def _apply_command(self, action: str, value: Any) -> None:
        if action == "heater":
            on = bool(value)
            self._gpio.set_heater(on)
            self._heater_on = on
        elif action == "fan":
            on = bool(value)
            self._gpio.set_fan(on)
            self._fan_on = on
        elif action == "turn":
            self._gpio.move_turner(int(value) if value else 200, 1)
            self._last_turn_ts = time.time()
        else:
            logger.debug("ignoring unknown control command: %s", action)

    def _publish_state(self, state: dict) -> None:
        try:
            _atomic_write_json(self._state_path, state)
        except OSError as exc:
            logger.debug("state publish error: %s", exc)

    # -- lifecycle --------------------------------------------------------
    def apply_failsafe(self) -> None:
        for output, value in failsafe_outputs().items():
            try:
                if output == HEATER:
                    self._gpio.set_heater(value)
                    self._heater_on = value
            except Exception as exc:  # noqa: BLE001
                logger.warning("fail-safe set %s failed: %s", output, exc)

    def run(self) -> None:
        logger.info("Control daemon starting (interval=%ss)", self._interval)
        self.apply_failsafe()
        while not self._stop.is_set():
            try:
                self.loop_once()
            except Exception as exc:  # noqa: BLE001 — a bad iteration must not kill control
                logger.warning("control iteration error: %s", exc)
            self._stop.wait(self._interval)
        # On a clean stop, leave outputs in the defined safe state.
        self.apply_failsafe()
        logger.info("Control daemon stopped")

    def stop(self) -> None:
        self._stop.set()


def _default_settings_provider() -> dict:
    from ..database import get_db
    from ..settings_store import get_settings

    db = next(get_db())
    try:
        return get_settings(db)
    finally:
        db.close()


def build_daemon() -> ControlDaemon:
    from ..config import settings
    from ..services.gpio_service import GPIOService

    gpio = GPIOService(
        dht_pin=settings.gpio_dht_pin,
        heater_pin=settings.gpio_heater_pin,
        fan_pin=settings.gpio_fan_pin,
        turner_pin=settings.gpio_turner_pin,
        turner_dir_pin=settings.gpio_turner_dir_pin,
        candle_pin=settings.gpio_candle_pin,
        alarm_pin=settings.gpio_alarm_pin,
        lock_pin=settings.gpio_lock_pin,
        door_pin=settings.gpio_door_pin,
        setup_button_pin=settings.gpio_setup_button_pin,
        relay_active_low=settings.gpio_relay_active_low,
        mock=settings.gpio_mock,
    )
    gpio.setup()
    return ControlDaemon(
        gpio,
        settings_provider=_default_settings_provider,
        interval_seconds=settings.control_interval_seconds,
        hysteresis_c=settings.control_hysteresis_c,
        turn_interval_hours=settings.turn_interval_hours,
        humidity_mode=settings.humidity_control_mode,
        state_path=settings.control_state_path,
        command_path=settings.control_command_path,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    daemon = build_daemon()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: daemon.stop())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    daemon.run()


if __name__ == "__main__":
    main()
