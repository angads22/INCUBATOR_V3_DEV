"""
Physical setup-button monitor.

The button is read through an injected ``read_pressed()`` callable (normally
``GPIOService.read_button``, which goes through the HAL) or, on dev machines,
the ``INCUBATOR_BUTTON_MOCK_FILE`` file containing ``1`` while "pressed". No
hardware library is imported here.

Gestures (all timings configurable):
  * hold >= ``hold_seconds`` (4 s)            -> ``callback("button_hold")`` once
    (setup mode: the device raises its own Wi-Fi again);
  * hold >= ``factory_reset_hold_seconds``   -> ``on_progress("factory_reset_armed")``
    (15 s)                                      once, so the buzzer/LED can warn;
  * RELEASE after the reset was armed         -> ``callback("factory_reset")``.

Release is what fires the reset: there is no way to abort once armed except
by not having held for 15 s in the first place — the armed warning (3 rapid
beeps, fast LED) is the operator's cue. A hold that never reaches the arm
threshold only triggers setup mode. Set ``factory_reset_hold_seconds`` to 0
to disable factory reset from the button entirely.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class SetupButtonService:
    def __init__(
        self,
        hold_seconds: float,
        callback: Callable[[str], None],
        *,
        read_pressed: Callable[[], bool] | None = None,
        gpio_pin: str | int = "18",
        mock_file: str = "",
        factory_reset_hold_seconds: float = 15.0,
        on_progress: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 0.1,
    ) -> None:
        self.hold_seconds = float(hold_seconds)
        self.factory_reset_hold_seconds = float(factory_reset_hold_seconds or 0.0)
        if 0 < self.factory_reset_hold_seconds <= self.hold_seconds:
            logger.warning(
                "factory_reset_hold_seconds (%.1f) must exceed hold_seconds (%.1f) — factory reset from the button disabled",
                self.factory_reset_hold_seconds, self.hold_seconds,
            )
            self.factory_reset_hold_seconds = 0.0
        self.callback = callback
        self.on_progress = on_progress
        self.gpio_pin = gpio_pin
        self.mock_file = mock_file or os.getenv("INCUBATOR_BUTTON_MOCK_FILE", "")
        self._read_pressed_fn = read_pressed
        self._clock = clock
        self._poll_interval = max(0.01, float(poll_interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Gesture state (one press at a time).
        self._pressed_at: float | None = None
        self._hold_fired = False
        self._reset_armed = False

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _read_mock_file(self) -> bool | None:
        """``True``/``False`` from the mock file, or None when there is none."""
        if not self.mock_file or not os.path.exists(self.mock_file):
            return None
        try:
            with open(self.mock_file) as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    def _read_pressed(self) -> bool:
        # A present mock file wins (dev machines), then the injected reader.
        mocked = self._read_mock_file()
        if mocked is not None:
            return mocked
        if self._read_pressed_fn is not None:
            try:
                return bool(self._read_pressed_fn())
            except Exception as exc:  # noqa: BLE001 — a flaky read must not kill the monitor
                logger.debug("button read failed: %s", exc)
        return False

    # ------------------------------------------------------------------
    # Gesture state machine (one step; testable with a fake clock)
    # ------------------------------------------------------------------

    def _fire(self, fn: Callable[[str], None] | None, event: str) -> None:
        if fn is None:
            return
        try:
            fn(event)
        except Exception as exc:  # noqa: BLE001 — never let a handler kill the monitor thread
            logger.error("button handler for %r failed: %s", event, exc)

    def tick(self, pressed: bool | None = None, now: float | None = None) -> str | None:
        """Advance the gesture state machine. Returns the event fired, if any."""
        pressed = self._read_pressed() if pressed is None else bool(pressed)
        now = self._clock() if now is None else now
        if pressed:
            if self._pressed_at is None:
                self._pressed_at = now
                self._hold_fired = False
                self._reset_armed = False
                return None
            held = now - self._pressed_at
            if not self._hold_fired and held >= self.hold_seconds:
                self._hold_fired = True
                logger.info("Setup button held for %.1fs — setup mode requested", self.hold_seconds)
                self._fire(self.callback, "button_hold")
                return "button_hold"
            if (
                self.factory_reset_hold_seconds > 0
                and not self._reset_armed
                and held >= self.factory_reset_hold_seconds
            ):
                self._reset_armed = True
                logger.warning(
                    "Setup button held for %.0fs — FACTORY RESET armed; releasing the button will wipe the device",
                    self.factory_reset_hold_seconds,
                )
                self._fire(self.on_progress, "factory_reset_armed")
                return "factory_reset_armed"
            return None
        # Released.
        armed = self._reset_armed
        self._pressed_at = None
        self._hold_fired = False
        self._reset_armed = False
        if armed:
            logger.warning("Setup button released after factory-reset arm — resetting")
            self._fire(self.callback, "factory_reset")
            return "factory_reset"
        return None

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("button tick error: %s", exc)
            self._stop.wait(self._poll_interval)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        src = "mock file" if self.mock_file else ("gpio reader" if self._read_pressed_fn else "none")
        logger.debug("SetupButtonService: pin %s via %s (hold %.1fs, reset %.0fs)",
                     self.gpio_pin, src, self.hold_seconds, self.factory_reset_hold_seconds)
        self._thread = threading.Thread(target=self._run, daemon=True, name="button-svc")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
