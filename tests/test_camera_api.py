"""Camera API routes — snapshot, stream gating, egg tile, and auth gating.

Uses the mock camera backend, so no hardware is required.
"""

import io

from PIL import Image


def _make_user():
    """Create an owner account directly so auth gating engages."""
    from app.auth import hash_password
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        db.add(User(username="owner", email="o@example.com", password_hash=hash_password("password123"), role="owner"))
        db.commit()
    finally:
        db.close()


def _make_egg(roi=None):
    from app.database import SessionLocal
    from app.models import Egg

    db = SessionLocal()
    try:
        egg = Egg(incubator_id=1, label="A1", state="unknown")
        if roi:
            egg.roi_x, egg.roi_y, egg.roi_w, egg.roi_h = roi
        db.add(egg)
        db.commit()
        db.refresh(egg)
        return egg.id
    finally:
        db.close()


def test_snapshot_returns_jpeg(client):
    r = client.get("/api/camera/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    Image.open(io.BytesIO(r.content)).verify()


def test_stream_disabled_by_default(client):
    # CAMERA_STREAM_ENABLED defaults to false → the MJPEG endpoint 404s.
    r = client.get("/api/camera/stream")
    assert r.status_code == 404


def test_egg_tile_without_roi_returns_full_frame(client):
    egg_id = _make_egg(roi=None)
    r = client.get(f"/api/camera/egg/{egg_id}")
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.format == "JPEG"


def test_egg_tile_with_roi_returns_cropped(client):
    egg_id = _make_egg(roi=(50, 40, 120, 100))
    r = client.get(f"/api/camera/egg/{egg_id}")
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (120, 100)


def test_snapshot_requires_auth_once_account_exists(client):
    _make_user()
    # No session cookie → must be rejected, exactly like /hardware/send.
    r = client.get("/api/camera/snapshot")
    assert r.status_code == 401
