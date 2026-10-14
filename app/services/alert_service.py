"""
Alert engine and cached sensor state.

The sensor poller thread is the single DHT22 reader: every reading (good or
failed) flows through ``record_reading``.  This service caches the latest
values for the dashboard and ``/api/sensors/latest`` — the DHT22 must never
be read on the request path (a failed read blocks ~3s holding the GPIO lock)
— and raises/clears alerts, driving the buzzer.

Thread-safety: the poller writes, request handlers read/silence; all shared
state sits behind one lock.  Persisting alert events is left to the caller
(the poller owns its own DB session).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gpio_service import GPIOService

# Consecutive out-of-range polls before a range alert is raised, so a reading
# hovering at the boundary does not make the buzzer flap.
RANGE_DEBOUNCE_POLLS = 2

# Consecutive mismatched polls before a heater-circuit fault is raised. Higher
# than the range debounce because relays switch between polls and inrush settles
# — we only want to alarm on a persistent commanded-vs-measured disagreement.
HEATER_DEBOUNCE_POLLS = 3

ALERT_MESSAGES = {
    "temp_low": "Temperature is below the target range",
    "temp_high": "Temperature is above the target range",
    "humidity_low": "Humidity is below the target range",
    "humidity_high": "Humidity is above the target range",
    "sensor_offline": "Sensor is not responding — live readings are unavailable",
    "sensor_faulty": "Sensor readings are not trustworthy (frozen or impossible values) — heating has been stopped as a precaution",
    "heater_cutoff": "Heater is ON but no current is flowing — the thermal cutoff may have tripped or the heater is disconnected",
    "heater_stuck": "Current is flowing to the heater while it is commanded OFF — the relay may be stuck closed",
    "output_fault": "The control loop cannot drive a relay line (heater/fan GPIO unavailable) — check the wiring and the control service log",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_float(app_settings: dict[str, str], key: str, default: float) -> float:
    try:
        return float(app_settings.get(key, default))
    except (TypeError, ValueError):
        return default


class AlertService:
    """Evaluates sensor readings against targets and owns the alarm state."""

    def __init__(self, gpio: "GPIOService") -> None:
        self._gpio = gpio
        self._lock = threading.Lock()
        self._last_reading: dict[str, Any] | None = None
        self._online = False
        self._fail_count = 0
        self._range_counts: dict[str, int] = {}
        self._heater_counts: dict[str, int] = {}
        self._active: dict[str, str] = {}  # alert type -> ISO timestamp raised
        # Alert TYPES the operator silenced. Silence applies only to the alerts
        # that were active when the button was pressed: a new, different alert
        # (humidity_low muted, then temp_high appears) sounds the buzzer again.
        self._silenced: set[str] = set()
        self._alarm_on = False
        # Sensor-integrity tracking (latch detection across polls).
        self._integ_last: tuple[float | None, float | None] = (None, None)
        self._integ_unchanged_since: float | None = None

    def reset(self) -> None:
        """Drop all cached state (used by tests and re-initialisation)."""
        with self._lock:
            self._last_reading = None
            self._online = False
            self._fail_count = 0
            self._range_counts = {}
            self._heater_counts = {}
            self._active = {}
            self._silenced = set()
            self._alarm_on = False
            self._integ_last = (None, None)
            self._integ_unchanged_since = None

    # ------------------------------------------------------------------
    # Sensor integrity
    # ------------------------------------------------------------------

    def _integrity_fault(self, reading: dict[str, Any]) -> str:
        """Name the integrity problem with an ok=True reading, else "".

        Shares the control daemon's thresholds so the alarm and the thermostat
        can never disagree about whether the sensor is trustworthy.
        """
        import time as _time

        from ..control.loop import reading_plausible, sensor_stuck

        # Mock readings are exempt — the mock sensor returns a fixed value by
        # design, so checking it would alarm on every dev/demo/sensor-less unit.
        if not reading.get("ok") or reading.get("mock"):
            self._integ_unchanged_since = None
            return ""
        temp = reading.get("temperature_c")
        hum = reading.get("humidity_pct")
        if temp is not None and not reading_plausible(temp):
            return "implausible"
        last_t, last_h = self._integ_last
        stuck, self._integ_unchanged_since = sensor_stuck(
            temp, hum, last_t, last_h, self._integ_unchanged_since, _time.time(),
        )
        self._integ_last = (temp, hum)
        return "stuck" if stuck else ""

    # ------------------------------------------------------------------
    # Poller entry point
    # ------------------------------------------------------------------

    def record_reading(self, reading: dict[str, Any], app_settings: dict[str, str]) -> list[dict[str, str]]:
        """Feed one poll result through the engine.

        Returns transition events (alert raised/cleared) for the caller to
        persist; thresholds are re-read from ``app_settings`` every poll so
        settings changes apply without a restart.
        """
        events: list[dict[str, str]] = []
        with self._lock:
            # The control daemon reports outputs it could not drive (a relay
            # line that failed to request). That is an alarm in its own right:
            # the thermostat may be "on" while nothing is heating.
            if str(reading.get("output_fault") or ""):
                events += self._raise("output_fault")
            else:
                events += self._clear("output_fault")
            # A reading can claim ok=True and still be untrustworthy: a DHT22
            # that latches returns the same value forever, and a glitched read
            # can be physically impossible. The control daemon already refuses
            # to act on these; the farmer needs to HEAR about it too, so an
            # integrity failure raises its own alarm rather than being silently
            # averaged into a healthy-looking history.
            # The control daemon already decided a reading is untrustworthy
            # (latched / implausible / heat runaway) and reports why; show the
            # farmer THAT message, not a generic "sensor not responding".
            integrity_fault = str(reading.get("sensor_fault") or "") or self._integrity_fault(reading)
            if integrity_fault:
                self._range_counts.clear()
                events += self._raise("sensor_faulty")
                self._apply_alarm(app_settings)
                return events
            events += self._clear("sensor_faulty")

            if reading.get("ok"):
                self._online = True
                self._fail_count = 0
                self._last_reading = {
                    "temperature_c": reading.get("temperature_c"),
                    "humidity_pct": reading.get("humidity_pct"),
                    "mock": bool(reading.get("mock")),
                    "read_at": _utcnow_iso(),
                }
                events += self._clear("sensor_offline")
                events += self._evaluate_ranges(app_settings)
            else:
                self._online = False
                self._fail_count += 1
                self._range_counts.clear()
                fail_limit = max(1, int(_get_float(app_settings, "alert_sensor_fail_count", 3)))
                if self._fail_count >= fail_limit:
                    events += self._raise("sensor_offline")
            self._apply_alarm(app_settings)
        return events

    def evaluate_heater(
        self,
        commanded_on: bool,
        present: bool,
        amps: float | None,
        app_settings: dict[str, str],
    ) -> list[dict[str, str]]:
        """Cross-check commanded heater state against measured current.

        Requires an INA219 current sensor on the heater leg (present=True). Two
        persistent faults, each latched into the alarm after HEATER_DEBOUNCE_POLLS:
          * ``heater_cutoff`` — commanded ON but ~0 A: the independent thermal
            cutoff tripped, or the element/relay opened. Heating has stopped.
          * ``heater_stuck``  — current flowing while commanded OFF: the relay
            welded closed / SSR shorted. Heating cannot be turned off in software.
        Both drive the buzzer via the existing alarm path. When no sensor is
        fitted the check is a no-op (and any prior heater fault is cleared).
        """
        events: list[dict[str, str]] = []
        with self._lock:
            if not present or amps is None:
                events += self._clear("heater_cutoff")
                events += self._clear("heater_stuck")
                self._heater_counts.clear()
                self._apply_alarm(app_settings)
                return events
            threshold = _get_float(app_settings, "alert_heater_current_min_a", 0.05)
            events += self._debounced_heater("heater_cutoff", commanded_on and amps < threshold)
            events += self._debounced_heater("heater_stuck", (not commanded_on) and amps >= threshold)
            self._apply_alarm(app_settings)
        return events

    def _debounced_heater(self, alert_type: str, faulted: bool) -> list[dict[str, str]]:
        """Raise after HEATER_DEBOUNCE_POLLS consecutive faults; clear otherwise.
        Call with self._lock held."""
        if faulted:
            self._heater_counts[alert_type] = self._heater_counts.get(alert_type, 0) + 1
            if self._heater_counts[alert_type] >= HEATER_DEBOUNCE_POLLS:
                return self._raise(alert_type)
            return []
        self._heater_counts[alert_type] = 0
        return self._clear(alert_type)

    # ------------------------------------------------------------------
    # State for the UI
    # ------------------------------------------------------------------

    def sensor_snapshot(self) -> dict[str, Any]:
        """Latest cached reading plus online flag for SSR and the sensors API."""
        with self._lock:
            snapshot: dict[str, Any] = {
                "online": self._online,
                "has_reading": self._last_reading is not None,
                "temperature_c": None,
                "humidity_pct": None,
                "read_at": None,
                "mock": False,
            }
            if self._last_reading:
                snapshot.update(self._last_reading)
            return snapshot

    def _all_silenced(self) -> bool:
        """True when every active alert has been silenced (call with lock held)."""
        return bool(self._active) and all(t in self._silenced for t in self._active)

    def alert_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": [
                    {
                        "type": alert_type,
                        "message": ALERT_MESSAGES[alert_type],
                        "since": since,
                        "silenced": alert_type in self._silenced,
                    }
                    for alert_type, since in self._active.items()
                ],
                "alarm_on": self._alarm_on,
                # Buzzer suppressed for everything currently active.
                "silenced": self._all_silenced(),
                "silenced_types": sorted(t for t in self._silenced if t in self._active),
            }

    def silence(self) -> dict[str, Any]:
        """Stop the buzzer for the alerts active right now.

        Each silenced alert stays muted until it clears; an alert of a type that
        is not active yet will still sound the buzzer when it is raised.
        """
        with self._lock:
            self._silenced |= set(self._active)
            if self._alarm_on:
                self._gpio.set_alarm(False)
                self._alarm_on = False
        return self.alert_state()

    def test_alarm(self) -> dict[str, Any]:
        return self._gpio.pulse_alarm(1.0)

    # ------------------------------------------------------------------
    # Internals (call with self._lock held)
    # ------------------------------------------------------------------

    def _evaluate_ranges(self, app_settings: dict[str, str]) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        reading = self._last_reading or {}
        temp = reading.get("temperature_c")
        hum = reading.get("humidity_pct")
        target_t = _get_float(app_settings, "target_temp_c", 37.5)
        tol_t = _get_float(app_settings, "alert_temp_tolerance_c", 1.0)
        target_h = _get_float(app_settings, "target_humidity_pct", 55.0)
        tol_h = _get_float(app_settings, "alert_humidity_tolerance_pct", 10.0)

        checks = {
            "temp_low": temp is not None and temp < target_t - tol_t,
            "temp_high": temp is not None and temp > target_t + tol_t,
            "humidity_low": hum is not None and hum < target_h - tol_h,
            "humidity_high": hum is not None and hum > target_h + tol_h,
        }
        for alert_type, out_of_range in checks.items():
            if out_of_range:
                self._range_counts[alert_type] = self._range_counts.get(alert_type, 0) + 1
                if self._range_counts[alert_type] >= RANGE_DEBOUNCE_POLLS:
                    events += self._raise(alert_type)
            else:
                self._range_counts[alert_type] = 0
                events += self._clear(alert_type)
        return events

    def _raise(self, alert_type: str) -> list[dict[str, str]]:
        if alert_type in self._active:
            return []
        self._active[alert_type] = _utcnow_iso()
        return [{"type": alert_type, "kind": "raised", "message": ALERT_MESSAGES[alert_type]}]

    def _clear(self, alert_type: str) -> list[dict[str, str]]:
        if alert_type not in self._active:
            return []
        del self._active[alert_type]
        # A cleared alert is no longer muted: if it comes back it must be heard.
        self._silenced.discard(alert_type)
        return [
            {
                "type": f"{alert_type}_cleared",
                "kind": "cleared",
                "message": f"{ALERT_MESSAGES[alert_type]} — cleared",
            }
        ]

    def _apply_alarm(self, app_settings: dict[str, str]) -> None:
        alarm_enabled = app_settings.get("alarm_enabled", "true").lower() == "true"
        if not self._active:
            self._silenced.clear()
        # Sound for any active alert the operator has NOT silenced — so a new
        # alert type re-sounds the buzzer even while an older one is muted.
        unmuted = [t for t in self._active if t not in self._silenced]
        should_alarm = bool(unmuted) and alarm_enabled
        if should_alarm != self._alarm_on:
            self._gpio.set_alarm(should_alarm)
            self._alarm_on = should_alarm
