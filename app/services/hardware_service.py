from __future__ import annotations

from typing import Any

from .camera_service import CameraService
from .gpio_service import GPIOService


class HardwareService:
    """High-level hardware commands that map to GPIO actions.

    All methods return a dict with at least {"ok": bool}.

    When the control daemon owns the climate lines (``daemon_state_path`` set),
    every sensor read comes from the daemon's published state file instead of
    the sensor itself, and ``get_state()`` reports the daemon's heater/fan
    state — the web process never touches those lines in that mode.
    """

    def __init__(self, gpio: GPIOService, camera: CameraService, *, daemon_state_path: str | None = None) -> None:
        self.gpio = gpio
        self.camera = camera
        self.daemon_state_path = daemon_state_path or None

    @property
    def daemon_mode(self) -> bool:
        return self.daemon_state_path is not None

    def _daemon_reading(self) -> dict[str, Any]:
        from ..control.bridge import daemon_reading

        return daemon_reading(self.daemon_state_path)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Enclosure
    # ------------------------------------------------------------------

    def open_lock(self) -> dict[str, Any]:
        return self.gpio.set_lock(locked=False)

    def close_lock(self) -> dict[str, Any]:
        return self.gpio.set_lock(locked=True)

    def open_door(self) -> dict[str, Any]:
        return self.gpio.set_door(open_=True)

    def close_door(self) -> dict[str, Any]:
        return self.gpio.set_door(open_=False)

    # ------------------------------------------------------------------
    # Climate
    # ------------------------------------------------------------------

    def set_heater(self, on: bool) -> dict[str, Any]:
        return self.gpio.set_heater(on)

    def set_fan(self, on: bool) -> dict[str, Any]:
        return self.gpio.set_fan(on)

    # ------------------------------------------------------------------
    # Turner
    # ------------------------------------------------------------------

    def move_motor(self, value: int | str) -> dict[str, Any]:
        """value encodes turn direction/steps.  Positive = forward, negative = reverse."""
        try:
            steps = int(value)
        except (TypeError, ValueError):
            steps = 200
        direction = -1 if steps < 0 else 1
        return self.gpio.move_turner(abs(steps) or 200, direction)

    # ------------------------------------------------------------------
    # Sensors
    # ------------------------------------------------------------------

    def read_environment(self) -> dict[str, Any]:
        """Read temperature + humidity in one call (daemon state file in daemon mode)."""
        if self.daemon_mode:
            return self._daemon_reading()
        return self.gpio.read_temperature_humidity()

    def read_temp(self) -> dict[str, Any]:
        result = self.read_environment()
        if not result.get("ok"):
            return result
        return {"ok": True, "temperature_c": result["temperature_c"], "mock": result.get("mock", False)}

    def read_humidity(self) -> dict[str, Any]:
        result = self.read_environment()
        if not result.get("ok"):
            return result
        return {"ok": True, "humidity_pct": result["humidity_pct"], "mock": result.get("mock", False)}

    # ------------------------------------------------------------------
    # Candling / light
    # ------------------------------------------------------------------

    def set_candle(self, on: bool) -> dict[str, Any]:
        return self.gpio.set_candle(on)

    # ------------------------------------------------------------------
    # Alarm / buzzer
    # ------------------------------------------------------------------

    def set_alarm(self, on: bool) -> dict[str, Any]:
        return self.gpio.set_alarm(on)

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------

    def capture_image(self) -> dict[str, Any]:
        return self.camera.capture()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Output snapshot; in daemon mode heater/fan come from the daemon."""
        state = self.gpio.get_state()
        if self.daemon_mode:
            reading = self._daemon_reading()
            if reading.get("fresh"):
                state["heater"] = bool(reading.get("heater_on"))
                state["fan"] = bool(reading.get("fan_on"))
                state["source"] = "control-daemon"
            else:
                state["heater"] = state["fan"] = None
                state["source"] = "control-daemon-stale"
        return state
