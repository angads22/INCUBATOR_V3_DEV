"""Cached sensor state: /api/sensors/latest and the dashboard's honesty.

A failed DHT22 read must surface as an explicit offline state with the last
known reading — never as the target values masquerading as live data.
"""

import pytest

FAILED_READ = {"ok": False, "error": "DHT22 read failed", "temperature_c": None, "humidity_pct": None}


@pytest.fixture(autouse=True)
def _fresh_alert_state():
    from app import main

    main.alert_service.reset()
    yield
    main.alert_service.reset()


def _poll(times=1):
    from app import main

    for _ in range(times):
        main._sensor_poller._poll()


def test_sensors_latest_serves_cached_reading(client):
    _poll()
    body = client.get("/api/sensors/latest").json()
    assert body["ok"] is True
    assert body["online"] is True
    assert body["temperature_c"] is not None
    assert body["read_at"] is not None
    assert body["alerts"]["active"] == []


def test_sensor_failure_reports_offline_with_last_known(client, monkeypatch):
    from app import main

    _poll()  # cache one good reading
    good = client.get("/api/sensors/latest").json()

    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(FAILED_READ))
    _poll(times=3)

    body = client.get("/api/sensors/latest").json()
    assert body["online"] is False
    # Last known values survive — they are not replaced by targets or None.
    assert body["temperature_c"] == good["temperature_c"]
    assert any(a["type"] == "sensor_offline" for a in body["alerts"]["active"])


def test_dashboard_shows_offline_state_not_targets(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.gpio_service, "read_temperature_humidity", lambda: dict(FAILED_READ))
    main.alert_service.reset()
    _poll(times=3)

    r = client.get("/")
    assert r.status_code == 200
    assert "Sensor offline" in r.text
    # No reading was ever cached, so metrics render as em dashes.
    assert "—" in r.text
