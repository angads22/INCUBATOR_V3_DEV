"""Farmer-proof setup: fewer steps, fail-fast checks, and Wi-Fi recovery mode.

The wizard is 6 steps (password lives inline on the Wi-Fi step, shown only
when needed). When a join fails, the device re-raises its setup AP and the
wizard reopens in RECOVERY mode: it says what went wrong, preselects the
failed network, and asks only for the Wi-Fi — name and login stay saved.
"""

import pytest

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


@pytest.fixture(autouse=True)
def _fast(client):
    from app import main
    main.onboarding_service._complete_grace_seconds = 0.0
    main.onboarding_service._hotspot_active = False
    yield
    main.onboarding_service._hotspot_active = False


def _fail_join(client, ssid="HomeNet"):
    """Onboard with an account, then simulate the join failing."""
    from app import main
    from app.routes.web import _complete_with_recovery

    r = client.post("/onboarding/complete", json={
        "ssid": ssid, "wifi_password": "wrongpass99", "device_name": "Coop 1",
        "create_account": True, **ACCOUNT,
    })
    assert r.json()["ok"]
    orig = main.wifi_service.connect_client
    main.wifi_service.connect_client = lambda s, p: False
    try:
        _complete_with_recovery(ssid, "wrongpass99")
    finally:
        main.wifi_service.connect_client = orig


# ── Wizard structure: 6 steps, inline password ───────────────────────────────

def test_wizard_is_six_steps_with_inline_password(client):
    html = client.get("/onboarding").text
    assert 'data-step="5"' in html and 'data-step="6"' not in html
    # Password input lives INSIDE the Wi-Fi step card, revealed on demand.
    assert "wifiPasswordWrap" in html
    assert "Step 1 of 6" in html


def test_fail_fast_checks_exist_in_wizard_js():
    js = open("app/static/js/onboarding.js").read()
    # Doomed joins are refused on-screen, not discovered minutes later.
    assert "at least 8 characters" in js          # WPA2 minimum
    assert "SSID_OK_RE" in js                     # device-side SSID rule mirrored
    assert "needs its Wi-Fi password" in js       # locked network, empty password


def test_finish_card_explains_the_safety_net(client):
    html = client.get("/onboarding").text
    assert "Safety net" in html
    assert "setup network back on" in html


# ── Recovery mode ────────────────────────────────────────────────────────────

def test_failed_join_switches_wizard_to_recovery_mode(client):
    _fail_join(client, "HomeNet")
    html = client.get("/onboarding").text
    assert "Let's fix the Wi-Fi" in html
    assert "HomeNet" in html                       # failed network named
    assert 'data-retry="1"' in html
    assert 'data-device-name="Coop 1"' in html     # name preserved for resubmit


def test_fresh_device_is_not_in_recovery_mode(client):
    html = client.get("/onboarding").text
    assert 'data-retry=""' in html
    assert "Let's fix the Wi-Fi" not in html


def test_recovery_resubmit_keeps_account_and_name(client):
    _fail_join(client, "HomeNet")
    # The recovery wizard resubmits with create_account=false + saved name.
    r = client.post("/onboarding/complete", json={
        "ssid": "HomeNet", "wifi_password": "rightpass99",
        "device_name": "Coop 1", "create_account": False,
    })
    assert r.json()["ok"]
    from app.database import get_db
    from sqlalchemy import select
    from app.models import DeviceConfig, User
    db = next(get_db())
    try:
        users = db.scalars(select(User)).all()
        cfg = db.scalar(select(DeviceConfig).limit(1))
        assert len(users) == 1 and users[0].username == "farmer"   # no duplicate
        assert cfg.device_name == "Coop 1" and cfg.wifi_ssid == "HomeNet"
    finally:
        db.close()


def test_recovery_mode_clears_after_successful_join(client):
    _fail_join(client, "HomeNet")
    client.post("/onboarding/complete", json={
        "ssid": "HomeNet", "wifi_password": "rightpass99",
        "device_name": "Coop 1", "create_account": False,
    })
    html = client.get("/onboarding").text
    assert "Let's fix the Wi-Fi" not in html       # back to the normal wizard
