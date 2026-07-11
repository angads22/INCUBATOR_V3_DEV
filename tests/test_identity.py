"""Sign in with Google / Apple, account linking, and the security hardening.

The Google device flow is exercised against a fake token endpoint; broker
assertions are exercised with a real HMAC secret. The core security property
under test: a provider identity can only LOG IN when already linked (or when
the device has no accounts at all), so a stranger's Google account can never
claim a farmer's incubator.
"""

import time

import pytest

import app.services.identity_service as ids
from app.routes.identity import identity_service

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


@pytest.fixture(autouse=True)
def _no_grace_sleep(client):
    """A prior test's simulated setup-AP would make complete() sleep its full
    12 s hand-off grace on every onboard here — zero it and reset AP state."""
    from app import main
    main.onboarding_service._complete_grace_seconds = 0.0
    main.onboarding_service._hotspot_active = False
    yield
    main.onboarding_service._hotspot_active = False


class _FakeGoogle:
    """Scripted httpx.post stand-in for Google's device/token endpoints."""

    def __init__(self):
        self.approved = False
        self.aud = "test-client-id"

    def __call__(self, url, data=None, timeout=None):
        import base64, json as _json

        class R:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        if url.endswith("/device/code"):
            return R({"device_code": "dev123", "user_code": "ABCD-EFGH",
                      "verification_url": "https://google.com/device",
                      "expires_in": 1800, "interval": 0})
        if not self.approved:
            return R({"error": "authorization_pending"})
        claims = {"iss": "https://accounts.google.com", "aud": self.aud,
                  "sub": "google-sub-1", "email": "farmer@gmail.com",
                  "exp": time.time() + 3600}
        b64 = base64.urlsafe_b64encode(_json.dumps(claims).encode()).rstrip(b"=").decode()
        return R({"id_token": f"h.{b64}.s"})


@pytest.fixture()
def google(monkeypatch):
    fake = _FakeGoogle()
    import httpx
    monkeypatch.setattr(httpx, "post", fake)
    monkeypatch.setattr(identity_service, "google_client_id", "test-client-id")
    yield fake
    identity_service._flows.clear()


@pytest.fixture()
def broker(monkeypatch):
    monkeypatch.setattr(identity_service, "broker_secret", "fleet-secret")
    monkeypatch.setattr(identity_service, "broker_url", "https://portal.example/link")
    yield
    identity_service._seen_assertions.clear()


def _onboard(client):
    return client.post("/onboarding/complete", json={
        "ssid": "", "wifi_password": "", "device_name": "Coop 1",
        "create_account": True, **ACCOUNT,
    })


# ── Config discovery ─────────────────────────────────────────────────────────

def test_providers_hidden_when_unconfigured(client):
    cfg = client.get("/api/identity/config").json()
    assert cfg["google"] is False and cfg["apple"] is False


def test_providers_visible_when_configured(client, google, broker):
    cfg = client.get("/api/identity/config").json()
    assert cfg["google"] is True and cfg["apple"] is True


# ── Google device flow ───────────────────────────────────────────────────────

def _fast_flow(begin):
    """Zero out Google's min poll interval so tests don't wait real seconds."""
    flow = identity_service._flows.get(begin["flow_id"])
    if flow:
        flow["interval"] = 0


def test_google_first_signin_creates_owner(client, google):
    begin = client.post("/api/identity/google/begin").json()
    assert begin["ok"] and begin["user_code"] == "ABCD-EFGH"
    _fast_flow(begin)
    # Not approved yet → pending.
    r = client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "login"}).json()
    assert r["status"] == "pending"
    # Operator approves on their phone → owner account is created + logged in.
    google.approved = True
    r = client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "login"}).json()
    assert r["status"] == "ok" and r["created"] is True
    assert r["username"] == "farmer"   # from farmer@gmail.com
    # Session cookie actually works on a protected endpoint.
    assert client.get("/api/identity/links").json()["ok"] is True


def test_google_login_refused_for_unlinked_identity(client, google):
    _onboard(client)                       # a local owner exists
    client.cookies.clear()                 # attacker: fresh browser, own Google account
    begin = client.post("/api/identity/google/begin").json()
    google.approved = True
    r = client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "login"})
    assert r.status_code == 403            # NOT allowed to take over the device
    assert "linked" in r.json()["detail"].lower() or "link" in r.json()["detail"].lower()


def test_google_link_then_login(client, google):
    _onboard(client)                       # logged-in owner
    begin = client.post("/api/identity/google/begin").json()
    google.approved = True
    r = client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "link"}).json()
    assert r["ok"] and r["linked"] is True
    links = client.get("/api/identity/links").json()["links"]
    assert links and links[0]["provider"] == "google"

    # Now the SAME Google identity logs in from a fresh browser.
    client.cookies.clear()
    begin = client.post("/api/identity/google/begin").json()
    r = client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "login"}).json()
    assert r["status"] == "ok" and r["username"] == "farmer" and r["created"] is False


