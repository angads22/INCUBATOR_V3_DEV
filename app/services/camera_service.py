"""
Camera service for Raspberry Pi Zero 2W.

Backends (set CAMERA_BACKEND env var):
  picamera2  — Pi Camera Module v1/v2/v3 via libcamera (default)
  opencv     — USB webcam via OpenCV / cv2
  mock       — Returns a placeholder image without touching hardware

The session is PERSISTENT: the camera is configured and started once and kept
running. Two outputs are exposed:

  * a low-resolution (~640x480) preview frame at ~1 fps for the live MJPEG stream
  * a full-resolution still on demand (snapshot / capture)

This fixes the earlier behaviour where picamera2 was stopped after the first
capture and never restarted — the persistent preview config stays live and
full-res stills are taken via a momentary mode switch that returns to preview.

Still images are stored under CAMERA_IMAGE_DIR (default: ./captures); transient
preview frames are kept in RAM (and optionally tmpfs), never written to the SD
card. The image path returned by ``capture()`` is used by the vision service.
"""

from __future__ import annotations

import datetime
import io
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# MJPEG multipart boundary token.
_BOUNDARY = "frame"


class CameraService:
    def __init__(
        self,
        backend: str = "picamera2",
        image_dir: str = "./captures",
        resolution: tuple[int, int] = (1920, 1080),
        preview_resolution: tuple[int, int] = (640, 480),
        preview_fps: float = 1.0,
        frame_dir: str = "/run/incubator/frames",
    ) -> None:
        self.backend = backend
        self.image_dir = Path(image_dir)
        self.resolution = resolution
        self.preview_resolution = preview_resolution
        self.preview_fps = max(0.2, float(preview_fps))
        self.frame_dir = Path(frame_dir)
        self._picam: Any = None
        self._still_config: Any = None
        self._cv_cap: Any = None
        # Serialises all hardware access — a snapshot and the preview stream can
        # run concurrently (FastAPI sync endpoints run in a threadpool).
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_image_path(self) -> Path:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:6]
        return self.image_dir / f"capture_{ts}_{uid}.jpg"

    @staticmethod
    def _encode_jpeg(image: Any, size: tuple[int, int] | None = None, quality: int = 80) -> bytes:
        """Encode a PIL image (or a numpy array) to JPEG bytes."""
        from PIL import Image  # type: ignore

        if not isinstance(image, Image.Image):
            image = Image.fromarray(image)
        if image.mode != "RGB":
            image = image.convert("RGB")
        if size is not None and image.size != size:
            image = image.resize(size)
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def _mock_jpeg(self, size: tuple[int, int]) -> bytes:
        """A valid, deterministic candled-egg-like JPEG for dev/CI.

        Produces a warm oval with a darker interior mass so the heuristic vision
        backend has real structure to measure when no camera is attached.
        """
        from PIL import Image, ImageDraw  # type: ignore

        w, h = size
        img = Image.new("RGB", (w, h), (24, 16, 10))
        draw = ImageDraw.Draw(img)
        # Glowing egg (warm), centred.
        margin_x, margin_y = int(w * 0.28), int(h * 0.18)
        draw.ellipse(
            [margin_x, margin_y, w - margin_x, h - margin_y],
            fill=(220, 180, 120),
        )
        # Darker embryo mass, lower-centre.
        cx, cy = w // 2, int(h * 0.58)
        rx, ry = int(w * 0.12), int(h * 0.14)
        draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(120, 80, 45))
        # Bright air cell at the blunt (top) end.
        ax, ay = w // 2, int(h * 0.30)
        ar = int(w * 0.06)
        draw.ellipse([ax - ar, ay - ar, ax + ar, ay + ar], fill=(245, 230, 200))
        return self._encode_jpeg(img, quality=82)

    def _write_preview_frame(self, data: bytes) -> None:
        """Best-effort cache of the latest preview frame in tmpfs (RAM)."""
        try:
            self.frame_dir.mkdir(parents=True, exist_ok=True)
            (self.frame_dir / "preview.jpg").write_bytes(data)
        except Exception:  # noqa: BLE001 — caching is optional, never fatal
            pass

    # ------------------------------------------------------------------
    # picamera2 (persistent session)
    # ------------------------------------------------------------------

    def _ensure_picam(self) -> Any:
        """Lazily create + start a persistent preview session. Returns the cam."""
        if self._picam is not None:
            return self._picam
        from picamera2 import Picamera2  # type: ignore

        cam = Picamera2()
        # Persistent low-res preview config — stays live for the MJPEG stream.
        preview_config = cam.create_video_configuration(
            main={"size": self.preview_resolution, "format": "RGB888"}
        )
        cam.configure(preview_config)
        # Full-res still config used only for momentary mode switches.
        self._still_config = cam.create_still_configuration(main={"size": self.resolution})
        cam.start()
        self._picam = cam
        return cam

    def _picam_preview_jpeg(self) -> bytes:
        cam = self._ensure_picam()
        frame = cam.capture_array("main")
        return self._encode_jpeg(frame, size=self.preview_resolution)

    def _picam_snapshot_jpeg(self) -> bytes:
        cam = self._ensure_picam()
        # Momentarily switch to the still config and return to preview — the
        # session is NOT stopped, so the stream keeps running afterwards.
        frame = cam.switch_mode_and_capture_array(self._still_config, "main")
        return self._encode_jpeg(frame, quality=90)

    # ------------------------------------------------------------------
    # OpenCV (persistent capture device)
    # ------------------------------------------------------------------

    def _ensure_cv(self) -> Any:
        import cv2  # type: ignore

        if self._cv_cap is not None and self._cv_cap.isOpened():
            return self._cv_cap
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            raise RuntimeError("No USB camera found")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        self._cv_cap = cap
        return cap

    def _cv_frame(self) -> Any:
        import cv2  # type: ignore

        cap = self._ensure_cv()
        ret, frame = cap.read()
        if not ret:
            raise RuntimeError("Frame capture failed")
        # OpenCV is BGR; convert to RGB for consistent JPEG colour.
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def _cv_preview_jpeg(self) -> bytes:
        return self._encode_jpeg(self._cv_frame(), size=self.preview_resolution)

    def _cv_snapshot_jpeg(self) -> bytes:
        return self._encode_jpeg(self._cv_frame(), quality=90)

    # ------------------------------------------------------------------
    # Public — JPEG outputs (used by the camera routes)
    # ------------------------------------------------------------------

    def snapshot_jpeg(self) -> bytes:
        """Full-resolution still as JPEG bytes."""
        with self._lock:
            if self.backend == "picamera2":
                return self._picam_snapshot_jpeg()
            if self.backend == "opencv":
                return self._cv_snapshot_jpeg()
            return self._mock_jpeg(self.resolution)

    def preview_jpeg(self) -> bytes:
        """Single low-resolution preview frame as JPEG bytes."""
        with self._lock:
            if self.backend == "picamera2":
                data = self._picam_preview_jpeg()
            elif self.backend == "opencv":
                data = self._cv_preview_jpeg()
            else:
                data = self._mock_jpeg(self.preview_resolution)
        self._write_preview_frame(data)
        return data

    def egg_crop_jpeg(self, roi: tuple[int, int, int, int] | None) -> bytes:
        """Crop the egg's ROI from a full-res snapshot. Full frame if roi is None."""
        snapshot = self.snapshot_jpeg()
        if not roi:
            return snapshot
        from PIL import Image  # type: ignore

        x, y, w, h = roi
        img = Image.open(io.BytesIO(snapshot)).convert("RGB")
        iw, ih = img.size
        # Clamp the box to the frame so a stale/oversized ROI never errors.
        x0 = max(0, min(int(x), iw - 1))
        y0 = max(0, min(int(y), ih - 1))
        x1 = max(x0 + 1, min(int(x) + int(w), iw))
        y1 = max(y0 + 1, min(int(y) + int(h), ih))
        return self._encode_jpeg(img.crop((x0, y0, x1, y1)), quality=88)

    def mjpeg_stream(self) -> Iterator[bytes]:
        """Yield multipart/x-mixed-replace MJPEG chunks at ~preview_fps."""
        interval = 1.0 / self.preview_fps
        while True:
            try:
                frame = self.preview_jpeg()
            except Exception as exc:  # noqa: BLE001 — a dropped frame must not kill the stream
                logger.debug("preview frame failed: %s", exc)
                time.sleep(interval)
                continue
            yield (
                b"--" + _BOUNDARY.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame + b"\r\n"
            )
            time.sleep(interval)

    @property
    def boundary(self) -> str:
        return _BOUNDARY

    # ------------------------------------------------------------------
    # Public — file capture (existing API, kept working)
    # ------------------------------------------------------------------

    def capture(self) -> dict[str, Any]:
        """Capture a full-res still to disk and return its path.

        VISION MODEL HOOK: The returned image_path is passed directly to
        VisionService.analyze_egg_image() / predict_stage() for inference.
        """
        path = self._get_image_path()
        try:
            data = self.snapshot_jpeg()
        except Exception as exc:  # noqa: BLE001
            logger.error("%s capture failed: %s", self.backend, exc)
            return {"ok": False, "error": str(exc), "backend": self.backend}
        try:
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            logger.error("Saving capture failed: %s", exc)
            return {"ok": False, "error": str(exc), "backend": self.backend}
        result = {"ok": True, "image_path": str(path), "backend": self.backend}
        if self.backend == "mock":
            result["mock"] = True
        return result

    def cleanup(self) -> None:
        with self._lock:
            if self._picam is not None:
                try:
                    self._picam.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._picam.close()
                except Exception:  # noqa: BLE001
                    pass
                self._picam = None
                self._still_config = None
            if self._cv_cap is not None:
                try:
                    self._cv_cap.release()
                except Exception:  # noqa: BLE001
                    pass
                self._cv_cap = None
