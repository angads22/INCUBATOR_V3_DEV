"""Closed-loop incubation control: thermostat, egg-turn scheduler, fail-safe.

The pure decision functions (``thermostat_decision``, ``humidity_decision``,
``turn_due``, ``failsafe_outputs``) carry the safety logic and are unit-tested
without any hardware. ``ControlDaemon`` wires them to a GPIOService, reads
targets from the settings store, drains a command file from the web app, and
publishes a state/health file the web app can read — all without the web app
needing to own the heater/turner pins.

This module is imported by the control daemon process, which must stay small
(512 MB boards): nothing here imports SQLAlchemy or the ORM models — settings
are read with the stdlib ``sqlite3`` module.
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

# Hard safety bounds on the values the loop will PURSUE, regardless of what
# the settings store says. The DB is user-writable and power-cut-corruptible;
# a target of "100" (bad API write, bit-rot, unit confusion) must never make
# the loop cook the eggs. These mirror the UI's input limits — anything
# outside them is clamped, and the clamp is logged so it's visible.
TEMP_TARGET_MIN_C = 20.0
TEMP_TARGET_MAX_C = 42.0
HUMIDITY_TARGET_MIN_PCT = 20.0
HUMIDITY_TARGET_MAX_PCT = 95.0

# ── Sensor integrity: a reading that says "ok" is not automatically true ──
# A DHT22 can fail in ways that still report success, and both are dangerous
# with a bang-bang thermostat:
#   * glitched LOW (e.g. -40 °C) → reads as "always below target" → the heater
#     runs forever;
#   * LATCHED (the same value returned indefinitely — a known DHT failure) →
#     the loop flies blind on a stale number, so it either bakes the eggs or
#     lets them go cold while every indicator looks healthy.
# Both routes therefore end where a dead sensor ends: the fail-safe (heater
# OFF), because that is the only safe answer to "I don't know the temperature".
SENSOR_PLAUSIBLE_MIN_C = 0.0    # a working incubator chamber is never colder
SENSOR_PLAUSIBLE_MAX_C = 60.0   # ...nor hotter; outside this the sensor is lying
# Real DHT22 readings jitter ~±0.1 °C, so bit-identical temperature AND humidity
# for this long means the sensor latched, not that the chamber is that stable.
SENSOR_STUCK_SECONDS = 900.0    # 15 minutes

# Anti-runaway: continuous heat with no temperature response means the sensor is
# misplaced/latched or the heater is failing. Warm-up from room temperature
# legitimately takes ~20-30 min, so the window is generous — but past it, with
# essentially no rise, holding the heater ON is the dangerous choice.
HEAT_RUNAWAY_SECONDS = 2700.0   # 45 minutes of continuous heat
HEAT_RUNAWAY_MIN_RISE_C = 0.5   # ...with less rise than this = something's wrong
# Once latched OFF, re-check every 15 min: if the chamber has COOLED by at
# least this much with the heater off, the sensor is evidently coupled to the
# heater (it was just a weak heater / open door), so heating may resume with a
# fresh baseline. No cooling → the sensor is not where the heat is → stay off.
RUNAWAY_RECHECK_SECONDS = 900.0
RUNAWAY_CLEAR_DROP_C = 0.3

# A full scheduled egg turn. Shorter manual nudges do not count as a turn.
TURN_STEPS = 200


def reading_plausible(temp_c: float | None) -> bool:
    """False when a reading is outside the physically possible chamber range.

    Critically this catches the glitched-LOW case, which a naive thermostat
    would answer with permanent heating.
    """
    if temp_c is None:
        return False
    return SENSOR_PLAUSIBLE_MIN_C <= temp_c <= SENSOR_PLAUSIBLE_MAX_C


def sensor_stuck(
    temp_c: float | None,
    hum_pct: float | None,
    last_temp: float | None,
    last_hum: float | None,
    unchanged_since_ts: float | None,
    now_ts: float,
    window_seconds: float = SENSOR_STUCK_SECONDS,
) -> tuple[bool, float | None]:
    """Detect a latched sensor. Returns ``(is_stuck, unchanged_since_ts)``.

    The caller carries ``unchanged_since_ts`` between iterations: it marks when
    the currently-repeating value was first seen, and resets the moment any
    reading changes.
    """
    if temp_c is None:
        return False, None
    if (temp_c != last_temp) or (hum_pct != last_hum) or unchanged_since_ts is None:
        return False, now_ts
    return (now_ts - unchanged_since_ts) >= window_seconds, unchanged_since_ts


def heat_runaway(
    heater_on: bool,
    temp_c: float | None,
    heat_start_ts: float | None,
    heat_start_temp: float | None,
    now_ts: float,
    window_seconds: float = HEAT_RUNAWAY_SECONDS,
    min_rise_c: float = HEAT_RUNAWAY_MIN_RISE_C,
) -> bool:
    """True when the heater has run continuously without warming the chamber."""
    if not heater_on or temp_c is None or heat_start_ts is None or heat_start_temp is None:
        return False
    if (now_ts - heat_start_ts) < window_seconds:
        return False
    return (temp_c - heat_start_temp) < min_rise_c


def runaway_clear(
    latch_ts: float | None,
    latch_temp: float | None,
    temp_c: float | None,
    now_ts: float,
    recheck_seconds: float = RUNAWAY_RECHECK_SECONDS,
    min_drop_c: float = RUNAWAY_CLEAR_DROP_C,
) -> bool | None:
    """Decide whether a heat-runaway latch may clear.

    Returns None while the re-check window has not elapsed, True when the
    chamber cooled by ``min_drop_c`` since the latch (clear it), False when it
    did not (keep it latched; the caller restarts the window).
    """
    if latch_ts is None or (now_ts - latch_ts) < recheck_seconds:
        return None
    if temp_c is None or latch_temp is None:
        return False
    return (latch_temp - temp_c) >= min_drop_c


def clamp_target(value: float, lo: float, hi: float, name: str = "target") -> float:
    """Clamp a control target into its safe band (logs when it acts)."""
    if value < lo:
        logger.warning("%s %.1f below safe minimum — clamped to %.1f", name, value, lo)
        return lo
    if value > hi:
        logger.warning("%s %.1f above safe maximum — clamped to %.1f", name, value, hi)
        return hi
    return value


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
    """Whether the egg turner is due to run.

    Clock-sanity: a backward wall-clock jump (power cut on an RTC-less unit,
    or a bad NTP step) makes ``now_ts - last_turn_ts`` negative, which would
    otherwise defer the next turn for hours. If the clock has rolled back
    behind the last turn, treat a turn as due rather than waiting out a bogus
    interval — a slightly early turn is harmless, a skipped one is not.
    """
    if interval_hours <= 0:
        return False
    if last_turn_ts is None:
        return True
    if now_ts < last_turn_ts:
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


def _read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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
        persist_path: str | None = None,
    ) -> None:
        self._gpio = gpio
        self._settings_provider = settings_provider
        self._interval = interval_seconds
        self._hysteresis = hysteresis_c
        self._turn_interval_hours = turn_interval_hours
        self._humidity_mode = humidity_mode
        self._state_path = state_path
        self._command_path = command_path
        self._persist_path = persist_path
        self._stop = threading.Event()
        self._last_turn_ts: float | None = None
        self._heater_on = False
        self._fan_on = False
        # Last settings snapshot that was read successfully. When the store is
        # unreadable (locked / corrupt DB) the loop keeps regulating on these
        # instead of skipping the iteration with the heater frozen in place.
        self._last_settings: dict | None = None
        self._settings_failing = False
        # Sensor-integrity state: last reading (latch detection) and when the
        # currently-repeating value was first seen.
        self._last_temp: float | None = None
        self._last_hum: float | None = None
        self._unchanged_since_ts: float | None = None
        # Anti-runaway state: start of the current continuous heating stretch,
        # and (while latched) when/at what temperature the latch engaged.
        self._heat_start_ts: float | None = None
        self._heat_start_temp: float | None = None
        self._runaway_latched = False
        # Names of outputs whose last write failed (line not requested / backend
        # error); published so the alert engine can tell the operator.
        self._output_fault = ""
        self._runaway_latch_ts: float | None = None
        self._runaway_latch_temp: float | None = None
        self._load_persisted()

    # -- persistence (survives restarts; not tmpfs) ------------------------
    def _load_persisted(self) -> None:
        if not self._persist_path:
            return
        data = _read_json(self._persist_path)
        ts = data.get("last_turn_ts")
        if isinstance(ts, (int, float)) and ts > 0:
            self._last_turn_ts = float(ts)
            logger.info("Restored last egg turn from %s (%.0f s ago)", self._persist_path, time.time() - ts)

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            _atomic_write_json(self._persist_path, {"last_turn_ts": self._last_turn_ts})
        except OSError as exc:
            logger.warning("could not persist control state to %s: %s", self._persist_path, exc)

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

    def _get_settings(self) -> dict:
        """Fetch settings; on failure reuse the last good snapshot (or defaults).

        Logged once per outage (and once on recovery) so a flapping SD card
        does not flood the journal every 10 s.
        """
        try:
            fetched = self._settings_provider()
        except Exception as exc:  # noqa: BLE001 — the loop must keep regulating
            if not self._settings_failing:
                self._settings_failing = True
                logger.error(
                    "settings store unreadable (%s) — continuing with %s",
                    exc, "the last good settings" if self._last_settings is not None else "built-in defaults",
                )
            return dict(self._last_settings) if self._last_settings is not None else {}
        if fetched is None:
            fetched = {}
        if self._settings_failing:
            self._settings_failing = False
            logger.info("settings store readable again")
        self._last_settings = dict(fetched)
        return dict(fetched)

    # -- one control iteration (testable) ---------------------------------
    def loop_once(self, now_ts: float | None = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        settings = self._get_settings()
        reading = self._gpio.read_temperature_humidity()
        temp = reading.get("temperature_c") if reading.get("ok") else None
        hum = reading.get("humidity_pct") if reading.get("ok") else None

        # ── Sensor integrity: distrust an "ok" reading that can't be true ──
        # Mock readings are exempt: the mock sensor deliberately returns a fixed
        # value, so integrity checks would flag every dev/demo/sensor-less unit
        # as faulty after the latch window.
        sensor_fault = ""
        is_mock = bool(reading.get("mock"))
        if is_mock:
            pass
        elif temp is not None and not reading_plausible(temp):
            sensor_fault = "implausible"
            logger.error(
                "Sensor reported %.1f C — outside the possible range; treating as failed",
                temp,
            )
        if not sensor_fault and not is_mock:
            stuck, self._unchanged_since_ts = sensor_stuck(
                temp, hum, self._last_temp, self._last_hum,
                self._unchanged_since_ts, now_ts,
            )
            if stuck:
                sensor_fault = "stuck"
                logger.error(
                    "Sensor frozen at %.1f C for >%.0f min — treating as failed",
                    temp, SENSOR_STUCK_SECONDS / 60.0,
                )
        self._last_temp, self._last_hum = temp, hum
        if sensor_fault:
            # Same treatment as a dead sensor: we do not know the temperature.
            temp = hum = None
        # A heating stretch that began without a valid reading (manual ON while
        # the sensor was failing) adopts the first good temperature as its
        # anti-runaway baseline instead of running unguarded.
        if (
            self._heater_on and self._heat_start_ts is not None
            and self._heat_start_temp is None and temp is not None
        ):
            self._heat_start_temp = temp

        target_t = clamp_target(
            self._f(settings, "target_temp_c", 37.5),
            TEMP_TARGET_MIN_C, TEMP_TARGET_MAX_C, "target_temp_c",
        )
        target_h = clamp_target(
            self._f(settings, "target_humidity_pct", 55.0),
            HUMIDITY_TARGET_MIN_PCT, HUMIDITY_TARGET_MAX_PCT, "target_humidity_pct",
        )
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

        # Anti-runaway: heat that isn't warming the chamber means the sensor is
        # misplaced/latched or the heater is failing — either way, continuing to
        # call for heat is the dangerous choice. Latch it OFF, then re-check
        # periodically whether the chamber cools with the heater off (sensor is
        # coupled to the heater → safe to resume) or not (stay latched).
        if not self._runaway_latched and heat_runaway(
            self._heater_on, temp, self._heat_start_ts, self._heat_start_temp, now_ts,
        ):
            logger.error(
                "Heater ran %.0f min with <%.1f C rise — forcing OFF (sensor or heater fault); "
                "re-checking in %.0f min",
                HEAT_RUNAWAY_SECONDS / 60.0, HEAT_RUNAWAY_MIN_RISE_C, RUNAWAY_RECHECK_SECONDS / 60.0,
            )
            self._runaway_latched = True
            self._runaway_latch_ts, self._runaway_latch_temp = now_ts, temp
        elif self._runaway_latched:
            verdict = runaway_clear(self._runaway_latch_ts, self._runaway_latch_temp, temp, now_ts)
            if verdict is True:
                logger.info(
                    "Chamber cooled %.1f -> %.1f C with the heater off — clearing heat-runaway latch",
                    self._runaway_latch_temp, temp,
                )
                self._runaway_latched = False
                self._runaway_latch_ts = self._runaway_latch_temp = None
                self._heat_start_ts = self._heat_start_temp = None
            elif verdict is False:
                logger.warning(
                    "Heat-runaway latch holds: chamber did not cool with the heater off (%.1f -> %s C); "
                    "re-checking in %.0f min",
                    self._runaway_latch_temp if self._runaway_latch_temp is not None else float("nan"),
                    "?" if temp is None else f"{temp:.1f}", RUNAWAY_RECHECK_SECONDS / 60.0,
                )
                self._runaway_latch_ts = now_ts
                if temp is not None:
                    self._runaway_latch_temp = temp
        if self._runaway_latched:
            want_heat = False
            # Report for as long as the latch holds — not just on the iteration
            # it tripped. Once the heater is forced off the runaway condition
            # stops being true, so a trip-only report would leave the UI
            # showing a healthy sensor while heat is actually suppressed.
            sensor_fault = sensor_fault or "heat_runaway"

        if want_heat is not None and want_heat != self._heater_on:
            self._heater_on = want_heat
            # Track the start of each continuous heating stretch so the
            # runaway check has a baseline to compare against.
            if want_heat:
                self._heat_start_ts, self._heat_start_temp = now_ts, temp
            else:
                self._heat_start_ts = self._heat_start_temp = None

        # Humidity (optional vent-fan mode).
        want_fan = humidity_decision(hum, target_h, hum_tol, self._humidity_mode)
        if want_fan is not None and want_fan != self._fan_on:
            self._fan_on = want_fan

        # Re-assert BOTH outputs every iteration, changed or not: the web app
        # (or a glitch, or a restart of the other process) can never leave a
        # relay in a state the daemon does not know about for longer than one
        # interval.
        heater_res = self._gpio.set_heater(self._heater_on) or {}
        fan_res = self._gpio.set_fan(self._fan_on) or {}
        # A write that did not land (line never requested — EBUSY, bad pin —
        # or a backend error) means the published state would be fiction.
        # Record it so the alert engine can raise it instead of trusting it.
        output_fault = ",".join(
            name for name, res in (("heater", heater_res), ("fan", fan_res)) if not res.get("ok")
        )
        if output_fault != self._output_fault:
            if output_fault:
                logger.error(
                    "Control outputs could not be driven: %s (%s) — check the GPIO lines / wiring",
                    output_fault, heater_res.get("error") or fan_res.get("error") or "unknown",
                )
            else:
                logger.info("Control outputs are driven again")
            self._output_fault = output_fault

        # Egg turning on schedule.
        turned = False
        if turner_enabled and turn_due(now_ts, self._last_turn_ts, turn_interval):
            self._gpio.move_turner(TURN_STEPS, 1)
            self._last_turn_ts = now_ts
            turned = True
            self._persist()

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
            # A sensor that failed integrity checks is NOT online, however
            # cheerfully it reported success — the UI/alerts must see the truth.
            "online": bool(reading.get("ok")) and not sensor_fault,
            "sensor_fault": sensor_fault,
            "mock": is_mock,
            "settings_ok": not self._settings_failing,
            "output_fault": self._output_fault,
        }
        self._publish_state(state)
        return state

    # -- command intake (web app → daemon) --------------------------------
    def _drain_commands(self) -> None:
        """Consume the web app's JSONL command queue.

        The file is claimed with an atomic rename before it is read, so a line
        the web app appends while we are reading lands in a fresh file (its
        O_APPEND open creates one) instead of being wiped by a truncate. A
        leftover ``.processing`` file from a crash mid-drain is replayed first.
        """
        path = self._command_path
        if not path:
            return
        processing = path + ".processing"
        if not os.path.exists(processing):
            if not os.path.exists(path):
                return
            try:
                os.replace(path, processing)
            except OSError as exc:
                logger.debug("command drain claim error: %s", exc)
                return
        try:
            with open(processing, "r") as fh:
                lines = fh.readlines()
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
        try:
            os.unlink(processing)
        except OSError as exc:
            logger.debug("command drain cleanup error: %s", exc)

    def _apply_command(self, action: str, value: Any) -> None:
        if action == "heater":
            on = bool(value)
            self._gpio.set_heater(on)
            if on != self._heater_on:
                # A manual ON must get the same anti-runaway baseline as a
                # thermostat ON, or a misplaced sensor could heat forever.
                if on:
                    self._heat_start_ts, self._heat_start_temp = time.time(), self._last_temp
                else:
                    self._heat_start_ts = self._heat_start_temp = None
            self._heater_on = on
        elif action == "fan":
            on = bool(value)
            self._gpio.set_fan(on)
            self._fan_on = on
        elif action == "turn":
            from ..services.gpio_service import clamp_turner_steps

            try:
                raw = int(value) if value not in (None, "") else TURN_STEPS
            except (TypeError, ValueError):
                raw = TURN_STEPS
            direction = -1 if raw < 0 else 1
            steps = clamp_turner_steps(raw, default=TURN_STEPS)
            self._gpio.move_turner(steps, direction)
            # Only a full turn resets the schedule; a one-step nudge from the
            # Controls page must not postpone the next real turn by hours.
            if steps >= TURN_STEPS:
                self._last_turn_ts = time.time()
                self._persist()
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
        from ..sd_notify import sd_ready, sd_watchdog_ping

        logger.info("Control daemon starting (interval=%ss)", self._interval)
        self.apply_failsafe()
        sd_ready()
        while not self._stop.is_set():
            try:
                self.loop_once()
            except Exception as exc:  # noqa: BLE001 — a bad iteration must not kill control
                logger.warning("control iteration error: %s", exc)
            # Heartbeat AFTER the iteration: a wedged loop stops pinging and
            # systemd (WatchdogSec) restarts the daemon — the layer between
            # Restart=always (exits only) and the hardware watchdog (kernel).
            sd_watchdog_ping()
            self._stop.wait(self._interval)
        # On a clean stop, leave outputs in the defined safe state.
        self.apply_failsafe()
        logger.info("Control daemon stopped")

    def stop(self) -> None:
        self._stop.set()


# ──────────────────────────────────────────────────────────────────────────
# Settings access without the ORM (keeps SQLAlchemy out of this process)
# ──────────────────────────────────────────────────────────────────────────

def sqlite_path_from_url(db_url: str) -> str | None:
    """``sqlite:///./database/incubator.db`` -> ``./database/incubator.db``; None for other engines."""
    url = (db_url or "").strip()
    if not url.startswith("sqlite"):
        return None
    path = url.split("///", 1)[-1] if "///" in url else ""
    if not path or path == ":memory:":
        return None
    return path.split("?", 1)[0]


