"""
Physical setup button monitor for Pi Zero 2W.

Three ways to detect a press:
  1. RPi.GPIO edge detection on setup_button_pin (BCM) — production path
  2. GPIOService.read_button() polling fallback when edge detection unavailable
  3. INCUBATOR_BUTTON_MOCK_FILE — contains "1" when pressed, for dev machines

Holds the button for `hold_seconds` to avoid accidental triggers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as _GPIO  # type: ignore

    _HAS_GPIO = True
except Exception:
    _HAS_GPIO = False


class SetupButtonService:
    def __init__(
        self,
        hold_seconds: float,
        callback: Callable[[str], None],
        gpio_pin: int = 18,
        mock_file: str = "",
    ) -> None:
        self.hold_seconds = hold_seconds
        self.callback = callback
        self.gpio_pin = gpio_pin
        self.mock_file = mock_file or os.getenv("INCUBATOR_BUTTON_MOCK_FILE", "")
        self._stop = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _read_pressed(self) -> bool:
        # Mock file takes priority (dev mode)
        if self.mock_file and os.path.exists(self.mock_file):
            try:
                with open(self.mock_file) as f:
                    return f.read().strip() == "1"
            except OSError:
                return False
        # Real hardware
        if _HAS_GPIO:
            try:
                return _GPIO.input(self.gpio_pin) == _GPIO.LOW
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        pressed_at: float | None = None
        triggered = False
        while not self._stop:
            pressed = self._read_pressed()
            now = time.monotonic()
            if pressed:
                if pressed_at is None:
                    pressed_at = now
                    triggered = False
                elif not triggered and (now - pressed_at) >= self.hold_seconds:
                    logger.info("Setup button held for %.1fs — triggering callback", self.hold_seconds)
                    self.callback("button_hold")
                    triggered = True
            else:
                pressed_at = None
                triggered = False
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        if _HAS_GPIO and not self.mock_file:
            # Setup already done by GPIOService.setup(); just start polling
            logger.debug("SetupButtonService: using RPi.GPIO on BCM pin %d", self.gpio_pin)
        self._thread = threading.Thread(target=self._run, daemon=True, name="button-svc")
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
