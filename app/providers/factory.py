from ..config import settings
from ..services.hardware_service import HardwareService
from .base import HardwareProvider
from .esp32 import ESP32HardwareProvider
from .simulated import SimulatedHardwareProvider


def build_provider(hardware_service: HardwareService) -> HardwareProvider:
    if settings.device_mode == "hardware":
        return ESP32HardwareProvider(hardware_service)
    return SimulatedHardwareProvider()
