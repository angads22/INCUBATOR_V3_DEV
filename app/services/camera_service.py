"""
Camera service: Pi camera (picamera2), USB camera (OpenCV) or a mock.

Backends (``CAMERA_BACKEND``):
  auto       — picamera2 importable → picamera2; else cv2 importable and a
               ``/dev/video*`` node exists → opencv; else mock
  picamera2  — Pi Camera Module v1/v2/v3 via libcamera
  opencv     — USB webcam via OpenCV / cv2 (first of /dev/video0..3 that opens
               AND delivers a frame — on many SBCs /dev/video0 is a VPU node)
  mock       — Returns a placeholder image without touching hardware

``backend_resolved`` is the backend actually in use.

The session is PERSISTENT: the camera is configured and started once and kept
running. Two outputs are exposed:

  * a low-resolution (~640x480) preview frame at ~preview_fps for the MJPEG stream
  * a full-resolution still on demand (snapshot / capture)

The picamera2 preview pipeline is configured with ``FrameDurationLimits`` so
the sensor/ISP run at the preview rate (default 1 fps), not libcamera's 30 fps
default, and only two buffers — a 512 MB board cannot spare more.

The MJPEG generator ALWAYS yields something each interval: when the camera is
unavailable it serves mock frames at preview_fps. Because it never spins
without yielding, a client disconnect lets Starlette drop the generator and
free its threadpool worker; the stream can never pin a thread or hold up
shutdown.

Still images are stored under CAMERA_IMAGE_DIR (default: ./captures); transient
preview frames are kept in RAM (and optionally tmpfs), never written to the SD
card. The image path returned by ``capture()`` is used by the vision service.
"""

from __future__ import annotations

import datetime
import glob
import importlib.util
import io
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

# MJPEG multipart boundary token.
_BOUNDARY = "frame"

BACKENDS = ("auto", "picamera2", "opencv", "mock")
DEFAULT_VIDEO_DEVICES = ("/dev/video0", "/dev/video1", "/dev/video2", "/dev/video3")
# Preview buffers: picamera2 defaults to 6 for video; 2 is enough at <= 5 fps.
PREVIEW_BUFFER_COUNT = 2
# Sensor frame-duration bounds accepted when translating preview_fps (us).
_MIN_FRAME_US = 33_333          # 30 fps
_MAX_FRAME_US = 2_000_000       # 0.5 fps


