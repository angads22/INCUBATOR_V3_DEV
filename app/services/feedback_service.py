"""
Operator feedback without a screen: status LED patterns + buzzer chirps.

| Event                 | Buzzer                          | Status LED               |
| boot                  | 1 short                         | fast blink (then pattern)|
| setup_ready           | 2 short                         | double-blink (pairing)   |
| connecting            | -                               | fast blink               |
| connected             | short-short-long (rising)       | slow heartbeat           |
| failed                | 1 long                          | SOS                      |
| button_ack            | 1 medium                        | -                        |
| factory_reset_armed   | 3 rapid                         | fast blink               |
| factory_reset         | 1 long                          | fast blink               |
| alarm                 | - (the alert engine owns it)    | -                        |

The buzzer is the same line the alert engine latches for alarms. Chirps are
played through ``GPIOService.pulse_alarm`` on a daemon thread: a pulse is
skipped while an alarm is latched, and the pulse's off-edge never clears an
alarm that was raised mid-chirp, so feedback can never silence a real alert.
``event()`` returns immediately; a new pattern replaces one still pending.
Everything is a no-op when the hardware is missing or ``sound`` is False.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# name -> list of (beep seconds, pause-after seconds)
BUZZER_PATTERNS: dict[str, list[tuple[float, float]]] = {
    "boot": [(0.08, 0.0)],
    "setup_ready": [(0.08, 0.12), (0.08, 0.0)],
    "connecting": [],
    "connected": [(0.08, 0.1), (0.08, 0.1), (0.4, 0.0)],
    "failed": [(0.7, 0.0)],
    "button_ack": [(0.25, 0.0)],
    "factory_reset_armed": [(0.06, 0.08), (0.06, 0.08), (0.06, 0.0)],
    "factory_reset": [(0.9, 0.0)],
    "alarm": [],
}

# name -> StatusLed pattern, or None for "leave the LED alone"
LED_PATTERNS: dict[str, str | None] = {
    "boot": "blink_fast",
    "setup_ready": "blink_double",
    "connecting": "blink_fast",
    "connected": "heartbeat",
    "failed": "sos",
    "button_ack": None,
    "factory_reset_armed": "blink_fast",
    "factory_reset": "blink_fast",
    "alarm": None,
}

EVENTS: tuple[str, ...] = tuple(BUZZER_PATTERNS)


class FeedbackService:
    def __init__(self, gpio: Any, led: Any, sound: bool = True, *, sleep: Any = None) -> None:
        self._gpio = gpio
        self._led = led
        self._sound = bool(sound)
        self._sleep = sleep or threading.Event().wait  # interruptible sleep
        self._lock = threading.Lock()
        self._pending: list[tuple[float, float]] | None = None
        self._pending_name = ""
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.last_event: str | None = None
        self.events: list[str] = []

    # -- public -----------------------------------------------------------
    @property
    def led(self) -> Any:
        return self._led

    @property
    def sound(self) -> bool:
        return self._sound

    def event(self, name: str) -> None:
        """Play the LED + buzzer feedback for ``name``. Never raises, never blocks."""
        name = (name or "").strip().lower()
        if name not in BUZZER_PATTERNS:
            logger.debug("feedback: unknown event %r ignored", name)
            return
        if self._closed:
            return
        self.last_event = name
        self.events.append(name)
        if len(self.events) > 50:
            del self.events[:-50]
        self._set_led(LED_PATTERNS.get(name))
        pattern = BUZZER_PATTERNS[name]
        if self._sound and pattern and self._gpio is not None:
            self._queue(name, pattern)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(2.0)
        if self._led is not None:
            try:
                self._led.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("feedback: led close failed: %s", exc)

    # -- internals --------------------------------------------------------
    def _set_led(self, pattern: str | None) -> None:
        if pattern is None or self._led is None:
            return
        try:
            self._led.set_pattern(pattern)
        except Exception as exc:  # noqa: BLE001 — a dead LED must never matter
            logger.debug("feedback: led pattern %r failed: %s", pattern, exc)

    def _alarm_latched(self) -> bool:
        try:
            return bool(self._gpio.get_state().get("alarm"))
        except Exception:  # noqa: BLE001
            return False

    def _queue(self, name: str, pattern: list[tuple[float, float]]) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending = list(pattern)
            self._pending_name = name
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="feedback-buzzer", daemon=True)
                self._thread.start()
            self._wake.set()

    def _take(self) -> tuple[str, list[tuple[float, float]] | None]:
        with self._lock:
            pattern, name = self._pending, self._pending_name
            self._pending = None
            return name, pattern

    def _run(self) -> None:
        while not self._stop.is_set():
            name, pattern = self._take()
            if pattern is None:
                self._wake.wait()
                self._wake.clear()
                continue
            self._play(name, pattern)

    def _play(self, name: str, pattern: list[tuple[float, float]]) -> None:
        for on_s, off_s in pattern:
            if self._stop.is_set():
                return
            if self._alarm_latched():
                logger.debug("feedback: %s chirp skipped — alarm is sounding", name)
                return
            try:
                result = self._gpio.pulse_alarm(on_s)
            except Exception as exc:  # noqa: BLE001
                logger.debug("feedback: buzzer write failed: %s", exc)
                return
            if isinstance(result, dict) and result.get("pulsed") is False:
                return
            # Wait out the beep plus its pause before the next one.
            self._sleep(on_s + off_s + 0.02)
