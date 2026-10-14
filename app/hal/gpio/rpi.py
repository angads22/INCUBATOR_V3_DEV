"""Legacy RPi.GPIO backend (BCM numbering). Raspberry Pi only."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from .. import _util
from .base import GpioBackend, InputLine, OutputLine, PinSpec, Pull

logger = logging.getLogger(__name__)

_GPIO_NAME = re.compile(r"^GPIO(\d+)$", re.IGNORECASE)


def is_available() -> bool:
    return _util.optional_import("RPi.GPIO") is not None


def bcm_number(pin: PinSpec) -> int:
    """RPi.GPIO only understands BCM numbers; line names must be GPIO<n>."""
    if pin.offset is not None:
        if pin.chip:
            logger.debug("RPi.GPIO ignores chip %r for pin %s", pin.chip, pin)
        return pin.offset
    m = _GPIO_NAME.match(pin.name or "")
    if not m:
        raise ValueError(f"RPi.GPIO backend needs a BCM number or GPIO<n> name, got {pin.name!r}")
    return int(m.group(1))


class _RpiOutput(OutputLine):
    def __init__(self, backend: "RpiGpioBackend", bcm: int) -> None:
        self._backend = backend
        self._bcm = bcm
        self._released = False

    def set(self, value: bool) -> None:
        g = self._backend._GPIO
        g.output(self._bcm, g.HIGH if value else g.LOW)

    def get(self) -> bool:
        g = self._backend._GPIO
        return g.input(self._bcm) == g.HIGH

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._backend._release(self._bcm, self)


class _RpiInput(InputLine):
    def __init__(self, backend: "RpiGpioBackend", bcm: int) -> None:
        self._backend = backend
        self._bcm = bcm
        self._released = False

    def get(self) -> bool:
        g = self._backend._GPIO
        return g.input(self._bcm) == g.HIGH

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._backend._release(self._bcm, self)


class RpiGpioBackend(GpioBackend):
    name = "rpi"

    def __init__(self, chip: str = "") -> None:
        gpio = _util.optional_import("RPi.GPIO")
        if gpio is None:
            raise RuntimeError("RPi.GPIO not importable")
        self._GPIO: Any = gpio
        self._GPIO.setmode(self._GPIO.BCM)
        self._GPIO.setwarnings(False)
        self._lines: dict[int, Any] = {}
        self._lock = threading.Lock()

    def request_output(self, pin: PinSpec, initial: bool, consumer: str = "incubator") -> OutputLine:
        bcm = bcm_number(pin)
        g = self._GPIO
        g.setup(bcm, g.OUT, initial=g.HIGH if initial else g.LOW)
        line = _RpiOutput(self, bcm)
        with self._lock:
            self._lines[bcm] = line
        return line

    def request_input(self, pin: PinSpec, pull: Pull = "up", consumer: str = "incubator") -> InputLine:
        bcm = bcm_number(pin)
        g = self._GPIO
        pud = {"up": g.PUD_UP, "down": g.PUD_DOWN}.get(pull, g.PUD_OFF)
        g.setup(bcm, g.IN, pull_up_down=pud)
        line = _RpiInput(self, bcm)
        with self._lock:
            self._lines[bcm] = line
        return line

    def _release(self, bcm: int, line: Any) -> None:
        with self._lock:
            if self._lines.get(bcm) is line:
                del self._lines[bcm]
        try:
            self._GPIO.cleanup(bcm)
        except Exception as exc:
            logger.debug("RPi.GPIO cleanup(%s) failed: %s", bcm, exc)

    def close(self) -> None:
        with self._lock:
            lines = list(self._lines.values())
        for line in lines:
            line.release()