def _importable(name: str) -> bool:
    """True when ``import name`` would succeed — WITHOUT importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def frame_duration_us(fps: float) -> int:
    """Translate a preview frame rate into a libcamera FrameDurationLimits value."""
    fps = max(0.01, float(fps))
    return int(max(_MIN_FRAME_US, min(_MAX_FRAME_US, round(1_000_000 / fps))))


class CameraService:
    def __init__(
        self,
        backend: str = "auto",
        image_dir: str = "./captures",
        resolution: tuple[int, int] = (1920, 1080),
        preview_resolution: tuple[int, int] = (640, 480),
        preview_fps: float = 1.0,
        frame_dir: str = "/run/incubator/frames",
        *,
        video_devices: "tuple[str, ...] | list[str] | None" = None,
        importable: Callable[[str], bool] = _importable,
    ) -> None:
        self.backend_requested = (backend or "auto").strip().lower()
        self.image_dir = Path(image_dir)
        self.resolution = resolution
        self.preview_resolution = preview_resolution
        self.preview_fps = max(0.2, float(preview_fps))
        self.frame_dir = Path(frame_dir)
        self.video_devices = tuple(video_devices) if video_devices is not None else DEFAULT_VIDEO_DEVICES
        self._importable = importable
        self._picam: Any = None
        self._still_config: Any = None
        self._cv_cap: Any = None
        self._cv_device: str | int | None = None
        # Serialises all hardware access — a snapshot and the preview stream can
        # run concurrently (FastAPI sync endpoints run in a threadpool).
        self._lock = threading.Lock()
        self._stream_failures = 0
        self._warned_stream_fallback = False
        self.active_streams = 0
        self.backend = self._resolve_backend(self.backend_requested)
        if self.backend_requested != self.backend:
            logger.info("camera: backend %r resolved to %r", self.backend_requested, self.backend)

    # ------------------------------------------------------------------
    # Backend resolution
    # ------------------------------------------------------------------

    def _existing_video_devices(self) -> list[str]:
        found = [d for d in self.video_devices if Path(d).exists()]
        if not found and self.video_devices == DEFAULT_VIDEO_DEVICES:
            found = sorted(glob.glob("/dev/video[0-9]*"))[:4]
        return found

    def _resolve_backend(self, requested: str) -> str:
        if requested in ("picamera2", "opencv", "mock"):
            return requested
        if requested != "auto":
            logger.warning("camera: unknown CAMERA_BACKEND=%r; choose one of %s — using auto", requested, ", ".join(BACKENDS))
        if self._importable("picamera2"):
            return "picamera2"
        if self._importable("cv2") and self._existing_video_devices():
            return "opencv"
        return "mock"

    @property
    def backend_resolved(self) -> str:
        return self.backend

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "backend_requested": self.backend_requested,
            "device": self._cv_device,
            "preview_fps": self.preview_fps,
            "active_streams": self.active_streams,
        }

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

    def _preview_controls(self) -> dict[str, Any]:
        us = frame_duration_us(self.preview_fps)
        return {"FrameDurationLimits": (us, us)}

    def _ensure_picam(self) -> Any:
        """Lazily create + start a persistent preview session. Returns the cam."""
        if self._picam is not None:
            return self._picam
        from picamera2 import Picamera2  # type: ignore

        cam = Picamera2()
        # Persistent low-res preview config — stays live for the MJPEG stream.
        # Run the sensor at the preview rate (not libcamera's 30 fps default)
        # with a small buffer pool; fall back to defaults if this libcamera
        # build rejects the controls.
        main = {"size": self.preview_resolution, "format": "RGB888"}
        try:
            preview_config = cam.create_video_configuration(
                main=main, controls=self._preview_controls(), buffer_count=PREVIEW_BUFFER_COUNT,
            )
        except TypeError:
            logger.info("camera: picamera2 rejected preview controls — using defaults")
            preview_config = cam.create_video_configuration(main=main)
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

    def _open_cv_device(self, cv2: Any, device: "str | int") -> Any:
        """Open ``device`` and return the capture only if it delivers a frame."""
        cap = cv2.VideoCapture(device)
        try:
            if not cap.isOpened():
                raise RuntimeError("not opened")
            ok, _frame = cap.read()
            if not ok:
                raise RuntimeError("no frame")
        except Exception:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
            return None
        return cap

    def _ensure_cv(self) -> Any:
        import cv2  # type: ignore

        if self._cv_cap is not None and self._cv_cap.isOpened():
            return self._cv_cap
        candidates: list[str | int] = list(self._existing_video_devices())
        if not candidates:
            candidates = [0]  # non-Linux / unusual layout: let OpenCV pick
        for device in candidates:
            cap = self._open_cv_device(cv2, device)
            if cap is None:
                logger.debug("camera: %s does not deliver frames — skipping", device)
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
            self._cv_cap = cap
            self._cv_device = device
            logger.info("camera: using %s via OpenCV", device)
            return cap
        raise RuntimeError(f"No USB camera found (tried {', '.join(str(c) for c in candidates)})")

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

    @staticmethod
    def _mjpeg_part(frame: bytes) -> bytes:
        return (
            b"--" + _BOUNDARY.encode() + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
            + frame + b"\r\n"
        )

    def mjpeg_stream(self, *, should_stop: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep) -> Iterator[bytes]:
        """Yield multipart/x-mixed-replace MJPEG chunks at ~preview_fps.

        A frame is yielded EVERY interval: when the camera fails (missing
        picamera2, unplugged webcam) a mock frame is served instead, so the
        generator never spins without yielding and a disconnected client always
        releases its worker thread at the next ``yield``. ``should_stop`` lets a
        caller end the stream cooperatively (tests, shutdown).
        """
        interval = 1.0 / self.preview_fps
        mock_frame: bytes | None = None
        self.active_streams += 1
        try:
            while not (should_stop and should_stop()):
                try:
                    frame = self.preview_jpeg()
                    self._stream_failures = 0
                except Exception as exc:  # noqa: BLE001 — a dropped frame must not kill the stream
                    self._stream_failures += 1
                    if not self._warned_stream_fallback:
                        self._warned_stream_fallback = True
                        logger.warning("camera: preview unavailable (%s) — streaming placeholder frames", exc)
                    else:
                        logger.debug("preview frame failed: %s", exc)
                    if mock_frame is None:
                        mock_frame = self._mock_jpeg(self.preview_resolution)
                    frame = mock_frame
                yield self._mjpeg_part(frame)
                sleep(interval)
        finally:
            self.active_streams = max(0, self.active_streams - 1)

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

    def available(self) -> bool:
        """Cheap probe: can this backend deliver a preview frame right now?"""
        try:
            return bool(self.preview_jpeg())
        except Exception:  # noqa: BLE001
            return False

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
                self._cv_device = None
