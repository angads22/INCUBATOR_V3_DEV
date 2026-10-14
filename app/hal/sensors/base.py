"""Sensor interface shared by every backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


def reading(
    name: str,
    *,
    ok: bool,
    temperature_c: float | None = None,
    humidity_pct: float | None = None,
    mock: bool = False,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the uniform reading dict every sensor returns."""
    out: dict[str, Any] = {
        "ok": bool(ok),
        "temperature_c": None if temperature_c is None else round(float(temperature_c), 2),
        "humidity_pct": None if humidity_pct is None else round(float(humidity_pct), 2),
        "mock": bool(mock),
        "sensor": name,
    }
    if error:
        out["error"] = error
    out.update(extra)
    return out


class Sensor(ABC):
    """A temperature/humidity sensor.

    ``read()`` never raises: failures come back as ``{"ok": False, "error": ...}``
    so the control loop can treat *any* doubt as "no reading" (heater off).
    """

    name: str = "sensor"

    @abstractmethod
    def read(self) -> dict[str, Any]: ...

    def close(self) -> None:
        """Release the bus/device. Idempotent."""

    def __repr__(self) -> str:
        return f"<Sensor {self.name}>"