def read_app_settings_sqlite(path: str, timeout: float = 5.0) -> dict[str, str]:
    """Read the ``app_settings`` key/value table with the stdlib sqlite3 module.

    A missing file or table means "no settings yet" and returns {} (the loop
    uses its built-in defaults). Lock/corruption errors propagate so the
    daemon can log them and fall back to its last good snapshot.
    """
    import sqlite3

    if not os.path.exists(path):
        return {}
    uri = "file:" + os.path.abspath(path) + "?mode=ro"
    conn = sqlite3.connect(uri, timeout=timeout, uri=True)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        try:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return {}
            raise
    finally:
        conn.close()
    return {str(k): ("" if v is None else str(v)) for k, v in rows}


def _default_settings_provider() -> dict:
    from ..config import settings

    path = sqlite_path_from_url(settings.db_url)
    if path is not None:
        return read_app_settings_sqlite(path)
    # Non-SQLite deployments (none shipped) fall back to the ORM.
    from ..database import get_db
    from ..settings_store import get_settings

    db = next(get_db())
    try:
        return get_settings(db)
    finally:
        db.close()


def build_daemon() -> ControlDaemon:
    from ..config import settings
    from ..services.gpio_service import DAEMON_OUTPUTS, GPIOService

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
        backend=settings.gpio_backend,
        chip=settings.gpio_chip,
        sensor="auto",
        owned_outputs=DAEMON_OUTPUTS,
        request_button=False,   # the web app's SetupButtonService owns the button line
        sensor_backend=settings.sensor_backend,
        sensor_i2c_bus=settings.sensor_i2c_bus,
        sensor_i2c_addr=settings.sensor_i2c_addr,
        sensor_iio_device=settings.sensor_iio_device,
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
        persist_path=settings.control_persist_path,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import signal

    from ..config import settings

    # The unit can be enabled on every image; the flag is the real switch.
    # When disabled, idle quietly (don't touch GPIO) so it can't contend with
    # the web app, and don't exit (Restart=always would thrash).
    if not settings.control_daemon_enabled:
        from ..sd_notify import sd_ready, sd_watchdog_ping

        logger.info("Control daemon disabled (CONTROL_DAEMON_ENABLED=false) — idling.")
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        sd_ready()
        # Keep petting the service watchdog while intentionally idle, or
        # systemd would endlessly restart a daemon that is doing its job.
        while not stop.wait(15):
            sd_watchdog_ping()
        return

    daemon = build_daemon()
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop())
    signal.signal(signal.SIGINT, lambda *_: daemon.stop())
    daemon.run()


if __name__ == "__main__":
    main()
