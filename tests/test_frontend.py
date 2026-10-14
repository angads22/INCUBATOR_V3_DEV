"""Web front-end: pages render with the new navigation + PWA bits, static
assets are served, CachedStaticFiles sets Cache-Control and the gzip
middleware compresses text.

The CachedStaticFiles / add_web_middleware tests build their own tiny
Starlette app, so they do not depend on how app.main wires them.
"""

import io
import json
import pathlib

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from app.static_files import CACHE_IMMUTABLE, CACHE_SHORT, CachedStaticFiles, add_web_middleware

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


def _onboard(client, create_account=True):
    return client.post("/onboarding/complete", json={
        "ssid": "HomeNet",
        "wifi_password": "",
        "device_name": "Coop 1",
        "create_account": create_account,
        **ACCOUNT,
    })


def _render(name, **context):
    """Render a template directly so new context variables can be exercised
    before the back-end sends them."""
    from app.routes.web import templates

    return templates.get_template(name).render(**context)


# ── Pages ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/status", "/help", "/onboarding", "/login", "/hardware"])
def test_pages_render_with_nav_tabbar_and_manifest(client, path):
    r = client.get(path)
    assert r.status_code == 200
    html = r.text
    # PWA head
    assert 'rel="manifest"' in html and "/static/manifest.webmanifest" in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="theme-color"' in html
    assert 'name="apple-mobile-web-app-capable"' in html
    # Top nav + mobile tab bar carry the four primary links
    assert 'class="tabbar"' in html
    for href in ("/", "/hardware", "/status", "/help"):
        assert f'href="{href}"' in html
    # Profile menu entries
    assert 'href="/testing"' in html and 'href="/onboarding"' in html and 'href="/settings"' in html
    # Versioned asset URLs (long-lived cache)
    assert "/static/css/tokens.css?v=" in html and "/static/js/base.js?v=" in html


def test_settings_page_has_device_card_when_logged_in(client):
    _onboard(client)  # creates the owner and logs the client in
    r = client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert 'id="device"' in html
    assert "Restart setup mode" in html and "Factory reset" in html
    assert 'id="factoryResetInput"' in html and 'id="deviceInfoChips"' in html


def test_help_page_documents_led_buzzer_and_setup_button(client):
    html = client.get("/help").text
    assert 'id="ledTable"' in html
    for text in (
        "Power on, app ready", "Setup mode (own Wi-Fi up)", "double-blink", "slow heartbeat",
        "SOS", "Factory reset armed", "short-short-long",
    ):
        assert text in html
    assert "Hold 4 seconds" in html and "Hold 15 seconds" in html
    assert "Updates are automatic" in html
    assert ".local:8000" in html


def test_update_popup_no_longer_tells_users_to_reflash(client):
    html = client.get("/").text
    assert "updates itself automatically" in html
    assert "git pull" not in html


def test_dashboard_uses_appliance_defaults_for_a_fresh_device(client):
    html = client.get("/").text
    assert "Pi Zero Incubator" not in html
    assert "PI-UNCLAIMED" not in html
    assert "Device ID: unclaimed" in html
    assert 'class="card home-metrics"' in html


def test_dashboard_welcome_card_only_once_setup_is_complete(client):
    assert 'id="welcomeCard"' not in client.get("/").text
    _onboard(client)
    html = client.get("/").text
    assert 'id="welcomeCard"' in html and "Welcome to your incubator" in html


# ── Onboarding wizard states ─────────────────────────────────────────────────

def test_onboarding_defaults_to_setup_mode_without_new_context(client):
    """Today's back-end sends no hotspot_active/candidate_urls — the wizard
    must still render, in setup mode, with safe defaults."""
    html = client.get("/onboarding").text
    assert 'data-in-setup="1"' in html
    assert 'class="wizard-step active" data-step="1"' in html
    assert 'data-step="3"' in html and 'data-step="4"' not in html
    assert 'id="wizardCandidates">[]</script>' in html
    assert 'data-ping-path="/api/setup/ping"' in html
    assert "Step 1 of 2" in html and "Finish up" in html
    assert 'id="connectSteps"' in html and 'id="finishFound"' in html and 'id="finishFailed"' in html


def _onboarding_ctx(**overrides):
    ctx = dict(
        version="x", ap_ip="10.42.0.1", ap_password="", ap_ssid_prefix="Incubator",
        ap_ssid="Incubator-AB12", retry_mode=False, retry_ssid="", device_name="Coop 1",
        hotspot_active=False, setup_mode=False, has_account=True, hostname="incubator-ab12",
        candidate_urls=["http://incubator-ab12.local:8000", "http://incubator-ab12:8000"],
        ping_path="/api/setup/ping",
    )
    ctx.update(overrides)
    return ctx


def test_onboarding_not_in_setup_mode_shows_change_wifi_card():
    html = _render("onboarding.html", **_onboarding_ctx())
    assert 'data-in-setup=""' in html
    assert 'class="wizard-step active" data-step="0"' in html
    assert "Change Wi-Fi / run setup again" in html
    assert 'id="startSetupModeBtn"' in html
    assert "http://incubator-ab12.local:8000" in html  # candidate list embedded as JSON
    assert 'data-hostname="incubator-ab12"' in html


