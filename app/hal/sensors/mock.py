"""Fixed 37.4 degC / 55 % with a tiny jitter, for laptops and CI."""

from __future__ import annotations

import random
from typing import Any

from .base import Sensor, reading


class MockSensor(Sensor):
    name = "mock"

    def __init__(self, temperature_c: float = 37.4, humidity_pct: float = 55.0, jitter: float = 0.05) -> None:
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.jitter = jitter

    def read(self) -> dict[str, Any]:
        j = self.jitter
        return reading(
            self.name,
            ok=True,
            temperature_c=self.temperature_c + random.uniform(-j, j),
            humidity_pct=self.humidity_pct + random.uniform(-j * 4, j * 4),
            mock=True,
        )
