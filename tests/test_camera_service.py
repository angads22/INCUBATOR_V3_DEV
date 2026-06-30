"""CameraService — mock backend produces valid images with no hardware.

Covers the persistent-session API surface used by the camera routes:
snapshot, low-res preview, file capture, and per-egg ROI cropping.
"""

import io

from PIL import Image

from app.services.camera_service import CameraService


def _cam(tmp_path):
    return CameraService(
        backend="mock",
        image_dir=str(tmp_path / "captures"),
        resolution=(640, 480),
        preview_resolution=(160, 120),
        frame_dir=str(tmp_path / "frames"),
    )


def test_snapshot_is_valid_jpeg(tmp_path):
    cam = _cam(tmp_path)
    data = cam.snapshot_jpeg()
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.size == (640, 480)


def test_preview_is_low_res_jpeg(tmp_path):
    cam = _cam(tmp_path)
    data = cam.preview_jpeg()
    img = Image.open(io.BytesIO(data))
    assert img.size == (160, 120)


def test_capture_writes_file(tmp_path):
    cam = _cam(tmp_path)
    result = cam.capture()
    assert result["ok"] is True
    assert result["backend"] == "mock"
    from pathlib import Path

    p = Path(result["image_path"])
    assert p.exists() and p.stat().st_size > 512
    # The saved file is a real, openable JPEG (not placeholder bytes).
    Image.open(p).verify()


def test_egg_crop_with_roi_returns_cropped_region(tmp_path):
    cam = _cam(tmp_path)
    cropped = cam.egg_crop_jpeg((100, 80, 200, 150))
    img = Image.open(io.BytesIO(cropped))
    assert img.size == (200, 150)


def test_egg_crop_without_roi_returns_full_frame(tmp_path):
    cam = _cam(tmp_path)
    full = cam.egg_crop_jpeg(None)
    img = Image.open(io.BytesIO(full))
    assert img.size == (640, 480)


def test_oversized_roi_is_clamped(tmp_path):
    cam = _cam(tmp_path)
    # ROI larger than the frame must not error — it is clamped to the frame.
    cropped = cam.egg_crop_jpeg((500, 400, 9999, 9999))
    img = Image.open(io.BytesIO(cropped))
    assert img.size[0] <= 640 and img.size[1] <= 480


def test_repeated_captures_do_not_error(tmp_path):
    # Regression guard: the session must keep working across many captures.
    cam = _cam(tmp_path)
    for _ in range(5):
        assert cam.capture()["ok"] is True
        assert len(cam.preview_jpeg()) > 0
    cam.cleanup()
