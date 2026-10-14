"""main.py wiring: lifespan, /health additions, cached static + gzip, poller
watchdog pings, retention boot delay, button -> feedback / factory reset, and
the daemon-mode heater-current check."""

import json
import sys
import time
import types

import pytest


# ── /health ──────────────────────────────────────────────────────────────────

def test_health_reports_board_and_backends(client):
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["gpio_backend"] == "mock" and body["gpio_mock"] is True
    assert body["gpio_mock_fallback"] is False
    assert body["sensor_backend"] == "mock"
    assert body["camera_backend"] == "mock"
    assert body["control_daemon_enabled"] is False
    assert "model" in body["board"] and "arch" in body["board"]


def test_health_gpio_mock_true_when_backend_fell_back(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings.__class__, "gpio_mock", property(lambda s: False), raising=False)
    monkeypatch.setattr(main.gpio_service, "mock_fallback", True)
    body = client.get("/health").json()
    assert body["gpio_mock"] is True and body["gpio_mock_fallback"] is True


# ── static caching + gzip ────────────────────────────────────────────────────

def test_static_assets_are_cacheable(client):
    r = client.get("/static/css/tokens.css?v=1.99")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    r = client.get("/static/css/tokens.css")
    assert r.headers["cache-control"] == "public, max-age=3600"


def test_html_is_gzipped_when_accepted(client):
    r = client.get("/help", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"


# ── lifespan ─────────────────────────────────────────────────────────────────

def test_app_uses_lifespan_not_on_event():
    from app import main

    assert main.app.router.lifespan_context is not None
    assert not main.app.router.on_startup and not main.app.router.on_shutdown
    # Startup played the boot feedback first (other tests may have emitted
    # events since, and the history is a rolling window).
    assert main.feedback_service.first_event == "boot"


def test_startup_created_indexes(client):
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}
    assert {"ix_sensor_logs_captured_at", "ix_action_logs_created_at"} <= names


def test_root_logging_configured():
    import logging

    assert logging.getLogger().handlers, "root logger must have a handler so app INFO reaches journald"


# ── poller ───────────────────────────────────────────────────────────────────

def test_retention_sweep_is_not_on_the_boot_path():
    from app import main

    poller = main._SensorPoller(30)
    first_due = poller._last_retention_sweep + poller.RETENTION_PERIOD
    assert poller.RETENTION_BOOT_DELAY - 5 <= first_due - time.time() <= poller.RETENTION_BOOT_DELAY + 5


def test_poller_pings_watchdog_before_and_after_each_poll(monkeypatch):
    from app import main
    from app import sd_notify

    events = []
    monkeypatch.setattr(sd_notify, "sd_watchdog_ping", lambda: events.append("ping") or True)
    poller = main._SensorPoller(0)
    monkeypatch.setattr(poller, "_poll", lambda: events.append("poll"))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    class _Stop:
        def __init__(self):
            self.n = 0

        def wait(self, _t):
            self.n += 1
            return self.n > 2                      # two iterations then stop

        def set(self):
            pass

    poller._stop_flag = _Stop()
    poller.run()
    assert events == ["ping", "poll", "ping", "ping", "poll", "ping"]


def test_daemon_mode_heater_check_uses_daemon_heater_state(client, monkeypatch, tmp_path):
    """heater_on from the daemon state must reach evaluate_heater (it never did)."""
    from app import main

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ts": time.time(), "online": True, "temperature_c": 37.5,
                                 "humidity_pct": 55.0, "heater_on": True, "fan_on": False, "sensor_fault": ""}))
    monkeypatch.setattr(main.settings.__class__, "control_daemon_enabled", property(lambda s: True), raising=False)
    monkeypatch.setattr(main.settings.__class__, "heater_current_sense_enabled", property(lambda s: True), raising=False)
    monkeypatch.setattr(main.settings.__class__, "control_state_path", property(lambda s: str(state)), raising=False)
    monkeypatch.setattr(main.gpio_service, "read_heater_current", lambda: {"ok": True, "present": True, "amps": 2.5})
    seen = []
    real = main.alert_service.evaluate_heater
    monkeypatch.setattr(main.alert_service, "evaluate_heater",
                        lambda commanded, present, amps, s: seen.append((commanded, present, amps)) or real(commanded, present, amps, s))
    main.alert_service.reset()
    main._sensor_poller._poll()
    assert seen == [(True, True, 2.5)]              # commanded ON + current flowing: no fault
    assert [a["type"] for a in main.alert_service.alert_state()["active"]] == []