def test_unlink_removes_provider_login(client, google):
    _onboard(client)
    begin = client.post("/api/identity/google/begin").json()
    google.approved = True
    client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "link"})
    link_id = client.get("/api/identity/links").json()["links"][0]["id"]
    assert client.delete(f"/api/identity/links/{link_id}").json()["ok"] is True

    client.cookies.clear()
    begin = client.post("/api/identity/google/begin").json()
    r = client.post("/api/identity/google/poll", json={"flow_id": begin["flow_id"], "intent": "login"})
    assert r.status_code == 403            # unlinked → refused again


# ── Apple via broker assertions ──────────────────────────────────────────────

def test_apple_link_and_login_via_broker(client, broker):
    _onboard(client)
    a = identity_service.make_broker_assertion("apple", "apple-sub-9", "farmer@icloud.com")
    r = client.post("/api/identity/broker", json={"assertion": a, "intent": "link"}).json()
    assert r["ok"] and r["provider"] == "apple"

    client.cookies.clear()
    a2 = identity_service.make_broker_assertion("apple", "apple-sub-9", "farmer@icloud.com")
    r = client.post("/api/identity/broker", json={"assertion": a2, "intent": "login"}).json()
    assert r["ok"] and r["username"] == "farmer"


def test_broker_assertion_tamper_replay_expiry(client, broker):
    _onboard(client)
    a = identity_service.make_broker_assertion("apple", "s1", "x@icloud.com")
    body, sig = a.rsplit(".", 1)
    # Tampered signature refused.
    r = client.post("/api/identity/broker", json={"assertion": f"{body}.{'0'*64}", "intent": "link"})
    assert r.status_code == 400
    # Valid assertion accepted once…
    assert client.post("/api/identity/broker", json={"assertion": a, "intent": "link"}).status_code == 200
    # …and refused on replay.
    assert client.post("/api/identity/broker", json={"assertion": a, "intent": "link"}).status_code == 400
    # Expired assertion refused.
    old = identity_service.make_broker_assertion("apple", "s2", None, iat=time.time() - 3600)
    assert client.post("/api/identity/broker", json={"assertion": old, "intent": "link"}).status_code == 400


def test_link_requires_session(client, broker):
    _onboard(client)
    client.cookies.clear()
    a = identity_service.make_broker_assertion("apple", "s3", None)
    assert client.post("/api/identity/broker", json={"assertion": a, "intent": "link"}).status_code == 401


def test_identity_cannot_be_linked_to_two_accounts(client, google, broker):
    _onboard(client)
    a = identity_service.make_broker_assertion("apple", "shared-sub", None)
    assert client.post("/api/identity/broker", json={"assertion": a, "intent": "link"}).status_code == 200
    # Second local account tries to grab the same Apple identity.
    from app.database import get_db
    from app.auth import hash_password, create_session
    from app.models import User
    db = next(get_db())
    u2 = User(username="second", email="second@example.com", password_hash=hash_password("hunter2pass"))
    db.add(u2); db.commit()
    token = create_session(db, u2.id)
    db.close()
    client.cookies.set("incubator_session", token)
    a2 = identity_service.make_broker_assertion("apple", "shared-sub", None)
    assert client.post("/api/identity/broker", json={"assertion": a2, "intent": "link"}).status_code == 409


# ── Security hardening ───────────────────────────────────────────────────────

def test_login_throttled_after_repeated_failures(client):
    from app.auth import reset_login_throttle
    reset_login_throttle()
    _onboard(client)
    client.cookies.clear()
    for _ in range(5):
        r = client.post("/api/login", json={"username": "farmer", "password": "wrong-pass"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"username": "farmer", "password": "wrong-pass"})
    assert r.status_code == 429
    # Even the CORRECT password is refused during lockout (that's the point).
    r = client.post("/api/login", json={"username": "farmer", "password": ACCOUNT["password"]})
    assert r.status_code == 429
    reset_login_throttle()
    r = client.post("/api/login", json={"username": "farmer", "password": ACCOUNT["password"]})
    assert r.status_code == 200


def test_security_headers_present(client):
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_cross_origin_post_refused(client):
    r = client.post("/api/login", json={"username": "x", "password": "y"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    # Same-origin (matching host) posts pass through the guard.
    host = "testserver"
    r = client.post("/api/login", json={"username": "x", "password": "yyyyyyyy"},
                    headers={"Origin": f"http://{host}"})
    assert r.status_code in (401, 429)     # reaches the endpoint (auth fails, not blocked)


def test_reverse_proxy_post_allowed_via_forwarded_host(client):
    """Remote access via a reverse proxy: the browser's Origin is the external
    domain while Host is rewritten to the backend. X-Forwarded-Host (set by the
    trusted proxy) makes the external Origin a valid served-as host, so the POST
    must NOT be falsely blocked."""
    r = client.post(
        "/api/login",
        json={"username": "x", "password": "yyyyyyyy"},
        headers={
            "Origin": "https://farm.example.com",
            "Host": "10.42.0.1:8000",
            "X-Forwarded-Host": "farm.example.com",
        },
    )
    assert r.status_code in (401, 429)     # reached the handler, not the 403 guard
    # …but a genuine cross-site Origin is still blocked even with XFH present.
    r = client.post(
        "/api/login",
        json={"username": "x", "password": "y"},
        headers={"Origin": "https://evil.example", "Host": "10.42.0.1:8000",
                 "X-Forwarded-Host": "farm.example.com"},
    )
    assert r.status_code == 403
