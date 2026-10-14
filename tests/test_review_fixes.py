"""Regression tests for the adversarial-review findings on the v2.00 change."""

import json
from types import SimpleNamespace

from app.control.bridge import daemon_reading
from app.control.loop import ControlDaemon, build_daemon
from app.services.alert_service import AlertService
from app.services.gpio_service import DAEMON_OUTPUTS, GPIOService


def _svc(**kw):
    base = dict(
        dht_pin="4", heater_pin="17", fan_pin="27", turner_pin="22", turner_dir_pin="23",
        candle_pin="24", alarm_pin="25", lock_pin="12", door_pin="13", setup_button_pin="18",
        relay_active_low=True, mock=True,
    )
    base.update(kw)
    return GPIOService(**base)


# ── The control daemon must never request the setup-button line ─────────────

def test_daemon_shaped_service_does_not_request_the_button_line():
    svc = _svc(owned_outputs=DAEMON_OUTPUTS, request_button=False)
    svc.setup()
    assert svc._button_line is None
    assert svc.read_button() is False
    requested = {line.pin.key for line in svc._backend.lines.values()}
    assert "18" not in requested and not any(k.endswith(":18") for k in requested)


def test_web_shaped_service_still_owns_the_button_line():
    svc = _svc()
    svc.setup()
    assert svc._button_line is not None


def test_build_daemon_leaves_the_button_to_the_web_app():
    daemon = build_daemon()
    try:
        assert daemon._gpio._button_line is None
    finally:
        daemon._gpio.cleanup()


# ── Temperature-only sensors are valid readings ──────────────────────────────

from app.hal.sensors import Sensor


class _TempOnlySensor(Sensor):
    name = "bmp280"

    def read(self):
        return {"ok": True, "temperature_c": 37.2, "humidity_pct": None, "mock": False, "sensor": "bmp280"}

    def close(self):
        pass


def test_temperature_only_reading_is_accepted():
    svc = _svc(sensor=_TempOnlySensor())
    svc.setup()
    r = svc.read_temperature_humidity()
    assert r["ok"] is True and r["temperature_c"] == 37.2 and r["humidity_pct"] is None


# ── A relay write that did not land is published and alarmed ────────────────

class _BrokenHeaterGpio:
    def __init__(self):
        self.calls = []

    def read_temperature_humidity(self):
        return {"ok": True, "temperature_c": 30.0, "humidity_pct": 50.0, "mock": False}

    def set_heater(self, on):
        self.calls.append(("heater", on))
        return {"ok": False, "error": "not_owned"}

    def set_fan(self, on):
        return {"ok": True, "fan": on}

    def move_turner(self, *a, **k):
        return {"ok": True}


def test_failed_relay_write_is_reported_as_output_fault(tmp_path):
    state = tmp_path / "state.json"
    gpio = _BrokenHeaterGpio()
    daemon = ControlDaemon(
        gpio, settings_provider=lambda: {"target_temp_c": "37.5", "heater_enabled": "true", "turner_enabled": "false"},
        state_path=str(state), command_path=str(tmp_path / "cmd.jsonl"),
    )
    out = daemon.loop_once(now_ts=1000.0)
    assert out["output_fault"] == "heater"
    reading = daemon_reading(str(state), now=1001.0)
    assert reading["output_fault"] == "heater"

    alerts = AlertService(gpio=_svc())
    events = alerts.record_reading(reading, {"alarm_enabled": "false"})
    assert any(e["type"] == "output_fault" and e["kind"] == "raised" for e in events)
    assert "output_fault" in {a["type"] for a in alerts.alert_state()["active"]}
    # Clears again once the write lands.
    reading["output_fault"] = ""
    events = alerts.record_reading(reading, {"alarm_enabled": "false"})
    assert any(e["type"] == "output_fault_cleared" and e["kind"] == "cleared" for e in events)


# ── Manual heater ON without a reading adopts the first good baseline ───────

