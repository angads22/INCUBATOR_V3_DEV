"""Egg-photo storage + disk-pressure auto-prune."""

import tempfile
from pathlib import Path

from app.services.storage_service import StorageService, plan_pruning


def _photo(pid, size, age_s, pinned=False, now=1000):
    return {"id": pid, "size": size, "created_ts": now - age_s, "pinned": pinned}


# ── Pure planner ─────────────────────────────────────────────────────────────

def test_no_pressure_deletes_nothing():
    photos = [_photo(i, 100, age) for i, age in [(1, 500), (2, 400), (3, 300)]]
    out = plan_pruning(
        photos, free_bytes=10**9, dir_bytes=300, min_free_bytes=1000,
        target_free_bytes=2000, max_dir_bytes=0, keep_min=0, now_ts=1000, retention_seconds=0,
    )
    assert out == []


def test_low_free_deletes_oldest_keeps_newest_and_pinned():
    # 5 photos, oldest→newest = ids 1..5. keep_min protects the 2 newest.
    photos = [_photo(i, 100, age) for i, age in [(1, 500), (2, 400), (3, 300), (4, 200), (5, 100)]]
    out = plan_pruning(
        photos, free_bytes=0, dir_bytes=500, min_free_bytes=250,
        target_free_bytes=250, max_dir_bytes=0, keep_min=2, now_ts=1000, retention_seconds=0,
    )
    assert set(out) == {1, 2, 3}          # oldest pruned
    assert 4 not in out and 5 not in out  # keep_min protected the newest


def test_pinned_is_never_deleted():
    photos = [_photo(1, 100, 500, pinned=True), _photo(2, 100, 400), _photo(3, 100, 300),
              _photo(4, 100, 200), _photo(5, 100, 100)]
    out = plan_pruning(
        photos, free_bytes=0, dir_bytes=500, min_free_bytes=250,
        target_free_bytes=250, max_dir_bytes=0, keep_min=2, now_ts=1000, retention_seconds=0,
    )
    assert 1 not in out                   # pinned survives even under pressure


def test_retention_prunes_aged_first():
    photos = [_photo(i, 100, age) for i, age in [(1, 500), (2, 400), (3, 300), (4, 200), (5, 100)]]
    out = plan_pruning(
        photos, free_bytes=10**9, dir_bytes=500, min_free_bytes=0,
        target_free_bytes=0, max_dir_bytes=0, keep_min=0, now_ts=1000, retention_seconds=250,
    )
    assert set(out) == {1, 2, 3}          # only the >250s-old photos


def test_dir_cap_triggers_pruning():
    photos = [_photo(i, 100, age) for i, age in [(1, 500), (2, 400), (3, 300), (4, 200), (5, 100)]]
    out = plan_pruning(
        photos, free_bytes=10**9, dir_bytes=500, min_free_bytes=0,
        target_free_bytes=0, max_dir_bytes=250, keep_min=0, now_ts=1000, retention_seconds=0,
    )
    # Prune oldest until dir (500) is back under the 250-byte cap.
    assert out[:3] == [1, 2, 3]


# ── Service (real files in a temp dir) ───────────────────────────────────────

def _db():
    from app.database import get_db
    return next(get_db())


def test_save_creates_file_and_row(client):
    svc = StorageService(tempfile.mkdtemp(), enabled=True, min_free_mb=0,
                         target_free_mb=0, max_dir_mb=0, keep_min=100, retention_days=0)
    db = _db()
    try:
        row = svc.save_photo(db, b"\xff\xd8jpeg-bytes", label="Egg 7", backend="mock", confidence=0.91)
        assert row.id is not None
        assert Path(row.path).is_file()
        assert "egg-7" in Path(row.path).name.lower()       # label slug in filename
        u = svc.usage(db)
        assert u["photo_count"] == 1 and u["enabled"] is True
    finally:
        db.close()


def test_auto_prune_keeps_only_newest_under_pressure(client):
    svc = StorageService(tempfile.mkdtemp(), enabled=True, min_free_mb=10,
                         target_free_mb=10, max_dir_mb=0, keep_min=2, retention_days=0)
    svc._disk_free_total = lambda: (0, 100 * 1024 * 1024)   # pretend the card is full
    db = _db()
    try:
        for i in range(5):
            svc.save_photo(db, b"\xff\xd8" + bytes(64), label=f"egg{i}")
        u = svc.usage(db)
        assert u["photo_count"] == 2                          # pruned down to keep_min
        # The two survivors still have their files on disk.
        from app.models import EggPhoto
        from sqlalchemy import select
        for row in db.scalars(select(EggPhoto)).all():
            assert Path(row.path).is_file()
    finally:
        db.close()


def test_pinned_photo_survives_pressure(client):
    svc = StorageService(tempfile.mkdtemp(), enabled=True, min_free_mb=10,
                         target_free_mb=10, max_dir_mb=0, keep_min=1, retention_days=0)
    svc._disk_free_total = lambda: (0, 100 * 1024 * 1024)
    db = _db()
    try:
        first = svc.save_photo(db, b"\xff\xd8" + bytes(64), label="keepme")
        svc.set_pinned(db, first.id, True)
        for i in range(4):
            svc.save_photo(db, b"\xff\xd8" + bytes(64), label=f"egg{i}")
        from app.models import EggPhoto
        assert db.get(EggPhoto, first.id) is not None         # pinned not pruned
        assert Path(first.path).is_file()
    finally:
        db.close()


# ── Endpoints ────────────────────────────────────────────────────────────────

def test_capture_list_image_pin_delete(client):
    r = client.post("/api/captures/capture", params={"label": "Egg A"})
    assert r.status_code == 200
    pid = r.json()["photo"]["id"]

    listing = client.get("/api/captures").json()
    assert listing["ok"] and listing["total"] >= 1

    assert client.get("/api/captures/image", params={"id": pid}).status_code == 200
    assert client.get("/api/captures/storage").json()["ok"] is True

    assert client.post(f"/api/captures/{pid}/pin", params={"pinned": True}).json()["photo"]["pinned"] is True
    assert client.post("/api/captures/prune").json()["ok"] is True
    assert client.delete(f"/api/captures/{pid}").json()["deleted"] == pid
    assert client.get("/api/captures/image", params={"id": pid}).status_code == 404
