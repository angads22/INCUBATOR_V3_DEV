from dataclasses import dataclass
from pathlib import Path

from .esp32_link import ESP32Link


@dataclass
class CameraService:
    link: ESP32Link
    image_dir: Path = Path("./captures")

    def capture_image(self) -> dict:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        response = self.link.send_command("capture_image")
        if not response.get("ok"):
            return response
        return {
            "ok": True,
            "image_ref": response.get("image_ref"),
            "note": "Image bytes transfer implementation will be integrated with ESP32 protocol.",
        }
