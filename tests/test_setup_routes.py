"""Setup-flow routes: who may run the wizard, device discovery ping, the
Device card (info + factory reset), relay toggles that never touch pins from
the web process, Wi-Fi switching from the AP, and honest page context."""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

import pytest

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


@pytest.fixture(autouse=True)
def _fast(client):
    from app import main
    main.onboarding_service._complete_grace_seconds = 0.0
    yield


def _main():
    from app import main
    return main


def _onboard(client, ssid="HomeNet", **extra):
    return client.post("/onboarding/complete", json={
        "ssid": ssid, "wifi_password": "", "device_name": "Coop 1",
        "create_account": True, **ACCOUNT, **extra,
    })


def _login(client):
    r = client.post("/api/login", json={"username": ACCOUNT["username"], "password": ACCOUNT["password"]})
    assert r.status_code == 200


def _db():
    from app.database import get_db
    return next(get_db())


# ── Onboarding access rule ───────────────────────────────────────────────────

def test_onboarding_routes_are_open_before_any_account_exists(client):
    assert client.get("/onboarding").status_code == 200
    assert client.get("/onboarding/wifi-scan").status_code == 200
    assert client.post("/onboarding/start").json()["ok"] is True


def test_onboarding_routes_require_login_from_the_lan_once_an_account_exists(client):
    _onboard(client)
    client.cookies.clear()
    main = _main()
    assert main.onboarding_service.is_hotspot_active() is False   # the join "succeeded" (mock)
    page = client.get("/onboarding", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.post("/onboarding/start").status_code == 401
    assert client.get("/onboarding/wifi-scan").status_code == 401
    r = client.post("/onboarding/complete", json={"ssid": "Evil", "device_name": "pwned", "create_account": False})
    assert r.status_code == 401
    # Nothing moved: setup mode stayed off, so /api/reset-password stays shut too.
    assert main.setup_mode_service.is_setup_mode() is False
    assert client.post("/api/reset-password", json={"identifier": "farmer", "new_password": "attacker123"}).status_code == 403
    db = _db()
    try:
        from sqlalchemy import select
        from app.models import DeviceConfig, User
        assert db.scalar(select(DeviceConfig).limit(1)).device_name == "Coop 1"
        assert [u.username for u in db.scalars(select(User)).all()] == ["farmer"]
    finally:
        db.close()


def test_onboarding_routes_allowed_with_a_valid_session(client):
    _onboard(client)                                    # keeps the owner cookie
    assert client.get("/onboarding").status_code == 200
    assert client.get("/onboarding/wifi-scan").status_code == 200
    assert client.post("/onboarding/complete", json={"ssid": "BarnNet", "device_name": "Coop 1", "create_account": False}).status_code == 200
    assert client.post("/onboarding/start").json()["ok"] is True
    assert _main().setup_mode_service.reason() == "manual_trigger"   # a browser click is not physical presence


def test_recovery_mode_on_the_active_ap_needs_no_login(client):
    _onboard(client)
    client.cookies.clear()
    main = _main()
    main.onboarding_service._hotspot_active = True      # phone is on the setup AP
    html = client.get("/onboarding").text
    assert 'data-in-setup="1"' in html
    assert client.get("/onboarding/wifi-scan").status_code == 200
    assert client.post("/onboarding/start").json()["ok"] is True
    r = client.post("/onboarding/complete", json={"ssid": "HomeNet", "wifi_password": "rightpass99", "device_name": "Coop 1", "create_account": False})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_recent_button_hold_opens_onboarding_for_ten_minutes_only(client):
    _onboard(client)
    client.cookies.clear()
    main = _main()
    main.setup_mode_service.enter_setup_mode("button_hold")
    assert client.get("/onboarding/wifi-scan").status_code == 200
    main.setup_mode_service._state["entered_at"] = time.time() - 601
    assert client.get("/onboarding/wifi-scan").status_code == 401
    main.setup_mode_service.enter_setup_mode("manual_trigger")   # web-triggered: not presence
    assert client.get("/onboarding/wifi-scan").status_code == 401


def test_create_account_is_refused_for_anonymous_callers_once_an_owner_exists(client):
    _onboard(client)
    client.cookies.clear()
    main = _main()
    main.onboarding_service._hotspot_active = True      # even standing on the AP
    r = client.post("/onboarding/complete", json={
        "ssid": "HomeNet", "device_name": "x", "create_account": True,
        "username": "evil", "email": "e@x.com", "password": "12345678",
    })
    assert r.status_code == 409
    db = _db()
    try:
        from sqlalchemy import select
        from app.models import User
        assert [u.username for u in db.scalars(select(User)).all()] == ["farmer"]
    finally:
        db.close()
    _login(client)                                      # a signed-in owner may add another account
    r = client.post("/onboarding/complete", json={
        "ssid": "HomeNet", "device_name": "Coop 1", "create_account": True,
        "username": "second", "email": "s@x.com", "password": "12345678",
    })
    assert r.status_code == 200


def test_wizard_requests_count_as_activity(client, monkeypatch):
    main = _main()
    seen = []
    monkeypatch.setattr(main.onboarding_service, "touch_activity", lambda: seen.append(1))
    client.get("/onboarding")
    client.get("/onboarding/wifi-scan")
    client.post("/onboarding/start")
    assert len(seen) == 3


# ── Vision / AI endpoints are gated like every other control API ─────────────

def test_vision_and_ai_endpoints_require_auth_once_an_account_exists(client):
    _onboard(client)
    client.cookies.clear()
    assert client.post("/api/vision/candle", json={}).status_code == 401
    assert client.post("/api/vision/analyze", json={"image_path": "/etc/passwd"}).status_code == 401
    assert client.post("/api/ai/chat", json={"question": "hi"}).status_code == 401
    assert client.post("/api/ai/explain-status", json={}).status_code == 401
    assert client.get("/api/camera/snapshot").status_code == 401
    assert client.get("/api/captures").status_code == 401
    assert client.get("/api/vision/growth").status_code == 401
    assert client.get("/api/testing/results").status_code == 401
    _login(client)
    assert client.post("/api/vision/candle", json={}).status_code == 200
    assert client.post("/api/ai/chat", json={"question": "hi"}).status_code == 200
    assert client.post("/api/ai/explain-status", json={}).status_code == 200


def test_vision_analyze_only_accepts_saved_captures(client):
    from app.config import settings

    assert client.post("/api/vision/analyze", json={"image_path": "/etc/passwd"}).status_code == 400
    folder = Path(settings.camera_image_dir)
    folder.mkdir(parents=True, exist_ok=True)
    img = folder / "analyze_me.jpg"
    img.write_bytes(b"\xff\xd8\xff\xd9")
    r = client.post("/api/vision/analyze", json={"image_path": str(img)})
    assert r.status_code == 200 and r.json()["endpoint"] == "vision.analyze"


# ── Discovery ping ───────────────────────────────────────────────────────────

def test_setup_ping_is_public_and_cors_enabled(client):
    _onboard(client)
    client.cookies.clear()
    r = client.get("/api/setup/ping")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert "GET" in r.headers["access-control-allow-methods"]
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    for key in ("ok", "device_id", "device_name", "hostname", "version", "ips", "setup_mode", "hotspot_active"):
        assert key in body
    assert body["ok"] is True and body["device_name"] == "Coop 1" and isinstance(body["ips"], list)
    assert body["hotspot_active"] is False and body["setup_mode"] is False
    pre = client.options("/api/setup/ping")
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"


def test_setup_ping_reports_hotspot_state(client):
    _main().onboarding_service._hotspot_active = True
    assert client.get("/api/setup/ping").json()["hotspot_active"] is True


# ── Device card ──────────────────────────────────────────────────────────────

def test_device_info_requires_auth_once_an_account_exists(client):
    body = client.get("/api/device/info").json()          # fresh device: open
    for key in ("board", "gpio_backend", "sensor_backend", "camera_backend", "wifi_iface", "hostname", "ips", "version"):
        assert key in body
    _onboard(client)
    client.cookies.clear()
    assert client.get("/api/device/info").status_code == 401
    _login(client)
    assert client.get("/api/device/info").status_code == 200


def test_set_runtime_services_accepts_gpio_and_camera_services(client):
    from app.routes import web
    main = _main()

    class Gpio:
        backend_name = "gpiod"
        sensor_name = "aht20"

    web.set_runtime_services(
        setup_mode_service=main.setup_mode_service, wifi_service=main.wifi_service,
        onboarding_service=main.onboarding_service, hardware_service=main.hardware_service,
        vision_service=main.vision_service, alert_service=main.alert_service,
        gpio_service=Gpio(), camera_service=main.camera_service,
    )
    try:
        body = client.get("/api/device/info").json()
        assert body["gpio_backend"] == "gpiod" and body["sensor_backend"] == "aht20"
        html = client.get("/status").text
        assert "GPIO driver: gpiod" in html and "Sensor: aht20" in html
    finally:
        web.set_runtime_services(
            setup_mode_service=main.setup_mode_service, wifi_service=main.wifi_service,
            onboarding_service=main.onboarding_service, hardware_service=main.hardware_service,
            vision_service=main.vision_service, alert_service=main.alert_service,
        )


def test_factory_reset_requires_auth_and_confirmation_then_wipes_and_raises_ap(client):
    from sqlalchemy import select
    from app.models import ActionLog, DeviceConfig, User

    _onboard(client)
    assert client.post("/api/device/factory-reset", json={"confirm": "nope"}).status_code == 400
    client.cookies.clear()
    assert client.post("/api/device/factory-reset", json={"confirm": "RESET"}).status_code == 401
    _login(client)
    r = client.post("/api/device/factory-reset", json={"confirm": "RESET"})
    assert r.status_code == 200 and r.json()["ok"] is True
    main = _main()
    db = _db()
    try:
        assert db.scalars(select(User)).all() == []
        cfg = db.scalar(select(DeviceConfig).limit(1))
        assert cfg.wifi_ssid is None and cfg.device_name is None and cfg.claimed is False and cfg.device_id
        actions = {a.action for a in db.scalars(select(ActionLog)).all()}
        assert {"system.factory_reset_requested", "system.factory_reset"} <= actions
    finally:
        db.close()
    assert main.onboarding_service.is_hotspot_active() is True        # back in setup mode on the AP
    assert main.setup_mode_service.reason() == "factory_reset"
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).status_code == 200  # no account → open again


