"""End-to-end auth lifecycle tests (run against the real FastAPI app in mock mode).

These guard the behaviour that was previously broken: login could never succeed
because /api/login did not exist and no session was ever created.
"""

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


def _onboard(client, create_account=True, **overrides):
    payload = {
        "ssid": "HomeNet",
        "wifi_password": "",
        "device_name": "Coop 1",
        "create_account": create_account,
        "username": ACCOUNT["username"],
        "email": ACCOUNT["email"],
        "password": ACCOUNT["password"],
    }
    payload.update(overrides)
    return client.post("/onboarding/complete", json=payload)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dashboard_open_before_any_account(client):
    # A fresh device is open so onboarding can run.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_onboarding_creates_account_and_auto_logs_in(client):
    r = _onboard(client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["claimed"] is True
    assert "incubator_session" in client.cookies
    assert client.get("/", follow_redirects=False).status_code == 200


def test_login_required_once_account_exists(client):
    _onboard(client)
    client.cookies.clear()  # simulate a brand-new browser
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # Control APIs are gated too.
    assert client.post("/api/settings", json={"target_temp_c": 38.0}).status_code == 401
    assert client.post("/hardware/send", json={"action": "heater_on"}).status_code == 401


def test_login_logout_and_wrong_password(client):
    _onboard(client)
    client.cookies.clear()
    assert client.post("/api/login", json={"username": ACCOUNT["username"], "password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"username": ACCOUNT["username"], "password": ACCOUNT["password"]})
    assert r.status_code == 200 and "incubator_session" in client.cookies
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.post("/api/logout").status_code == 200


def test_login_by_email(client):
    _onboard(client)
    client.cookies.clear()
    r = client.post("/api/login", json={"username": ACCOUNT["email"], "password": ACCOUNT["password"]})
    assert r.status_code == 200


def test_login_page_renders_with_onboarding_hint(client):
    # No account yet -> the login page should point users to setup.
    r = client.get("/login")
    assert r.status_code == 200 and "Run device setup" in r.text
