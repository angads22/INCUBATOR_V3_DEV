from dataclasses import dataclass
from datetime import datetime


@dataclass
class EnvironmentState:
    temperature_c: float
    humidity_pct: float
    target_temp_c: float
    target_humidity_pct: float
    heater_on: bool
    fan_on: bool
    turner_on: bool
    alarm_active: bool
    hardware_online: bool
    sensor_online: bool
    simulated_mode: bool
    last_updated: datetime


@dataclass
class ControlResult:
    ok: bool
    message: str
