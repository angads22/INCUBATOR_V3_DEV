"""In-memory GPIO backend for laptops, CI and ``GPIO_MOCK=true``."""

from __future__ import annotations

import threading

from .base import GpioBackend, InputLine, OutputLine, PinSpec, Pull


class MockLine(OutputLine, InputLine):
    def __init__(self, backend: "MockGpioBackend", pin: PinSpec, value: bool, direction: str, pull: str = "none") -> None:
        self._backend = backend
        self.pin = pin
        self.value = value
        self.direction = direction
        self.pull = pull
        self.released = False

    def set(self, value: bool) -> None:
        self.value = bool(value)

    def get(self) -> bool:
        return bool(self.value)

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._backend._forget(self)

    def __repr__(self) -> str:
        return f"<MockLine {self.pin} {self.direction} value={int(self.value)}>"


class MockGpioBackend(GpioBackend):
    """Keeps every requested line in ``self.lines`` so tests can inspect them."""

    name = "mock"

    def __init__(self, chip: str = "") -> None:
        self.chip = chip
        self.lines: dict[str, MockLine] = {}
        self._lock = threading.Lock()

    def request_output(self, pin: PinSpec, initial: bool, consumer: str = "incubator") -> OutputLine:
        line = MockLine(self, pin, bool(initial), "out")
        with self._lock:
            self.lines[pin.key] = line
        return line

    def request_input(self, pin: PinSpec, pull: Pull = "up", consumer: str = "incubator") -> InputLine:
        # An idle pulled-up input reads HIGH, a pulled-down one LOW.
        line = MockLine(self, pin, pull == "up", "in", pull)
        with self._lock:
            self.lines[pin.key] = line
        return line

    def set_input_value(self, pin: PinSpec | str, value: bool) -> None:
        """Test hook: simulate the electrical level on an input."""
        key = pin.key if isinstance(pin, PinSpec) else str(pin)
        with self._lock:
            line = self.lines.get(key)
        if line is None:
            raise KeyError(f"no mock line requested for {key}")
        line.value = bool(value)

    def _forget(self, line: MockLine) -> None:
        with self._lock:
            if self.lines.get(line.pin.key) is line:
                del self.lines[line.pin.key]

    def close(self) -> None:
        with self._lock:
            lines = list(self.lines.values())
        for line in lines:
            line.release()
