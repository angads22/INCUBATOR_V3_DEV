"""GPIO abstraction shared by every backend.

A *pin* is described by :class:`PinSpec`; a backend turns it into an
:class:`OutputLine` or :class:`InputLine`. Backends never import hardware
libraries at module import time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

Pull = Literal["up", "down", "none"]


class GpioBackendUnavailable(RuntimeError):
    """Raised when an explicitly requested GPIO backend cannot be used."""


@dataclass(frozen=True)
class PinSpec:
    """Where a GPIO line lives.

    ``chip`` is a gpiochip name or device path (``gpiochip0`` / ``/dev/gpiochip4``)
    or None for "resolve it for me"; ``offset`` is the line offset on that chip
    (on a Raspberry Pi the BCM number); ``name`` is a kernel line name such as
    ``GPIO17`` or ``PA12`` that the backend looks up across all chips.
    """

    chip: str | None = None
    offset: int | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if self.offset is None and not self.name:
            raise ValueError("PinSpec needs an offset or a line name")
        if self.offset is not None and self.offset < 0:
            raise ValueError(f"GPIO offset must be >= 0, got {self.offset}")

    @property
    def key(self) -> str:
        """Stable string used as a dict key / log label."""
        return str(self)

    def __str__(self) -> str:
        if self.name and self.offset is None:
            return self.name if not self.chip else f"{self.chip}:{self.name}"
        if self.chip:
            return f"{self.chip}:{self.offset}"
        return str(self.offset)


class OutputLine(ABC):
    """A requested output line. ``set(True)`` drives the line HIGH (active)."""

    @abstractmethod
    def set(self, value: bool) -> None: ...

    @abstractmethod
    def get(self) -> bool: ...

    @abstractmethod
    def release(self) -> None: ...

    def __enter__(self) -> "OutputLine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class InputLine(ABC):
    """A requested input line. ``get()`` returns True when the line is HIGH."""

    @abstractmethod
    def get(self) -> bool: ...

    @abstractmethod
    def release(self) -> None: ...

    def __enter__(self) -> "InputLine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class GpioBackend(ABC):
    """Factory for lines. One instance per process is plenty."""

    name: str = "base"

    @abstractmethod
    def request_output(self, pin: PinSpec, initial: bool, consumer: str = "incubator") -> OutputLine: ...

    @abstractmethod
    def request_input(self, pin: PinSpec, pull: Pull = "up", consumer: str = "incubator") -> InputLine: ...

    def close(self) -> None:
        """Release every line this backend handed out. Idempotent."""

    # Optional introspection used by ``python -m app.hal``; backends that can
    # enumerate chips override these.
    def chips(self) -> list[dict]:
        return []

    def line_name(self, chip: str, offset: int) -> str | None:
        return None

    def __repr__(self) -> str:
        return f"<GpioBackend {self.name}>"
