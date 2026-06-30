"""Camera preview + vision-model endpoints."""

ACCOUNT = {"username": "farmer", "email": "farmer@example.com", "password": "hunter2pass"}


def test_camera_status_and_snapshot(client):
    status = client.get("/api/camera/status").json()
    assert status["ok"] is True and status["backend"] == "mock"

    # Mock backend produces a real JPEG, so the preview shows a valid image.
    r = client.get("/api/camera/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_camera_image_is_path_traversal_safe(client):
    # Names that aren't bare capture_* files are rejected before touching disk.
    assert client.get("/api/camera/image/passwd").status_code == 400
    assert client.get("/api/camera/image/..%2f..%2fetc%2fpasswd").status_code in (400, 404)
    # A well-formed but non-existent capture name → 404, not 400.
    assert client.get("/api/camera/image/capture_20000101_000000_aaaaaa.jpg").status_code == 404


def test_vision_status(client):
    body = client.get("/api/vision/status").json()
    assert body["ok"] is True
    assert body["backend"] == "mock"
    assert body["ready"] is True
    assert isinstance(body["labels"], list)


def test_candle_returns_image_url_and_shows_in_results(client):
    candle = client.post("/api/vision/candle", json={"egg_id": 1, "persist": True}).json()
    assert candle["endpoint"] == "vision.candle"
    assert candle["image_url"].startswith("/api/camera/image/capture_")

    results = client.get("/api/vision/results").json()
    assert results["ok"] is True
    assert len(results["results"]) >= 1
    assert results["results"][0]["image_url"].startswith("/api/camera/image/capture_")


def test_camera_requires_auth_once_account_exists(client):
    # Create an operator account, then drop the session — camera must lock down.
    client.post(
        "/onboarding/complete",
        json={"ssid": "Net", "wifi_password": "", "device_name": "Coop", "create_account": True, **ACCOUNT},
    )
    client.cookies.clear()
    assert client.get("/api/camera/snapshot").status_code == 401
    assert client.get("/api/vision/status").status_code == 401
