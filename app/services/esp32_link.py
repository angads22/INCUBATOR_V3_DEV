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
        with serial.Serial(self.port, self.baudrate, timeout=self.timeout) as conn:
            conn.write(line)
            response = conn.readline().decode("utf-8").strip()
        if not response:
            return {"ok": False, "error": "No response from ESP32"}
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Invalid response: {response}"}
