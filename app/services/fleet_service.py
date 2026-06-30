"""Fleet MQTT bus — Phase 1 of the fleet upgrade.

Each incubator is an independently addressable MQTT client. It publishes
telemetry under ``<base>/<device_id>/...`` and accepts commands on
``<base>/<device_id>/cmd``. ``device_id`` (the existing ``PI-xxxx``) is what
makes every unit independently identifiable and controllable from one hub.

Design notes:
  * The topic-building, telemetry-shaping, and command-dispatch logic are pure
    and unit-tested without a broker (see tests/test_fleet_service.py).
  * The paho client is wrapped so a missing/oﬄine broker NEVER breaks local
    incubation — every network path is best-effort and logged, never raised.
  * Commands are dispatched through an injected callable so this service has no
    direct dependency on the hardware layer (keeps it testable + decoupled).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# (action, value) -> result dict. Injected by main so we don't import hardware.
CommandDispatch = Callable[[str, Any], dict]
# Returns the latest cached sensor snapshot (same shape as /api/sensors/latest).
TelemetrySource = Callable[[], dict]


class FleetMqttService:
    def __init__(
        self,
        device_id: str,
        *,
        base_topic: str = "fleet",
        host: str = "",
        port: int = 1883,
        username: str = "",
        password: str = "",
        command_dispatch: CommandDispatch | None = None,
        telemetry_source: TelemetrySource | None = None,
        device_name: str = "",
    ) -> None:
        self._device_id = device_id or "PI-UNKNOWN"
        self._base = (base_topic or "fleet").strip("/")
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._dispatch = command_dispatch
        self._telemetry_source = telemetry_source
        self._device_name = device_name
        self._client: Any = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Pure helpers (unit-tested without a broker)
    # ------------------------------------------------------------------

    def topic(self, suffix: str) -> str:
        return f"{self._base}/{self._device_id}/{suffix.strip('/')}"

    @property
    def command_topic(self) -> str:
        return self.topic("cmd")

    @property
    def status_topic(self) -> str:
        return self.topic("status")

    def telemetry_messages(self, snapshot: dict | None = None) -> list[tuple[str, str, bool]]:
        """Return (topic, json_payload, retain) tuples to publish.

        ``status`` is retained so a late-joining hub sees the last known state.
        """
        if snapshot is None and self._telemetry_source is not None:
            snapshot = self._telemetry_source()
        snapshot = snapshot or {}
        temp = snapshot.get("temperature_c")
        hum = snapshot.get("humidity_pct")
        status = {
            "device_id": self._device_id,
            "name": self._device_name,
            "online": bool(snapshot.get("online", False)),
            "temperature_c": temp,
            "humidity_pct": hum,
            "read_at": snapshot.get("read_at"),
        }
        messages: list[tuple[str, str, bool]] = [
            (self.status_topic, json.dumps(status), True),
        ]
        if temp is not None:
            messages.append((self.topic("temp"), json.dumps(temp), False))
        if hum is not None:
            messages.append((self.topic("humidity"), json.dumps(hum), False))
        return messages

    def handle_command(self, raw_payload: str | bytes) -> dict:
        """Parse a command payload and run it through the injected dispatcher.

        Payload: ``{"action": "heater_on", "value": null}``. Returns a result
        dict; malformed payloads or unknown actions return an error dict rather
        than raising (the bus must tolerate junk).
        """
        try:
            data = json.loads(raw_payload)
        except (ValueError, TypeError):
            return {"ok": False, "error": "invalid_json"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "payload_not_object"}
        action = data.get("action")
        if not action or not isinstance(action, str):
            return {"ok": False, "error": "missing_action"}
        if self._dispatch is None:
            return {"ok": False, "error": "no_dispatcher"}
        try:
            result = self._dispatch(action, data.get("value"))
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 — never let a bad command crash the bus
            logger.warning("Fleet command '%s' failed: %s", action, exc)
            return {"ok": False, "error": str(exc), "action": action}

    # ------------------------------------------------------------------
    # Broker wiring (best-effort; missing broker must not break incubation)
    # ------------------------------------------------------------------

    def start(self) -> bool:
        if not self._host:
            logger.info("Fleet MQTT: no broker host configured — bus disabled.")
            return False
        try:
            import paho.mqtt.client as mqtt

            client = mqtt.Client(client_id=f"incubator-{self._device_id}", clean_session=True)
            if self._username:
                client.username_pw_set(self._username, self._password or None)
            # Last-will so the hub sees a unit drop off immediately.
            client.will_set(self.status_topic, json.dumps({"device_id": self._device_id, "online": False}), retain=True)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.connect_async(self._host, self._port, keepalive=60)
            client.loop_start()
            with self._lock:
                self._client = client
            logger.info("Fleet MQTT: connecting to %s:%d as %s", self._host, self._port, self._device_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fleet MQTT: could not start (continuing without bus): %s", exc)
            return False

    def _on_connect(self, client, userdata, flags, rc, *args) -> None:  # noqa: ANN001
        try:
            client.subscribe(self.command_topic, qos=1)
            self.publish_telemetry()
            logger.info("Fleet MQTT: connected, subscribed to %s", self.command_topic)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fleet MQTT on_connect error: %s", exc)

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ANN001
        result = self.handle_command(msg.payload)
        try:
            client.publish(self.topic("cmd/result"), json.dumps(result), qos=1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fleet MQTT result publish error: %s", exc)

    def publish_telemetry(self, snapshot: dict | None = None) -> None:
        with self._lock:
            client = self._client
        if client is None:
            return
        for topic, payload, retain in self.telemetry_messages(snapshot):
            try:
                client.publish(topic, payload, qos=0, retain=retain)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Fleet MQTT publish error on %s: %s", topic, exc)

    def stop(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is None:
            return
        try:
            client.publish(self.status_topic, json.dumps({"device_id": self._device_id, "online": False}), retain=True)
            client.loop_stop()
            client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Fleet MQTT stop error: %s", exc)
