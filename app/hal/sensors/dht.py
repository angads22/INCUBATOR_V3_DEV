"""Legacy adafruit_dht driver (bit-banged DHT22 on a Raspberry Pi).

Prefer the kernel ``dht11`` overlay + IIO backend when you can; this one needs
``adafruit-circuitpython-dht`` + Blinka and only maps BCM-numbered pins.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import _util
from .base import Sensor, reading

logger = logging.getLogger(__name__)

_GPIO_NAME = re.compile(r"^GPIO(\d+)$", re.IGNORECASE)


def is_available() -> bool:
    return _util.optional_import("adafruit_dht") is not None and _util.optional_import("board") is not None


def bcm_from_pin(pin: Any) -> int:
    """Accept 4, "4", "GPIO4", "gpiochip0:4"; adafruit only knows BCM numbers."""
    offset = getattr(pin, "offset", None)
    if offset is not None:
        return int(offset)
    name = getattr(pin, "name", None)
    text = str(name if name else pin).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    if text.isdigit():
        return int(text)
    m = _GPIO_NAME.match(text)
    if m:
        return int(m.group(1))
    raise ValueError(f"DHT backend needs a BCM number, got {pin!r}")


class DhtSensor(Sensor):
    def __init__(self, pin: Any, model: str = "dht22") -> None:
        adafruit_dht = _util.optional_import("adafruit_dht")
        board = _util.optional_import("board")
        if adafruit_dht is None or board is None:
            raise RuntimeError("adafruit_dht / board not importable (pip install -e '.[pi]')")
        bcm = bcm_from_pin(pin)
        board_pin = getattr(board, f"D{bcm}", None)
        if board_pin is None:
            raise RuntimeError(f"board has no pin D{bcm}")
        cls = adafruit_dht.DHT11 if model.lower() == "dht11" else adafruit_dht.DHT22
        self._device = cls(board_pin, use_pulseio=False)
        self.name = model.lower()
        self.pin = bcm

    def read(self) -> dict[str, Any]:
        try:
            temp = self._device.temperature
            hum = self._device.humidity
        except RuntimeError as exc:
            # adafruit_dht raises RuntimeError for checksum/timing errors — common
            # and harmless; the caller keeps the last good reading.
            return reading(self.name, ok=False, error=str(exc))
        except Exception as exc:
            return reading(self.name, ok=False, error=f"{type(exc).__name__}: {exc}")
        if temp is None or hum is None:
            return reading(self.name, ok=False, error="no reading")
        return reading(self.name, ok=True, temperature_c=float(temp), humidity_pct=float(hum))

    def close(self) -> None:
        exit_fn = getattr(self._device, "exit", None)
        if callable(exit_fn):
            try:
                exit_fn()
            except Exception:
                pass