# ── Settings toggles never drive relays from the web process ─────────────────

def test_settings_toggles_go_through_the_shared_dispatcher(client, monkeypatch):
    main = _main()
    dispatched = []
    monkeypatch.setattr(main, "execute_hardware_action", lambda action, value=None: dispatched.append(action) or {"ok": True})

    def never(*a, **k):
        raise AssertionError("relay pin written directly from the web process")

    monkeypatch.setattr(main.hardware_service, "set_heater", never)
    monkeypatch.setattr(main.hardware_service, "set_fan", never)
    assert client.post("/api/settings", json={"heater_enabled": True}).status_code == 200
    assert client.post("/api/settings", json={"fan_enabled": False}).status_code == 200
    assert dispatched == ["heater_on", "fan_off"]


def test_settings_toggles_in_daemon_mode_only_forward_off(client, monkeypatch):
    from app.routes import web
    main = _main()
    dispatched = []
    monkeypatch.setattr(main, "execute_hardware_action", lambda action, value=None: dispatched.append(action) or {"ok": True})
    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, control_daemon_enabled=True))
    assert client.post("/api/settings", json={"heater_enabled": True, "fan_enabled": True}).status_code == 200
    assert dispatched == []                                   # the daemon reads the flags itself
    assert client.post("/api/settings", json={"heater_enabled": False}).status_code == 200
    assert dispatched == ["heater_off"]                       # fail-safe direction is immediate
    db = _db()
    try:
        from app.settings_store import get_settings
        assert get_settings(db)["heater_enabled"] == "false"
    finally:
        db.close()


