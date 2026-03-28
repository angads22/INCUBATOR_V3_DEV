from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from ..domain import ControlResult, EnvironmentState
from .base import HardwareProvider


@dataclass
class SimState:
    temperature_c: float = 37.1
    humidity_pct: float = 53.0
    target_temp_c: float = 37.5
    target_humidity_pct: float = 55.0
    heater_on: bool = False
    fan_on: bool = True
    turner_on: bool = True
    alarm_active: bool = False
    hardware_online: bool = True
    sensor_online: bool = True
    last_updated: datetime = datetime.utcnow()


class SimulatedHardwareProvider(HardwareProvider):
    def __init__(self) -> None:
        self.state = SimState()

    def _step(self) -> None:
        # Simple stable simulation around setpoints.
        drift_t = (self.state.target_temp_c - self.state.temperature_c) * 0.12
        drift_h = (self.state.target_humidity_pct - self.state.humidity_pct) * 0.10

        heater_boost = 0.07 if self.state.heater_on else -0.03
        fan_effect = -0.05 if self.state.fan_on and self.state.temperature_c > self.state.target_temp_c else 0.01

        self.state.temperature_c += drift_t + heater_boost + fan_effect + random.uniform(-0.03, 0.03)
        self.state.humidity_pct += drift_h + random.uniform(-0.25, 0.25)

        self.state.temperature_c = round(max(34.0, min(39.5, self.state.temperature_c)), 2)
        self.state.humidity_pct = round(max(35.0, min(75.0, self.state.humidity_pct)), 2)

        self.state.alarm_active = abs(self.state.temperature_c - self.state.target_temp_c) > 1.0
        self.state.last_updated = datetime.utcnow()

    def read_environment(self) -> EnvironmentState:
        self._step()
        return EnvironmentState(
            temperature_c=self.state.temperature_c,
            humidity_pct=self.state.humidity_pct,
            target_temp_c=self.state.target_temp_c,
            target_humidity_pct=self.state.target_humidity_pct,
            heater_on=self.state.heater_on,
            fan_on=self.state.fan_on,
            turner_on=self.state.turner_on,
            alarm_active=self.state.alarm_active,
            hardware_online=self.state.hardware_online,
            sensor_online=self.state.sensor_online,
            simulated_mode=True,
            last_updated=self.state.last_updated,
        )

    def set_targets(self, temp_c: float, humidity_pct: float) -> None:
        self.state.target_temp_c = float(temp_c)
        self.state.target_humidity_pct = float(humidity_pct)

    def set_heater(self, enabled: bool) -> ControlResult:
        self.state.heater_on = enabled
        return ControlResult(ok=True, message=f"Heater {'on' if enabled else 'off'} (simulated)")

    def set_fan(self, enabled: bool) -> ControlResult:
        self.state.fan_on = enabled
        return ControlResult(ok=True, message=f"Fan {'on' if enabled else 'off'} (simulated)")

    def run_turn_cycle(self) -> ControlResult:
        self.state.turner_on = True
        return ControlResult(ok=True, message="Turn cycle executed (simulated)")

    def reset_alarm(self) -> ControlResult:
        self.state.alarm_active = False
        return ControlResult(ok=True, message="Alarm reset (simulated)")