def test_onboarding_hotspot_active_renders_wizard():
    html = _render("onboarding.html", **_onboarding_ctx(hotspot_active=True))
    assert 'data-in-setup="1"' in html
    assert 'class="wizard-step active" data-step="1"' in html


def test_onboarding_retry_mode_wins_even_when_hotspot_flag_is_false():
    html = _render("onboarding.html", **_onboarding_ctx(retry_mode=True, retry_ssid="HomeNet"))
    assert 'data-in-setup="1"' in html and 'data-retry="1"' in html
    assert "Let's fix the Wi-Fi" in html and "HomeNet" in html


def test_onboarding_js_probes_cross_origin_and_detects_hotspot_return():
    js = (ROOT / "app/static/js/onboarding.js").read_text()
    assert "mode: 'cors'" in js and "cache: 'no-store'" in js
    assert "AbortController" in js
    assert "/api/setup/ping" in js
    assert "hotspot_active" in js
    assert "180000" in js            # manual fallback after 180 s
    assert "25000" in js             # AP-return grace window
    assert "window.open(appUrl" in js


def test_status_page_shows_board_and_backend_chips_when_provided():
    health = dict(
        hardware="online", sensors="online", alarms="none", alarms_tone="info", gpio_mock=False,
        setup_mode="off", vision_backend="mock", camera_backend="mock", dht_pin="4",
        board={"model": "Raspberry Pi Zero 2 W"}, gpio_backend="gpiod", sensor_backend="sht3x",
    )
    html = _render("status.html", version="x", health=health, environment=None, hw_state={}, alerts={})
    assert "Board: Raspberry Pi Zero 2 W" in html
    assert "GPIO driver: gpiod" in html and "Sensor: sht3x" in html


# ── JS hygiene ───────────────────────────────────────────────────────────────

def test_ssids_are_rendered_as_text_not_html():
    for name in ("onboarding.js", "network.js"):
        js = (ROOT / "app/static/js" / name).read_text()
        assert "innerHTML" not in js, name
        assert "createTextNode" in js, name


def test_base_js_marks_active_link_in_both_bars():
    js = (ROOT / "app/static/js/base.js").read_text()
    assert ".nav-links a, .tabbar a" in js


def test_settings_js_uses_device_endpoints_via_helpers():
    js = (ROOT / "app/static/js/settings.js").read_text()
    assert "apiGet('/api/device/info')" in js
    assert "apiPost('/api/device/factory-reset', { confirm: 'RESET' })" in js
    assert "404" in js  # graceful message when the endpoints do not exist yet


# ── Static assets ────────────────────────────────────────────────────────────

def test_manifest_and_icons_are_served(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]
    manifest = json.loads(r.text)
    assert manifest["name"] == "Life Loop Incubator"
    assert manifest["short_name"] == "Incubator"
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert manifest["background_color"] == "#efe7d6" and manifest["theme_color"] == "#c2702a"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert any("maskable" in icon.get("purpose", "") for icon in manifest["icons"])

    from PIL import Image

    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        rr = client.get(f"/static/icons/{name}")
        assert rr.status_code == 200
        assert rr.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(rr.content) < 40_000, name
        assert Image.open(io.BytesIO(rr.content)).size == (size, size)

    svg = client.get("/static/icons/icon.svg")
    assert svg.status_code == 200 and "<svg" in svg.text and "#efe7d6" in svg.text


def _static_app(tmp_path):
    (tmp_path / "a.css").write_text("body{}" * 300)
    app = Starlette(routes=[
        Route("/big", lambda r: PlainTextResponse("x" * 5000)),
        Route("/small", lambda r: PlainTextResponse("x" * 100)),
        Mount("/static", CachedStaticFiles(directory=str(tmp_path)), name="static"),
    ])
    return app


def test_cached_static_files_sets_cache_control(tmp_path):
    app = _static_app(tmp_path)
    with TestClient(app) as c:
        plain = c.get("/static/a.css")
        assert plain.status_code == 200
        assert plain.headers["cache-control"] == CACHE_SHORT
        versioned = c.get("/static/a.css?v=2.00")
        assert versioned.headers["cache-control"] == CACHE_IMMUTABLE
        # An empty ?v= is not a real version — do not cache it for a year.
        assert c.get("/static/a.css?v=").headers["cache-control"] == CACHE_SHORT
        # Conditional requests keep the header too.
        not_modified = c.get("/static/a.css", headers={"If-None-Match": plain.headers["etag"]})
        assert not_modified.status_code == 304
        assert not_modified.headers["cache-control"] == CACHE_SHORT
        assert c.get("/static/missing.css").status_code == 404


def test_add_web_middleware_gzips_large_text_responses(tmp_path):
    app = add_web_middleware(_static_app(tmp_path))
    with TestClient(app) as c:
        big = c.get("/big", headers={"Accept-Encoding": "gzip"})
        assert big.status_code == 200
        assert big.headers.get("content-encoding") == "gzip"
        assert big.text == "x" * 5000  # client transparently decodes
        # Below the minimum size: left alone.
        small = c.get("/small", headers={"Accept-Encoding": "gzip"})
        assert small.headers.get("content-encoding") is None
        # No gzip support announced: left alone.
        identity = c.get("/big", headers={"Accept-Encoding": "identity"})
        assert identity.headers.get("content-encoding") is None
