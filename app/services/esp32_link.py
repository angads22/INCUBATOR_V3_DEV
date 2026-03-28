import json
from dataclasses import dataclass

import serial


@dataclass
class ESP32Link:
    port: str
    baudrate: int
    timeout: float = 1.0

    def send_command(self, action: str, value: str | int | float | None = None) -> dict:
        payload = {"action": action, "value": value}
        line = json.dumps(payload).encode("utf-8") + b"\n"
        try:
            with serial.Serial(self.port, self.baudrate, timeout=self.timeout) as conn:
                conn.write(line)
                response = conn.readline().decode("utf-8").strip()
        except serial.SerialException as exc:
            return {"ok": False, "error": f"Serial link unavailable: {exc}", "hardware_online": False}

        if not response:
            return {"ok": False, "error": "No response from ESP32", "hardware_online": False}
        try:
            parsed = json.loads(response)
            parsed.setdefault("hardware_online", True)
            return parsed
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Invalid response: {response}", "hardware_online": False}
