"""Password reset gated on physical presence (setup mode)."""

import pytest

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}
NEW_PASSWORD = "brandnewpass1"


@pytest.fixture
def setup_mode(client):
    # The setup-mode state file lives in /tmp and survives across tests —
    # always leave it switched off.
    from app.main import setup_mode_service

    setup_mode_service.exit_setup_mode()
    yield setup_mode_service
    setup_mode_service.exit_setup_mode()


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


def test_reset_forbidden_outside_setup_mode(client, setup_mode):
    _onboard(client)
    client.cookies.clear()
    r = client.post("/api/reset-password", json={"identifier": ACCOUNT["username"], "new_password": NEW_PASSWORD})
    assert r.status_code == 403


def test_reset_page_shows_instructions_outside_setup_mode(client, setup_mode):
    r = client.get("/reset-password")
    assert r.status_code == 200
    assert "setup button" in r.text


def test_reset_flow_changes_password(client, setup_mode):
    _onboard(client)
    client.cookies.clear()
    setup_mode.enter_setup_mode("test")

    r = client.post("/api/reset-password", json={"identifier": ACCOUNT["username"], "new_password": NEW_PASSWORD})
    assert r.status_code == 200 and r.json()["ok"] is True

    assert client.post("/api/login", json={"username": ACCOUNT["username"], "password": ACCOUNT["password"]}).status_code == 401
    assert client.post("/api/login", json={"username": ACCOUNT["username"], "password": NEW_PASSWORD}).status_code == 200


def test_reset_invalidates_existing_sessions(client, setup_mode):
    _onboard(client)  # leaves a logged-in session cookie
    setup_mode.enter_setup_mode("test")

    client.post("/api/reset-password", json={"identifier": ACCOUNT["email"], "new_password": NEW_PASSWORD})

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_reset_is_generic_for_unknown_accounts(client, setup_mode):
    _onboard(client)
    client.cookies.clear()
    setup_mode.enter_setup_mode("test")
    r = client.post("/api/reset-password", json={"identifier": "nobody", "new_password": NEW_PASSWORD})
    # Same response as a successful reset — no account enumeration.
    assert r.status_code == 200 and r.json()["ok"] is True
