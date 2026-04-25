"""
Direct GPIO control for Raspberry Pi Zero 2W.

Pin assignments use BCM numbering and are configured via config.py / env vars.
All GPIO calls are wrapped so that when GPIO_MOCK=true (dev machines without
RPi hardware) they fall back to simulated responses instead of crashing.

Wiring reference (BCM numbers, see config.py for overrides):
  GPIO4  → DHT22 data
  GPIO12 → Lock relay (active-LOW)
  GPIO13 → Door relay (active-LOW)
  GPIO17 → Heater relay (active-LOW)
  GPIO22 → Turner motor step
  GPIO23 → Turner motor direction
  GPIO24 → Candle / candling LED
  GPIO25 → Alarm / buzzer
  GPIO27 → Fan relay (active-LOW)
  GPIO18 → Setup button (pulled up, pressed = LOW)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as _GPIO  # type: ignore

    _GPIO.setmode(_GPIO.BCM)
    _GPIO.setwarnings(False)
    _HAS_GPIO = True
except Exception:
    _HAS_GPIO = False
    logger.info("RPi.GPIO not available — running in GPIO mock mode")

try:
    import adafruit_dht  # type: ignore
    import board  # type: ignore

    _HAS_DHT = True
except Exception:
    _HAS_DHT = False
    logger.info("adafruit_dht not available — DHT readings will be simulated")


class GPIOService:
    """Low-level Pi GPIO driver.  One instance is shared application-wide."""

    def __init__(
        self,
        dht_pin: int,
        heater_pin: int,
        fan_pin: int,
        turner_pin: int,
        turner_dir_pin: int,
        candle_pin: int,
        alarm_pin: int,
        lock_pin: int,
        door_pin: int,
        setup_button_pin: int,
        relay_active_low: bool = True,
        mock: bool = False,
    ) -> None:
        self.dht_pin = dht_pin
        self.heater_pin = heater_pin
        self.fan_pin = fan_pin
        self.turner_pin = turner_pin
        self.turner_dir_pin = turner_dir_pin
        self.candle_pin = candle_pin
        self.alarm_pin = alarm_pin
        self.lock_pin = lock_pin
        self.door_pin = door_pin
        self.setup_button_pin = setup_button_pin
        self.relay_active_low = relay_active_low
        self._mock = mock or not _HAS_GPIO
        self._lock = threading.Lock()
        self._mock_state: dict[str, Any] = {
            "heater": False,
            "fan": False,
            "turner": False,
            "candle": False,
            "alarm": False,
            "lock": False,
            "door": False,
            "temp_c": 37.4,
            "humidity_pct": 55.0,
        }
        self._dht_device: Any = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        if self._initialized:
            return
        if self._mock:
            logger.info("GPIOService: mock mode active")
            self._initialized = True
            return

        output_pins = [
            self.heater_pin,
            self.fan_pin,
            self.turner_pin,
            self.turner_dir_pin,
            self.candle_pin,
            self.alarm_pin,
            self.lock_pin,
            self.door_pin,
        ]
        for pin in output_pins:
            _GPIO.setup(pin, _GPIO.OUT, initial=_GPIO.HIGH if self.relay_active_low else _GPIO.LOW)

        _GPIO.setup(self.setup_button_pin, _GPIO.IN, pull_up_down=_GPIO.PUD_UP)

        if _HAS_DHT:
            try:
                board_pin = getattr(board, f"D{self.dht_pin}", None)
                if board_pin:
                    self._dht_device = adafruit_dht.DHT22(board_pin, use_pulseio=False)
                    logger.info("DHT22 initialised on GPIO%d", self.dht_pin)
            except Exception as exc:
                logger.warning("DHT22 init failed, will use mock readings: %s", exc)

        self._initialized = True
        logger.info("GPIOService initialised (BCM mode)")

    def cleanup(self) -> None:
        if not self._mock and _HAS_GPIO and self._initialized:
            try:
                _GPIO.cleanup()
            except Exception:
                pass
        if self._dht_device:
            try:
                self._dht_device.exit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _relay_on(self, pin: int) -> None:
        if self._mock:
            return
        _GPIO.output(pin, _GPIO.LOW if self.relay_active_low else _GPIO.HIGH)

    def _relay_off(self, pin: int) -> None:
        if self._mock:
            return
        _GPIO.output(pin, _GPIO.HIGH if self.relay_active_low else _GPIO.LOW)

    def _digital_write(self, pin: int, high: bool) -> None:
        if self._mock:
            return
        _GPIO.output(pin, _GPIO.HIGH if high else _GPIO.LOW)

    def _digital_read(self, pin: int) -> bool:
        if self._mock:
            return False
        return _GPIO.input(pin) == _GPIO.HIGH

    # ------------------------------------------------------------------
    # Sensor reads
    # ------------------------------------------------------------------

    def read_temperature_humidity(self) -> dict[str, Any]:
        """Read DHT22.  Returns dict with temperature_c, humidity_pct, ok."""
        with self._lock:
            if self._mock or not self._dht_device:
                return {
                    "ok": True,
                    "mock": True,
                    "temperature_c": self._mock_state["temp_c"],
                    "humidity_pct": self._mock_state["humidity_pct"],
                }
            for attempt in range(3):
                try:
                    temp = self._dht_device.temperature
                    hum = self._dht_device.humidity
                    if temp is not None and hum is not None:
                        return {"ok": True, "mock": False, "temperature_c": round(temp, 2), "humidity_pct": round(hum, 1)}
                except Exception as exc:
                    if attempt == 2:
                        logger.warning("DHT22 read failed after 3 attempts: %s", exc)
                    time.sleep(0.5)
            return {"ok": False, "error": "DHT22 read failed", "temperature_c": None, "humidity_pct": None}

    def read_button(self) -> bool:
        """Return True if setup button is currently pressed (active LOW)."""
        if self._mock:
            return False
        return not self._digital_read(self.setup_button_pin)

    # ------------------------------------------------------------------
    # Output control
    # ------------------------------------------------------------------

    def set_heater(self, on: bool) -> dict[str, Any]:
        with self._lock:
            if on:
                self._relay_on(self.heater_pin)
            else:
                self._relay_off(self.heater_pin)
            self._mock_state["heater"] = on
            return {"ok": True, "heater": on}

    def set_fan(self, on: bool) -> dict[str, Any]:
        with self._lock:
            if on:
                self._relay_on(self.fan_pin)
            else:
                self._relay_off(self.fan_pin)
            self._mock_state["fan"] = on
            return {"ok": True, "fan": on}

    def set_candle(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._digital_write(self.candle_pin, on)
            self._mock_state["candle"] = on
            return {"ok": True, "candle": on}

    def set_alarm(self, on: bool) -> dict[str, Any]:
        with self._lock:
            self._digital_write(self.alarm_pin, on)
            self._mock_state["alarm"] = on
            return {"ok": True, "alarm": on}

    def set_lock(self, locked: bool) -> dict[str, Any]:
        """Energise relay to LOCK, de-energise to UNLOCK (fail-safe open)."""
        with self._lock:
            if locked:
                self._relay_on(self.lock_pin)
            else:
                self._relay_off(self.lock_pin)
            self._mock_state["lock"] = locked
            return {"ok": True, "locked": locked}

    def set_door(self, open_: bool) -> dict[str, Any]:
        with self._lock:
            if open_:
                self._relay_on(self.door_pin)
            else:
                self._relay_off(self.door_pin)
            self._mock_state["door"] = open_
            return {"ok": True, "door_open": open_}

    def move_turner(self, steps: int = 200, direction: int = 1) -> dict[str, Any]:
        """Step the egg turner motor.  direction=1 forward, direction=-1 reverse."""
        with self._lock:
            self._mock_state["turner"] = True
            if not self._mock:
                _GPIO.output(self.turner_dir_pin, _GPIO.HIGH if direction > 0 else _GPIO.LOW)
                for _ in range(max(0, steps)):
                    _GPIO.output(self.turner_pin, _GPIO.HIGH)
                    time.sleep(0.001)
                    _GPIO.output(self.turner_pin, _GPIO.LOW)
                    time.sleep(0.001)
            self._mock_state["turner"] = False
            return {"ok": True, "steps": steps, "direction": direction}

    def get_state(self) -> dict[str, Any]:
        """Return a snapshot of all output states."""
        return dict(self._mock_state)