# ── Wi-Fi switching from Settings → Network ──────────────────────────────────

def test_network_connect_over_the_ap_switches_in_the_background(client, monkeypatch):
    _onboard(client)
    main = _main()
    main.onboarding_service._hotspot_active = True
    calls = []

    def fake_switch(ssid, password, *, previous_ssid=None):
        calls.append((ssid, password, previous_ssid))
        main.onboarding_service._hotspot_active = False
        return True

    monkeypatch.setattr(main.onboarding_service, "switch_network", fake_switch)
    r = client.post("/api/network/connect", json={"ssid": "BarnOffice", "password": "pass12345"})
    assert r.status_code == 200 and r.json()["switching"] is True
    assert calls == [("BarnOffice", "pass12345", "HomeNet")]
    assert client.get("/api/network/status").json()["configured_ssid"] == "BarnOffice"


def test_failed_background_switch_restores_previous_network_or_clears(client, monkeypatch):
    from sqlalchemy import select
    from app.models import ActionLog

    _onboard(client)
    main = _main()
    main.onboarding_service._hotspot_active = True
    monkeypatch.setattr(main.onboarding_service, "switch_network", lambda ssid, password, *, previous_ssid=None: False)
    monkeypatch.setattr(main.wifi_service, "get_connected_ssid", lambda: "HomeNet")   # fell back to the old one
    client.post("/api/network/connect", json={"ssid": "BarnOffice", "password": "pass12345"})
    assert client.get("/api/network/status").json()["configured_ssid"] == "HomeNet"
    monkeypatch.setattr(main.wifi_service, "get_connected_ssid", lambda: None)        # AP came back instead
    client.post("/api/network/connect", json={"ssid": "BarnOffice", "password": "pass12345"})
    assert client.get("/api/network/status").json()["configured_ssid"] is None
    db = _db()
    try:
        fails = db.scalars(select(ActionLog).where(ActionLog.action == "system.wifi_join_failed")).all()
        assert len(fails) == 2 and fails[-1].payload == "BarnOffice"
    finally:
        db.close()