def test_daemon_mode_stale_state_skips_heater_check(client, monkeypatch, tmp_path):
    from app import main

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ts": 1.0, "online": True, "temperature_c": 37.5, "humidity_pct": 55.0, "heater_on": True}))
    monkeypatch.setattr(main.settings.__class__, "control_daemon_enabled", property(lambda s: True), raising=False)
    monkeypatch.setattr(main.settings.__class__, "heater_current_sense_enabled", property(lambda s: True), raising=False)
    monkeypatch.setattr(main.settings.__class__, "control_state_path", property(lambda s: str(state)), raising=False)
    monkeypatch.setattr(main.gpio_service, "read_heater_current", lambda: {"ok": True, "present": True, "amps": 2.5})
    seen = []
    monkeypatch.setattr(main.alert_service, "evaluate_heater", lambda c, p, a, s: seen.append((c, p, a)) or [])
    main.alert_service.reset()
    main._sensor_poller._poll()
    assert seen == [(False, False, None)]           # unknown commanded state -> check is a no-op


# ── button -> feedback / factory reset ───────────────────────────────────────

def test_button_hold_plays_ack_and_starts_hotspot(client, monkeypatch):
    from app import main

    calls = []
    monkeypatch.setattr(main.onboarding_service, "start_manual_hotspot", lambda did: calls.append(did) or {"ok": True})
    monkeypatch.setattr(main.feedback_service, "event", lambda name: calls.append(name))
    main._on_button_held("button_hold")
    assert calls[0] == "button_ack" and len(calls) == 2


def test_button_hold_failure_plays_failed(client, monkeypatch):
    from app import main

    calls = []
    monkeypatch.setattr(main.onboarding_service, "start_manual_hotspot", lambda did: {"ok": False})
    monkeypatch.setattr(main.feedback_service, "event", lambda name: calls.append(name))
    main._on_button_held("button_hold")
    assert calls == ["button_ack", "failed"]


def test_factory_reset_armed_progress_plays_warning(client, monkeypatch):
    from app import main

    calls = []
    monkeypatch.setattr(main.feedback_service, "event", lambda name: calls.append(name))
    main._on_button_progress("factory_reset_armed")
    main._on_button_progress("something_else")
    assert calls == ["factory_reset_armed"]


def test_factory_reset_calls_device_service_with_spec_signature(client, monkeypatch):
    from app import main

    received = {}

    def fake_factory_reset(db, *, wifi_service, onboarding_service, setup_mode_service, device_id):
        received.update(wifi=wifi_service, onboarding=onboarding_service, setup=setup_mode_service, device_id=device_id)
        return {"ok": True, "wiped": ["users"]}

    fake = types.ModuleType("app.services.device_service")
    fake.factory_reset = fake_factory_reset
    monkeypatch.setitem(sys.modules, "app.services.device_service", fake)
    import app.services as services_pkg

    monkeypatch.setattr(services_pkg, "device_service", fake, raising=False)
    calls = []
    monkeypatch.setattr(main.feedback_service, "event", lambda name: calls.append(name))
    main._on_button_held("factory_reset")
    assert received["wifi"] is main.wifi_service and received["onboarding"] is main.onboarding_service
    assert received["setup"] is main.setup_mode_service and received["device_id"]
    assert calls == ["factory_reset"]


def test_factory_reset_without_device_service_logs_and_fails_safe(client, monkeypatch):
    from app import main

    monkeypatch.setitem(sys.modules, "app.services.device_service", None)   # ImportError on import
    import app.services as services_pkg

    monkeypatch.delattr(services_pkg, "device_service", raising=False)
    calls = []
    monkeypatch.setattr(main.feedback_service, "event", lambda name: calls.append(name))
    main._on_button_held("factory_reset")                  # must not raise
    assert calls == ["failed"]


def test_button_service_wired_to_gpio_reader_and_reset_threshold():
    from app import main

    assert main.button_service._read_pressed_fn == main.gpio_service.read_button
    assert main.button_service.factory_reset_hold_seconds == main.settings.factory_reset_hold_seconds
    assert main.button_service.hold_seconds == main.settings.setup_button_hold_seconds


def test_accepted_kwargs_filters_by_signature():
    from app.main import _accepted_kwargs

    def f(a, b=1):
        return None

    def g(**kw):
        return None

    assert _accepted_kwargs(f, b=2, zzz=3) == {"b": 2}
    assert _accepted_kwargs(g, b=2, zzz=3) == {"b": 2, "zzz": 3}


def test_execute_hardware_action_unchanged_for_web_owned_outputs(client):
    from app import main

    assert main.execute_hardware_action("set_candle", "on")["ok"] is True
    assert main.execute_hardware_action("nope")["error"] == "unknown_action"
