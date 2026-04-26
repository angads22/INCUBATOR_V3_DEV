"""
Camera service for Raspberry Pi Zero 2W.

Backends (set CAMERA_BACKEND env var):
  picamera2  — Pi Camera Module v1/v2/v3 via libcamera (default)
  opencv     — USB webcam via OpenCV / cv2
  mock       — Returns a placeholder path without touching hardware

Captured images are stored under CAMERA_IMAGE_DIR (default: ./captures).
The image path returned is used by the vision service for analysis.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CameraService:
    def __init__(self, backend: str = "picamera2", image_dir: str = "./captures",
                 resolution: tuple[int, int] = (1920, 1080)) -> None:
        self.backend = backend
        self.image_dir = Path(image_dir)
        self.resolution = resolution
        self._picam: Any = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_image_path(self) -> Path:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:6]
        return self.image_dir / f"capture_{ts}_{uid}.jpg"

    def _capture_picamera2(self, path: Path) -> dict[str, Any]:
        try:
            from picamera2 import Picamera2  # type: ignore

            if self._picam is None:
                self._picam = Picamera2()
                cfg = self._picam.create_still_configuration(
                    main={"size": self.resolution}
                )
                self._picam.configure(cfg)
            self._picam.start()
            self._picam.capture_file(str(path))
            self._picam.stop()
            return {"ok": True, "image_path": str(path), "backend": "picamera2"}
        except Exception as exc:
            logger.error("PiCamera2 capture failed: %s", exc)
            return {"ok": False, "error": str(exc), "backend": "picamera2"}

    def _capture_opencv(self, path: Path) -> dict[str, Any]:
        try:
            import cv2  # type: ignore

            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return {"ok": False, "error": "No USB camera found", "backend": "opencv"}
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return {"ok": False, "error": "Frame capture failed", "backend": "opencv"}
            cv2.imwrite(str(path), frame)
            return {"ok": True, "image_path": str(path), "backend": "opencv"}
        except Exception as exc:
            logger.error("OpenCV capture failed: %s", exc)
            return {"ok": False, "error": str(exc), "backend": "opencv"}

    def _capture_mock(self, path: Path) -> dict[str, Any]:
        # Write a deterministic placeholder large enough for downstream
        # vision-service minimum-size validation (_MIN_IMAGE_BYTES = 512).
        path.write_bytes(b"MOCK_IMAGE" * 64)
        return {"ok": True, "image_path": str(path), "backend": "mock", "mock": True}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def capture(self) -> dict[str, Any]:
        """Capture a still image and return its path.

        VISION MODEL HOOK: The returned image_path is passed directly to
        VisionService.analyze_egg_image() for inference.
        """
        path = self._get_image_path()
        if self.backend == "picamera2":
            return self._capture_picamera2(path)
        if self.backend == "opencv":
            return self._capture_opencv(path)
        return self._capture_mock(path)

    def cleanup(self) -> None:
        if self._picam is not None:
            try:
                self._picam.close()
            except Exception:
                pass
            self._picam = None
