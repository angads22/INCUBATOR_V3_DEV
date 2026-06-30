"""Testing-tab API — predict, record, results (MAE), clear — plus pure MAE.

Runs entirely on the heuristic backend with uploaded images: no camera, no GPU.
"""

import io

import pytest

from app.routes.testing import compute_mae


def _jpeg(color=(200, 160, 110), size=(160, 160)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (18, 12, 8))
    draw = ImageDraw.Draw(img)
    draw.ellipse([30, 20, size[0] - 30, size[1] - 20], fill=color)
    draw.ellipse([60, 70, 100, 120], fill=(110, 70, 40))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Pure MAE
# --------------------------------------------------------------------------

def test_compute_mae_basic():
    rows = [
        {"predicted_day": 8.0, "actual_day": 7.0},
        {"predicted_day": 5.0, "actual_day": 9.0},
        {"predicted_day": 3.0, "actual_day": None},  # ignored — no ground truth
    ]
    out = compute_mae(rows)
    assert out["count"] == 2
    assert out["mae"] == pytest.approx((1.0 + 4.0) / 2)


def test_compute_mae_empty():
    out = compute_mae([{"predicted_day": 3.0, "actual_day": None}])
    assert out["count"] == 0
    assert out["mae"] is None


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------

def test_testing_page_renders(client):
    r = client.get("/testing")
    assert r.status_code == 200
    assert "Vision Model Testing" in r.text
    # Nav gained a single Testing link, existing ones intact.
    assert 'href="/testing"' in r.text
    assert 'href="/status"' in r.text


# --------------------------------------------------------------------------
# Predict → record → results → clear
# --------------------------------------------------------------------------

def test_predict_returns_contract(client):
    r = client.post("/api/testing/predict", files={"files": ("egg.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["count"] == 1
    pred = body["predictions"][0]
    for key in ("day_estimate", "day_range", "stage", "confidence", "features", "backend", "path"):
        assert key in pred
    assert pred["backend"] == "heuristic"


def test_predict_requires_an_image(client):
    r = client.post("/api/testing/predict")
    assert r.status_code == 400


def test_record_and_results_compute_mae(client):
    # Two predictions recorded with known actual days.
    client.post("/api/testing/record", json={
        "image_path": "/tmp/a.jpg", "predicted_day": 8.0, "stage": "early",
        "confidence": 0.6, "backend": "heuristic", "actual_day": 7.0,
    })
    client.post("/api/testing/record", json={
        "image_path": "/tmp/b.jpg", "predicted_day": 12.0, "stage": "mid",
        "confidence": 0.7, "backend": "heuristic", "actual_day": 10.0,
    })
    r = client.get("/api/testing/results")
    body = r.json()
    assert body["ok"] is True
    assert body["total"] == 2
    assert body["count"] == 2
    assert body["mae"] == pytest.approx((1.0 + 2.0) / 2)
    # newest first
    assert body["results"][0]["image_path"] == "/tmp/b.jpg"
    assert body["results"][0]["error"] == pytest.approx(2.0)


def test_clear_empties_results(client):
    client.post("/api/testing/record", json={
        "image_path": "/tmp/a.jpg", "predicted_day": 8.0, "actual_day": 7.0,
    })
    assert client.get("/api/testing/results").json()["total"] == 1
    cleared = client.post("/api/testing/clear").json()
    assert cleared["ok"] is True and cleared["cleared"] == 1
    after = client.get("/api/testing/results").json()
    assert after["total"] == 0 and after["mae"] is None


def test_results_csv_export(client):
    client.post("/api/testing/record", json={
        "image_path": "/tmp/a.jpg", "predicted_day": 8.0, "stage": "early",
        "confidence": 0.6, "backend": "heuristic", "actual_day": 7.0,
    })
    r = client.get("/api/testing/results.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "predicted_day" in r.text and "/tmp/a.jpg" in r.text


def test_predict_rejects_path_outside_allowed_roots(client):
    # Path traversal / arbitrary read must be refused (no allowed image).
    r = client.post("/api/testing/predict", data={"paths": "/etc/passwd"})
    assert r.status_code == 400


def test_captures_listing_skips_symlinks_escaping_roots(client, tmp_path):
    # A symlink inside the captures dir pointing outside must NOT be listed —
    # otherwise its resolved (external) path would leak in the response.
    import os
    from pathlib import Path

    from app.config import settings

    secret = tmp_path / "secret_outside.jpg"
    secret.write_bytes(_jpeg())
    captures = Path(settings.captures_dir)
    captures.mkdir(parents=True, exist_ok=True)
    link = captures / "leak.jpg"
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        import pytest as _pytest
        _pytest.skip("symlinks unsupported on this platform")

    try:
        body = client.get("/api/testing/captures").json()
        paths = [c["path"] for c in body["captures"]]
        assert str(secret.resolve()) not in paths
        assert all("secret_outside" not in p for p in paths)
    finally:
        link.unlink()
