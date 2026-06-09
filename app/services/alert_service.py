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

ALERT_MESSAGES = {
    "temp_low": "Temperature is below the target range",
    "temp_high": "Temperature is above the target range",
    "humidity_low": "Humidity is below the target range",
    "humidity_high": "Humidity is above the target range",
    "sensor_offline": "Sensor is not responding — live readings are unavailable",
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
        self._active: dict[str, str] = {}  # alert type -> ISO timestamp raised
        self._silenced = False
        self._alarm_on = False

    def reset(self) -> None:
        """Drop all cached state (used by tests and re-initialisation)."""
        with self._lock:
            self._last_reading = None
            self._online = False
            self._fail_count = 0
            self._range_counts = {}
            self._active = {}
            self._silenced = False
            self._alarm_on = False

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

    def alert_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": [
                    {"type": alert_type, "message": ALERT_MESSAGES[alert_type], "since": since}
                    for alert_type, since in self._active.items()
                ],
                "alarm_on": self._alarm_on,
                "silenced": self._silenced,
            }

    def silence(self) -> dict[str, Any]:
        """Stop the buzzer until every active alert has cleared."""
        with self._lock:
            self._silenced = True
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
            self._silenced = False
        should_alarm = bool(self._active) and alarm_enabled and not self._silenced
        if should_alarm != self._alarm_on:
            self._gpio.set_alarm(should_alarm)
            self._alarm_on = should_alarm
