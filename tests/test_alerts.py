"""Alert engine: range alerts, debounce, buzzer, silencing, and event logging."""

import pytest
from sqlalchemy import select

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}

NORMAL = {"ok": True, "mock": True, "temperature_c": 37.4, "humidity_pct": 55.0}
TOO_HOT = {"ok": True, "mock": True, "temperature_c": 45.0, "humidity_pct": 55.0}


@pytest.fixture(autouse=True)
def _fresh_alert_state():
    from app import main

    main.alert_service.reset()
    main.gpio_service.set_alarm(False)
    yield
    main.alert_service.reset()
    main.gpio_service.set_alarm(False)


def _onboard(client):
    return client.post(
        "/onboarding/complete",
        json={
            "ssid": "HomeNet",
            "wifi_password": "",
            "device_name": "Coop 1",
            "create_account": True,
            **ACCOUNT,
        },
    )


def _poll(times=1):
    from app import main

    for _ in range(times):
        main._sensor_poller._poll()


def test_out_of_range_is_debounced_then_raises_and_sounds_alarm(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(TOO_HOT))
    _poll()
    assert main.alert_service.alert_state()["active"] == []  # one poll is not enough

    _poll()
    state = main.alert_service.alert_state()
    assert [a["type"] for a in state["active"]] == ["temp_high"]
    assert main.gpio_service.get_state()["alarm"] is True

    # Back in range: alert clears and the buzzer stops.
    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(NORMAL))
    _poll()
    assert main.alert_service.alert_state()["active"] == []
    assert main.gpio_service.get_state()["alarm"] is False


def test_alert_transitions_are_logged_as_system_events(client, monkeypatch):
    from app import main
    from app.database import get_db
    from app.models import ActionLog

    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(TOO_HOT))
    _poll(times=2)
    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(NORMAL))
    _poll()

    db = next(get_db())
    try:
        actions = [row.action for row in db.scalars(select(ActionLog)).all()]
    finally:
        db.close()
    assert "system.temp_high" in actions
    assert "system.temp_high_cleared" in actions


def test_silence_requires_auth_once_account_exists(client):
    _onboard(client)
    client.cookies.clear()
    assert client.post("/api/alerts/silence").status_code == 401


def test_silence_stops_buzzer_but_keeps_alert_visible(client, monkeypatch):
    from app import main

    _onboard(client)  # auto-logged in
    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(TOO_HOT))
    _poll(times=2)
    assert main.gpio_service.get_state()["alarm"] is True

    r = client.post("/api/alerts/silence")
    assert r.status_code == 200
    state = r.json()["alerts"]
    assert state["silenced"] is True
    assert state["alarm_on"] is False
    assert [a["type"] for a in state["active"]] == ["temp_high"]
    assert main.gpio_service.get_state()["alarm"] is False

    # Silence is sticky — the next out-of-range poll does not re-assert the buzzer.
    _poll()
    assert main.gpio_service.get_state()["alarm"] is False


def test_hardware_send_alarm_actions(client):
    r = client.post("/hardware/send", json={"action": "alarm_on"})
    assert r.status_code == 200 and r.json()["alarm"] is True

    r = client.post("/hardware/send", json={"action": "alarm_off"})
    assert r.status_code == 200 and r.json()["alarm"] is False

    r = client.post("/hardware/send", json={"action": "alarm_test"})
    assert r.status_code == 200 and r.json()["ok"] is True


# ── Sensor integrity reaches the buzzer, not just the control loop ───────────

# Real (non-mock) readings — integrity checks only apply to these.
REAL_STEADY = {"ok": True, "temperature_c": 37.5, "humidity_pct": 55.0}
ALARM_ON = {"target_temp_c": "37.5", "target_humidity_pct": "55", "alarm_enabled": "true"}


def test_latched_sensor_raises_alarm():
    """A frozen-but-'ok' sensor must alarm — the farmer has to hear about it."""
    import time as _t

    from app import main

    real = _t.time
    try:
        _t.time = lambda: 1000.0
        main.alert_service.record_reading(REAL_STEADY, ALARM_ON)   # first sighting
        _t.time = lambda: 1000.0 + 901                             # identical 15 min on
        events = main.alert_service.record_reading(REAL_STEADY, ALARM_ON)
    finally:
        _t.time = real

    assert any(e.get("type") == "sensor_faulty" for e in events)


def test_implausible_reading_raises_alarm():
    from app import main

    events = main.alert_service.record_reading(
        {"ok": True, "temperature_c": -40.0, "humidity_pct": 50.0}, ALARM_ON,
    )
    assert any(e.get("type") == "sensor_faulty" for e in events)


def test_mock_reading_never_raises_integrity_alarm():
    """Mock readings are constant by design — they must never alarm."""
    from app import main

    for _ in range(50):
        events = main.alert_service.record_reading(NORMAL, ALARM_ON)
        assert not any(e.get("type") == "sensor_faulty" for e in events)
