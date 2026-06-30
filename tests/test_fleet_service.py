"""Fleet MQTT bus — pure logic (topics, telemetry, command dispatch).

Broker-free: exercises the parts that must be correct without a live broker.
"""

import json

from app.services.fleet_service import FleetMqttService


def _svc(**kw):
    return FleetMqttService("PI-A1B2C3D4", base_topic="fleet", device_name="Coop 1", **kw)


def test_topics_are_namespaced_by_device_id():
    s = _svc()
    assert s.topic("temp") == "fleet/PI-A1B2C3D4/temp"
    assert s.command_topic == "fleet/PI-A1B2C3D4/cmd"
    assert s.status_topic == "fleet/PI-A1B2C3D4/status"


def test_telemetry_messages_shape():
    s = _svc()
    snap = {"online": True, "temperature_c": 37.6, "humidity_pct": 55.2, "read_at": "2026-06-29T00:00:00Z"}
    msgs = {topic: (json.loads(payload), retain) for topic, payload, retain in s.telemetry_messages(snap)}

    status, retain = msgs["fleet/PI-A1B2C3D4/status"]
    assert retain is True  # status is retained for late-joining hubs
    assert status["device_id"] == "PI-A1B2C3D4"
    assert status["name"] == "Coop 1"
    assert status["online"] is True
    assert status["temperature_c"] == 37.6

    assert msgs["fleet/PI-A1B2C3D4/temp"][0] == 37.6
    assert msgs["fleet/PI-A1B2C3D4/humidity"][0] == 55.2


def test_telemetry_omits_missing_readings():
    s = _svc()
    topics = {t for t, _, _ in s.telemetry_messages({"online": False, "temperature_c": None, "humidity_pct": None})}
    assert topics == {"fleet/PI-A1B2C3D4/status"}  # no temp/humidity topics when null


def test_handle_command_dispatches_to_injected_callable():
    seen = {}

    def dispatch(action, value):
        seen["action"] = action
        seen["value"] = value
        return {"ok": True, "did": action}

    s = _svc(command_dispatch=dispatch)
    result = s.handle_command(json.dumps({"action": "heater_on", "value": None}))
    assert result == {"ok": True, "did": "heater_on"}
    assert seen == {"action": "heater_on", "value": None}


def test_handle_command_tolerates_junk():
    s = _svc(command_dispatch=lambda a, v: {"ok": True})
    assert s.handle_command("not json")["error"] == "invalid_json"
    assert s.handle_command(json.dumps(["list"]))["error"] == "payload_not_object"
    assert s.handle_command(json.dumps({"value": 1}))["error"] == "missing_action"


def test_handle_command_without_dispatcher_is_safe():
    s = _svc()  # no dispatcher injected
    assert s.handle_command(json.dumps({"action": "heater_on"}))["error"] == "no_dispatcher"


def test_start_without_broker_host_is_a_noop():
    s = _svc(command_dispatch=lambda a, v: {"ok": True})
    assert s.start() is False  # no host configured → bus stays down, no raise
