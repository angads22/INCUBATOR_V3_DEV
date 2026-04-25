from __future__ import annotations

from typing import Any

from .camera_service import CameraService
from .gpio_service import GPIOService


class HardwareService:
    """High-level hardware commands that map to GPIO actions on the Pi Zero 2W.

    All methods return a dict with at least {"ok": bool}.
    """

    def __init__(self, gpio: GPIOService, camera: CameraService) -> None:
        self.gpio = gpio
        self.camera = camera

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

    def read_temp(self) -> dict[str, Any]:
        result = self.gpio.read_temperature_humidity()
        if not result.get("ok"):
            return result
        return {"ok": True, "temperature_c": result["temperature_c"], "mock": result.get("mock", False)}

    def read_humidity(self) -> dict[str, Any]:
        result = self.gpio.read_temperature_humidity()
        if not result.get("ok"):
            return result
        return {"ok": True, "humidity_pct": result["humidity_pct"], "mock": result.get("mock", False)}

    def read_environment(self) -> dict[str, Any]:
        """Read both temperature and humidity in one DHT22 call."""
        return self.gpio.read_temperature_humidity()

    # ------------------------------------------------------------------
    # Candling / light
    # ------------------------------------------------------------------

    def set_candle(self, on: bool) -> dict[str, Any]:
        return self.gpio.set_candle(on)

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------

    def capture_image(self) -> dict[str, Any]:
        return self.camera.capture()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        return self.gpio.get_state()
