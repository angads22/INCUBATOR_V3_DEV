from __future__ import annotations

import os
import threading
import time
from typing import Callable


class SetupButtonService:
    """Pin-2 setup trigger abstraction.

    Hardware path (UNO Q): can be wired to read GPIO value via a file endpoint.
    Dev path: set `INCUBATOR_PIN2_MOCK_FILE` to a file containing 0/1.
    """

    def __init__(self, hold_seconds: float, callback: Callable[[str], None]) -> None:
        self.hold_seconds = hold_seconds
        self.callback = callback
        self.mock_file = os.getenv("INCUBATOR_PIN2_MOCK_FILE", "")
        self._stop = False
        self._thread: threading.Thread | None = None

    def _read_pressed(self) -> bool:
        if self.mock_file and os.path.exists(self.mock_file):
            try:
                return open(self.mock_file, "r", encoding="utf-8").read().strip() == "1"
            except Exception:
                return False
        return False

    def _run(self) -> None:
        pressed_at: float | None = None
        while not self._stop:
            pressed = self._read_pressed()
            now = time.time()
            if pressed and pressed_at is None:
                pressed_at = now
            elif not pressed:
                pressed_at = None

            if pressed_at is not None and (now - pressed_at) >= self.hold_seconds:
                self.callback("pin2_long_press")
                pressed_at = None
                time.sleep(1.0)

            time.sleep(0.2)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
