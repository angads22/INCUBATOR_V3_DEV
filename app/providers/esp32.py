from datetime import datetime

from ..domain import ControlResult, EnvironmentState
from ..services.hardware_service import HardwareService
from .base import HardwareProvider


class ESP32HardwareProvider(HardwareProvider):
    def __init__(self, hardware_service: HardwareService, target_temp_c: float = 37.5, target_humidity_pct: float = 55.0):
        self.hw = hardware_service
        self.target_temp_c = target_temp_c
        self.target_humidity_pct = target_humidity_pct
        self.heater_on = False
        self.fan_on = True
        self.turner_on = True

    def read_environment(self) -> EnvironmentState:
        temp = self.hw.read_temp()
        humidity = self.hw.read_humidity()
        ok = temp.get("ok") and humidity.get("ok")

        return EnvironmentState(
            temperature_c=float(temp.get("value", 0.0) or 0.0),
            humidity_pct=float(humidity.get("value", 0.0) or 0.0),
            target_temp_c=self.target_temp_c,
            target_humidity_pct=self.target_humidity_pct,
            heater_on=self.heater_on,
            fan_on=self.fan_on,
            turner_on=self.turner_on,
            alarm_active=not bool(ok),
            hardware_online=bool(temp.get("hardware_online", True) and humidity.get("hardware_online", True)),
            sensor_online=bool(ok),
            simulated_mode=False,
            last_updated=datetime.utcnow(),
        )

    def set_heater(self, enabled: bool) -> ControlResult:
        self.heater_on = enabled
        result = self.hw.set_candle(enabled)
        return ControlResult(ok=bool(result.get("ok")), message=result.get("error", "ok"))

    def set_fan(self, enabled: bool) -> ControlResult:
        self.fan_on = enabled
        result = self.hw.link.send_command("set_fan", "on" if enabled else "off")
        return ControlResult(ok=bool(result.get("ok")), message=result.get("error", "ok"))

    def run_turn_cycle(self) -> ControlResult:
        result = self.hw.move_motor("turn_cycle")
        return ControlResult(ok=bool(result.get("ok")), message=result.get("error", "ok"))

    def reset_alarm(self) -> ControlResult:
        return ControlResult(ok=True, message="Alarm reset")
