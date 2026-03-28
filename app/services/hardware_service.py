from dataclasses import dataclass

from .camera_service import CameraService
from .esp32_link import ESP32Link


@dataclass
class HardwareService:
    link: ESP32Link
    camera: CameraService

    def open_lock(self) -> dict:
        return self.link.send_command("open_lock")

    def close_lock(self) -> dict:
        return self.link.send_command("close_lock")

    def open_door(self) -> dict:
        return self.link.send_command("open_door")

    def close_door(self) -> dict:
        return self.link.send_command("close_door")

    def move_motor(self, value: int | str) -> dict:
        return self.link.send_command("move_motor", value)

    def read_temp(self) -> dict:
        return self.link.send_command("read_temp")

    def read_humidity(self) -> dict:
        return self.link.send_command("read_humidity")

    def set_candle(self, on: bool) -> dict:
        return self.link.send_command("set_candle", "on" if on else "off")

    def capture_image(self) -> dict:
        return self.camera.capture_image()
