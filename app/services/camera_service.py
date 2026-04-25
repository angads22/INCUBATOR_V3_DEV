import struct
from dataclasses import dataclass
from pathlib import Path

from .esp32_link import ESP32Link


@dataclass
class CameraService:
    link: ESP32Link
    image_dir: Path = Path("./captures")

    def capture_image(self) -> dict:
        """Capture a JPEG from the ESP32-CAM.

        The firmware responds with a JSON line followed immediately by a
        4-byte little-endian length header and the raw JPEG bytes.  We read
        the binary frame here so it doesn't corrupt the next JSON exchange.
        """
        self.image_dir.mkdir(parents=True, exist_ok=True)

        response = self.link.send_command("capture_image")
        if not response.get("ok"):
            return response

        image_ref = response.get("value", "")
        jpeg_size = response.get("size", 0)

        if jpeg_size and jpeg_size > 0:
            jpeg_bytes = self._read_binary_frame(jpeg_size)
            if jpeg_bytes:
                dest = self.image_dir / f"{image_ref}.jpg"
                dest.write_bytes(jpeg_bytes)
                return {"ok": True, "image_ref": image_ref, "path": str(dest)}

        return {"ok": True, "image_ref": image_ref, "note": "no binary frame received"}

    # ── private ──────────────────────────────────────────────────────────────

    def _read_binary_frame(self, expected_size: int) -> bytes | None:
        """Read the raw binary JPEG that follows the JSON response line.

        Protocol:
            [4-byte LE uint32 length][JPEG bytes]
        """
        with self.link._lock:
            try:
                conn = self.link._connect()
                header = conn.read(4)
                if len(header) < 4:
                    return None
                length = struct.unpack("<I", header)[0]
                if length == 0 or length > 5_000_000:  # sanity cap 5 MB
                    return None
                data = b""
                remaining = length
                while remaining > 0:
                    chunk = conn.read(min(remaining, 4096))
                    if not chunk:
                        break
                    data += chunk
                    remaining -= len(chunk)
                return data if len(data) == length else None
            except Exception:
                return None
