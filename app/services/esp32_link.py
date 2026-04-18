import json
import logging
import threading
from dataclasses import dataclass

import serial

logger = logging.getLogger(__name__)


@dataclass
class ESP32Link:
    port: str
    baudrate: int
    timeout: float = 1.0

    def __post_init__(self):
        self._conn: serial.Serial | None = None
        self._lock = threading.Lock()

    def _connect(self) -> serial.Serial:
        if self._conn and self._conn.is_open:
            return self._conn
        self._conn = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        logger.debug("ESP32 serial connection opened on %s", self.port)
        return self._conn

    def send_command(self, action: str, value: str | int | float | None = None) -> dict:
        payload = {"action": action, "value": value}
        line = json.dumps(payload).encode("utf-8") + b"\n"
        with self._lock:
            try:
                conn = self._connect()
                conn.write(line)
                response = conn.readline().decode("utf-8").strip()
            except serial.SerialException as exc:
                logger.warning("ESP32 serial error on %s, will reconnect: %s", self.port, exc)
                self._conn = None
                return {"ok": False, "error": f"Serial error: {exc}"}
            except OSError as exc:
                logger.warning("ESP32 IO error on %s: %s", self.port, exc)
                self._conn = None
                return {"ok": False, "error": f"IO error: {exc}"}
        if not response:
            return {"ok": False, "error": "No response from ESP32"}
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"Invalid response: {response}"}
