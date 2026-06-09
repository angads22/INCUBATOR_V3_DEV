"""Network settings API: status and switching Wi-Fi after setup."""

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


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


def test_network_endpoints_require_auth_once_account_exists(client):
    _onboard(client)
    client.cookies.clear()
    assert client.get("/api/network/status").status_code == 401
    assert client.post("/api/network/connect", json={"ssid": "NewNet", "password": ""}).status_code == 401


def test_network_status_reports_configured_ssid(client):
    _onboard(client)
    body = client.get("/api/network/status").json()
    assert body["ok"] is True
    assert body["configured_ssid"] == "HomeNet"


def test_network_connect_updates_device_config(client):
    _onboard(client)
    # WiFiService simulates success in dev (no nmcli present).
    r = client.post("/api/network/connect", json={"ssid": "BarnOffice", "password": "pass12345"})
    assert r.status_code == 200 and r.json()["ok"] is True

    body = client.get("/api/network/status").json()
    assert body["configured_ssid"] == "BarnOffice"


def test_network_connect_rejects_bad_ssid(client):
    _onboard(client)
    r = client.post("/api/network/connect", json={"ssid": "", "password": ""})
    assert r.status_code == 422
