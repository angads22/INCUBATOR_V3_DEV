"""Status LED: a sysfs LED (``/sys/class/leds/<name>``) or a GPIO output line.

    led = StatusLed.create("auto")            # None when the board has no LED
    led.set_pattern("heartbeat")
    led.close()                               # restores the LED's old trigger

One daemon thread plays the current pattern; ``set_pattern`` takes effect
immediately. Every I/O error is swallowed (debug log) — a dead LED must never
stop the incubator.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import _util

logger = logging.getLogger(__name__)

SYSFS_LEDS = "/sys/class/leds"

PREFERRED_LEDS: tuple[str, ...] = (
    "ACT",
    "led0",
    "status",
    "user",
    "green:status",
    "blue:status",
    "red:status",
    "orangepi:red:status",
    "orangepi:green:pwr",
)
_KEYWORDS = ("status", "user", "act")

# name -> list of (on, seconds); the sequence loops. Steps of 0 s are skipped.
PATTERNS: dict[str, list[tuple[bool, float]]] = {
    "off": [(False, 1.0)],
    "on": [(True, 1.0)],
    "heartbeat": [(True, 0.1), (False, 1.9)],
    "blink_fast": [(True, 0.15), (False, 0.15)],
    "blink_double": [(True, 0.12), (False, 0.12), (True, 0.12), (False, 1.2)],
    "sos": (
        [(True, 0.15), (False, 0.15)] * 3
        + [(True, 0.45), (False, 0.15)] * 3
        + [(True, 0.15), (False, 0.15)] * 3
        + [(False, 1.0)]
    ),
}
STEADY = {"off": False, "on": True}


# ----------------------------------------------------------------------------
# Drivers
# ----------------------------------------------------------------------------

class _SysfsLedDriver:
    kind = "sysfs"

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(path)
        self._trigger_file = os.path.join(path, "trigger")
        self._brightness_file = os.path.join(path, "brightness")
        self.max_brightness = _util.read_int(os.path.join(path, "max_brightness"), 1) or 1
        self.saved_trigger = self._current_trigger()
        if self.saved_trigger is not None and self.saved_trigger != "none":
            _util.write_text(self._trigger_file, "none")

    def _current_trigger(self) -> str | None:
        text = _util.read_text(self._trigger_file)
        if text is None:
            return None
        for token in text.split():
            if token.startswith("[") and token.endswith("]"):
                return token[1:-1]
        return text.strip() or None

    def set(self, on: bool) -> None:
        _util.write_text(self._brightness_file, str(self.max_brightness if on else 0))

    def close(self) -> None:
        self.set(False)
        if self.saved_trigger and self.saved_trigger != "none":
            _util.write_text(self._trigger_file, self.saved_trigger)

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "path": self.path, "restored_trigger": self.saved_trigger}


class _GpioLedDriver:
    kind = "gpio"

    def __init__(self, line: Any, spec: str, active_low: bool = False) -> None:
        self._line = line
        self.name = spec
        self.active_low = active_low

    def set(self, on: bool) -> None:
        try:
            self._line.set((not on) if self.active_low else on)
        except Exception as exc:
            logger.debug("gpio LED write failed: %s", exc)

    def close(self) -> None:
        self.set(False)
        try:
            self._line.release()
        except Exception as exc:
            logger.debug("gpio LED release failed: %s", exc)

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "active_low": self.active_low}


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------

def list_sysfs_leds(sysfs_root: str | None = None) -> list[str]:
    try:
        return sorted(n for n in os.listdir(sysfs_root or SYSFS_LEDS) if os.path.isdir(os.path.join(sysfs_root or SYSFS_LEDS, n)))
    except OSError:
        return []


def pick_sysfs_led(sysfs_root: str | None = None) -> str | None:
    """First existing LED from the preference list, else any status/user/act-ish name."""
    names = list_sysfs_leds(sysfs_root)
    if not names:
        return None
    lower = {n.lower(): n for n in names}
    for want in PREFERRED_LEDS:
        if want.lower() in lower:
            return lower[want.lower()]
    for n in names:
        low = n.lower()
        if any(k in low for k in _KEYWORDS):
            return n
    return None


# ----------------------------------------------------------------------------
# StatusLed
# ----------------------------------------------------------------------------

class StatusLed:
    def __init__(self, driver: Any, spec: str) -> None:
        self._driver = driver
        self.spec = spec
        self.pattern = "off"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Serialises every write to the LED so a pattern step from the worker
        # can never interleave with (or land after) a synchronous on/off.
        self._io_lock = threading.Lock()
        self._closed = False
        self._state: bool | None = None

    # -- construction ----------------------------------------------------------
    @classmethod
    def create(
        cls,
        spec: str = "auto",
        gpio_backend: Any = None,
        *,
        sysfs_root: str | None = None,
    ) -> "StatusLed | None":
        """Build a StatusLed from ``none`` / ``auto`` / ``<sysfs led>`` / ``gpio:<pinspec>``.

        Returns None (never raises) when nothing usable exists.
        """
        spec = (spec or "auto").strip()
        try:
            if spec.lower() in ("none", "off", "false", "0"):
                return None
            if spec.lower().startswith("gpio:"):
                return cls._from_gpio(spec, spec[5:], gpio_backend)
            root = sysfs_root or SYSFS_LEDS
            if spec.lower() == "auto":
                name = pick_sysfs_led(root)
                if name is None:
                    logger.info("status LED: no sysfs LED found under %s (set STATUS_LED=gpio:<pin> to use a GPIO)", root)
                    return None
            else:
                name = spec
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                logger.warning("status LED %r not found under %s", name, root)
                return None
            led = cls(_SysfsLedDriver(path), spec)
            logger.info("status LED: sysfs %s", name)
            return led
        except Exception as exc:
            logger.debug("StatusLed.create(%r) failed: %s", spec, exc)
            return None

    @classmethod
    def _from_gpio(cls, spec: str, pin_text: str, gpio_backend: Any) -> "StatusLed | None":
        from .gpio import get_gpio_backend, parse_pin

        pin_text = pin_text.strip()
        active_low = False
        if pin_text.startswith("!"):
            active_low = True
            pin_text = pin_text[1:]
        pin = parse_pin(pin_text)
        if gpio_backend is None:
            gpio_backend = get_gpio_backend("auto")
        try:
            line = gpio_backend.request_output(pin, initial=active_low, consumer="incubator-led")
        except Exception as exc:
            logger.warning("status LED on GPIO %s unavailable: %s", pin, exc)
            return None
        logger.info("status LED: GPIO %s via %s", pin, getattr(gpio_backend, "name", "?"))
        return cls(_GpioLedDriver(line, spec, active_low), spec)

    # -- control -----------------------------------------------------------------
    @property
    def kind(self) -> str:
        return getattr(self._driver, "kind", "unknown")

    @property
    def name(self) -> str:
        return getattr(self._driver, "name", self.spec)

    def describe(self) -> dict[str, Any]:
        info = {"spec": self.spec, "pattern": self.pattern}
        try:
            info.update(self._driver.describe())
        except Exception:
            pass
        return info

    def _write(self, on: bool) -> None:
        if self._state is on:
            return
        self._state = on
        try:
            self._driver.set(on)
        except Exception as exc:
            logger.debug("LED write failed: %s", exc)

    def set_pattern(self, name: str) -> None:
        """Switch pattern immediately. Unknown names log once and turn the LED off."""
        name = (name or "off").strip().lower()
        if name not in PATTERNS:
            logger.warning("unknown LED pattern %r; turning LED off", name)
            name = "off"
        with self._lock:
            if self._closed:
                return
            if name in STEADY:
                # Steady states are written right here, under the I/O lock, so
                # the worker (which re-checks the pattern under the same lock
                # before every step) cannot overwrite them; it then parks.
                with self._io_lock:
                    self.pattern = name
                    self._write(STEADY[name])
            else:
                with self._io_lock:
                    self.pattern = name
                if self._thread is None or not self._thread.is_alive():
                    self._stop.clear()
                    self._thread = threading.Thread(target=self._run, name="status-led", daemon=True)
                    self._thread.start()
            self._wake.set()

    def on(self) -> None:
        self.set_pattern("on")

    def off(self) -> None:
        self.set_pattern("off")

    def _run(self) -> None:
        while not self._stop.is_set():
            pattern = self.pattern
            if pattern in STEADY:
                # set_pattern already wrote the level; just park until it changes.
                self._wake.wait()
                self._wake.clear()
                continue
            steps = PATTERNS.get(pattern, PATTERNS["off"])
            for on, seconds in steps:
                with self._io_lock:
                    if self.pattern != pattern or self._stop.is_set():
                        break  # changed under us: never write a stale step
                    self._write(on)
                if self._wake.wait(seconds):
                    self._wake.clear()
                    break  # pattern changed (or stopping) -> restart loop
                if self._stop.is_set():
                    break

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with self._io_lock:
                self.pattern = "off"
                self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        try:
            self._driver.close()
        except Exception as exc:
            logger.debug("LED close failed: %s", exc)

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def __repr__(self) -> str:
        return f"<StatusLed {self.spec} pattern={self.pattern}>"