def test_network_connect_on_the_lan_is_synchronous(client, monkeypatch):
    _onboard(client)
    main = _main()
    monkeypatch.setattr(main.onboarding_service, "switch_network", lambda ssid, password, *, previous_ssid=None: False)
    assert client.post("/api/network/connect", json={"ssid": "BarnOffice", "password": "pass12345"}).status_code == 502
    assert client.get("/api/network/status").json()["configured_ssid"] == "HomeNet"
    monkeypatch.setattr(main.onboarding_service, "switch_network", lambda ssid, password, *, previous_ssid=None: previous_ssid == "HomeNet")
    assert client.post("/api/network/connect", json={"ssid": "BarnOffice", "password": "pass12345"}).status_code == 200
    assert client.get("/api/network/status").json()["configured_ssid"] == "BarnOffice"


# ── Page context ─────────────────────────────────────────────────────────────

def test_onboarding_page_reveals_ap_password_only_on_the_ap(client):
    assert client.post("/api/settings/ap-password", json={"password": "barnpass1"}).status_code == 200
    html = client.get("/onboarding").text
    assert "barnpass1" not in html
    _main().onboarding_service._hotspot_active = True
    html = client.get("/onboarding").text
    assert "barnpass1" in html and 'data-in-setup="1"' in html


def test_onboarding_page_context_for_a_configured_device(client):
    _onboard(client)
    html = client.get("/onboarding").text
    assert 'data-in-setup=""' in html and "Change Wi-Fi / run setup again" in html
    assert 'data-has-account="1"' in html
    assert 'data-ping-path="/api/setup/ping"' in html
    assert ".local:8000" in html and 'data-hostname="' in html


def test_complete_response_carries_discovery_hints(client):
    body = _onboard(client).json()
    assert body["ping_path"] == "/api/setup/ping"
    assert body["candidate_urls"][0].startswith("http://") and ".local:8000" in body["candidate_urls"][0]
    assert "." not in body["hostname"]
    assert body["app_url"] == f"http://{body['hostname']}.local:8000"


def test_dashboard_uses_short_hostname_and_db_ap_password(client, monkeypatch):
    from app.services import onboarding_service as onb
    monkeypatch.setattr(onb.socket, "gethostname", lambda: "rock.farm.lan")
    client.post("/api/settings/ap-password", json={"password": "barnpass1"})
    _main().onboarding_service._hotspot_active = True
    html = client.get("/").text
    assert "http://rock.local:8000" in html and "rock.farm.lan.local" not in html
    assert "barnpass1" in html


def test_dashboard_quick_actions_use_controls_wording(client):
    html = client.get("/").text
    assert "Controls" in html and "Lock, door, turner, candling" in html
    assert "Manual GPIO test panel" not in html


def test_help_page_gets_factory_reset_hold_seconds(client):
    from app.config import settings
    html = client.get("/help").text
    assert f"Hold {int(settings.factory_reset_hold_seconds)} seconds" in html


def test_status_and_dashboard_show_the_daemons_output_state(client, monkeypatch, tmp_path):
    from app.routes import web

    state = tmp_path / "control-state.json"
    state.write_text(json.dumps({"ts": time.time(), "online": True, "heater_on": True, "fan_on": False, "turned": False, "last_turn_ts": 123.0}))
    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, control_daemon_enabled=True, control_state_path=str(state)))
    html = client.get("/status").text
    assert "Heater ON" in html and "Fan OFF" in html
    assert "Heater on" in client.get("/").text
    # Stale state (daemon not publishing) is shown as OFF — never a guess.
    state.write_text(json.dumps({"ts": time.time() - 1000, "online": True, "heater_on": True, "fan_on": True}))
    html = client.get("/status").text
    assert "Heater OFF" in html and "Fan OFF" in html