class _FlakyThenGoodGpio(_BrokenHeaterGpio):
    def __init__(self):
        super().__init__()
        self.readings = [{"ok": False}, {"ok": True, "temperature_c": 30.0, "humidity_pct": 50.0, "mock": False}]

    def read_temperature_humidity(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def set_heater(self, on):
        return {"ok": True, "heater": on}


def test_manual_heater_on_without_reading_gets_a_baseline_later(tmp_path):
    daemon = ControlDaemon(
        _FlakyThenGoodGpio(), settings_provider=lambda: {"heater_enabled": "true", "turner_enabled": "false"},
        state_path=str(tmp_path / "s.json"), command_path=str(tmp_path / "c.jsonl"),
    )
    daemon.loop_once(now_ts=1000.0)             # sensor failed on this iteration
    daemon._apply_command("heater", True)       # manual ON with no temperature known
    assert daemon._heat_start_ts is not None and daemon._heat_start_temp is None
    daemon.loop_once(now_ts=1010.0)             # first good reading
    assert daemon._heat_start_temp == 30.0      # baseline adopted → runaway guard armed


# ── --safe-outputs never undoes the Pi firmware levels ──────────────────────

def test_safe_outputs_is_skipped_on_a_raspberry_pi(monkeypatch):
    from app.hal import __main__ as hal_cli
    from app.hal import board as board_mod

    monkeypatch.setattr(board_mod, "detect_board", lambda root="/": SimpleNamespace(is_raspberry_pi=True))
    report = hal_cli.safe_outputs(SimpleNamespace(gpio_backend="mock", gpio_mock=True))
    assert report["skipped"] == "raspberry-pi" and report["lines"] == {}
    forced = hal_cli.safe_outputs(SimpleNamespace(
        gpio_backend="mock", gpio_mock=True, gpio_relay_active_low=True,
        gpio_heater_pin="17", gpio_fan_pin="27", gpio_lock_pin="12", gpio_door_pin="13",
        gpio_turner_pin="22", gpio_turner_dir_pin="23", gpio_candle_pin="24", gpio_alarm_pin="25",
    ), force=True)
    assert forced["ok"] is True and len(forced["lines"]) == 8


# ── The watchdog plays "connected" once when a normal boot joins Wi-Fi ──────

class _OnlineWiFi:
    def __init__(self):
        self.ssid = None

    def get_connected_ssid(self):
        return self.ssid

    def hotspot_status(self):
        return {"active": False}

    def is_hotspot_up(self, *a):
        return False


class _Setup:
    def is_setup_mode(self):
        return False

    def reason(self):
        return "normal"

    def enter_setup_mode(self, *a):
        pass

    def exit_setup_mode(self):
        pass


def test_watchdog_emits_connected_exactly_once_on_link_up(client):
    from app.database import get_db
    from app.models import DeviceConfig
    from app.services.onboarding_service import OnboardingService

    events = []
    wifi = _OnlineWiFi()
    svc = OnboardingService(
        wifi_service=wifi, setup_mode_service=_Setup(), ap_ssid_prefix="Incubator", ap_password="",
        ap_ip="10.42.0.1", ap_password_resolver=lambda: "", on_event=events.append,
    )
    db = next(get_db())
    try:
        db.add(DeviceConfig(device_id="PI-ABCD1234", claimed=True, wifi_ssid="HomeNet"))
        db.commit()
        svc.boot(db, "PI-ABCD1234")            # configured, but Wi-Fi not associated yet
        assert "connected" not in events
        wifi.ssid = "HomeNet"                   # NetworkManager joined a few seconds later
        svc.ensure_reachable(db)
        svc.ensure_reachable(db)
        assert events.count("connected") == 1
        wifi.ssid = None                        # link drops …
        svc.ensure_reachable(db)
        wifi.ssid = "HomeNet"                   # … and comes back: one more chime
        svc.ensure_reachable(db)
        assert events.count("connected") == 2
    finally:
        db.close()


# ── Changing Wi-Fi through the wizard keeps the previous network as a fallback ─

def test_wizard_change_wifi_uses_switch_network_and_keeps_previous_on_failure(client, monkeypatch):
    from app.database import get_db
    from app.models import DeviceConfig
    from app.routes import web

    calls = []

    class _Onboarding:
        def switch_network(self, ssid, password, *, previous_ssid=None):
            calls.append(("switch", ssid, previous_ssid))
            return False  # the new network could not be joined

        def complete(self, ssid, password):
            calls.append(("complete", ssid, None))
            return False

    class _WiFi:
        def get_connected_ssid(self):
            return "OldNet"  # the device fell back onto its previous network

    monkeypatch.setattr(web, "_onboarding_service", _Onboarding())
    monkeypatch.setattr(web, "_wifi_service", _WiFi())
    db = next(get_db())
    try:
        db.add(DeviceConfig(device_id="PI-ABCD1234", claimed=True, wifi_ssid="NewNet"))
        db.commit()
    finally:
        db.close()

    web._complete_with_recovery("NewNet", "typo-pass", "OldNet")
    assert calls == [("switch", "NewNet", "OldNet")]
    db = next(get_db())
    try:
        assert db.query(DeviceConfig).first().wifi_ssid == "OldNet"
    finally:
        db.close()

    # First-time setup (no previous network) still goes through complete().
    calls.clear()
    web._complete_with_recovery("NewNet", "pw", "")
    assert calls == [("complete", "NewNet", None)]


def test_wizard_hides_login_toggle_when_an_account_exists(client):
    r = client.post("/onboarding/complete", json={
        "ssid": "", "wifi_password": "", "device_name": "Coop", "create_account": True,
        "username": "owner", "email": "o@example.com", "password": "hunter2pass",
    })
    assert r.status_code == 200
    html = client.get("/onboarding").text
    assert 'id="createAccountCheck"' not in html
    assert "already has a login" in html


def test_manifest_does_not_lock_orientation(client):
    data = json.loads(client.get("/static/manifest.webmanifest").text)
    assert "orientation" not in data


def test_gzip_skips_the_mjpeg_stream():
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.static_files import add_web_middleware

    def stream(_request):
        return StreamingResponse((b"--frame\r\n" + b"x" * 4096 for _ in range(2)), media_type="multipart/x-mixed-replace; boundary=frame")

    app = Starlette(routes=[Route("/s", stream)])
    add_web_middleware(app)
    with TestClient(app) as c:
        r = c.get("/s", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") is None
