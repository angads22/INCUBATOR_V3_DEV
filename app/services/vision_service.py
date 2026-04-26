"""
Vision model service — supports three backends:

  mock    — Returns structured placeholder data.  Safe for dev/CI.
  tflite  — Loads a TFLite flatbuffer from VISION_TFLITE_MODEL_PATH and runs
             inference locally on the Pi Zero 2W CPU.  Suitable for small
             MobileNet / EfficientLite classification models (egg fertility,
             candling stage, crack detection).
  api     — POSTs the image (base64) to VISION_API_URL.  Use this to point at
             a remote model server (e.g. a cloud GPU running YOLO / GPT-4V).

INTEGRATION GUIDE
-----------------
To connect a real vision model:

  Option A — TFLite (on-device):
    1. Train or download a model for egg classification.
    2. Convert to TFLite: `tflite_convert --saved_model_dir=... --output_file=model.tflite`
    3. Set VISION_BACKEND=tflite and VISION_TFLITE_MODEL=./models/vision/model.tflite
    4. Update VISION_LABELS below or set VISION_LABELS_PATH to a labels.txt file.

  Option B — Remote API:
    1. Deploy any vision endpoint that accepts {"image_b64": "...", "mode": "..."}
       and returns {"label": "...", "confidence": 0.95, "details": {...}}.
    2. Set VISION_BACKEND=api, VISION_API_URL=https://..., VISION_API_KEY=...

  Option C — OpenAI / Claude Vision (cloud):
    1. See Option B above — wrap GPT-4V or Claude claude-sonnet-4-6 in a thin FastAPI
       proxy that conforms to the schema above.
    2. Alternatively wire directly here in _analyze_via_api() using the Anthropic
       or OpenAI SDK.  The image_path is already available.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Labels used by the TFLite classifier.  Replace with your own label file.
_DEFAULT_LABELS = [
    "fertile",
    "infertile",
    "blood_ring",
    "dead_embryo",
    "crack",
    "unknown",
]

# Minimum pixel dimension — images smaller than this are likely noise
_MIN_IMAGE_BYTES = 512


class VisionService:
    def __init__(
        self,
        backend: str = "mock",
        tflite_model_path: str = "./models/vision/model.tflite",
        api_url: str = "",
        api_key: str = "",
        confidence_threshold: float = 0.65,
    ) -> None:
        self.backend = backend
        self.tflite_model_path = tflite_model_path
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.confidence_threshold = confidence_threshold
        self._interpreter: Any = None
        self._labels: list[str] = []
        self._tflite_ready = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Load model if needed.  Called once at startup."""
        if self.backend == "tflite":
            self._load_tflite()
        elif self.backend == "api" and not self.api_url:
            logger.warning("VISION_BACKEND=api but VISION_API_URL is not set — falling back to mock")
            self.backend = "mock"

    def _load_tflite(self) -> None:
        model_path = Path(self.tflite_model_path)
        if not model_path.exists():
            logger.warning("TFLite model not found at %s — vision will return mock results", model_path)
            return
        try:
            import tflite_runtime.interpreter as tflite  # type: ignore

            self._interpreter = tflite.Interpreter(model_path=str(model_path))
            self._interpreter.allocate_tensors()
            self._load_labels(model_path)
            self._tflite_ready = True
            logger.info("TFLite model loaded from %s (%d labels)", model_path, len(self._labels))
        except ImportError:
            logger.warning("tflite_runtime not installed — install with: pip install tflite-runtime")
        except Exception as exc:
            logger.error("TFLite model load failed: %s", exc)

    def _load_labels(self, model_path: Path) -> None:
        labels_path = model_path.with_suffix(".txt")
        if labels_path.exists():
            self._labels = [l.strip() for l in labels_path.read_text().splitlines() if l.strip()]
        else:
            self._labels = _DEFAULT_LABELS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_egg_image(self, image_path: str) -> dict[str, Any]:
        """Main entry point — classify a captured egg image.

        VISION MODEL HOOK: This is where real inference happens.
        Called from routes/ai.py and from the auto-candle-and-analyze flow.
        """
        # Skip size validation in mock mode — mock captures intentionally write
        # small placeholder bytes and the mock backend doesn't read the file.
        if self.backend != "mock" and not self._image_valid(image_path):
            return {"ok": False, "error": "Image file missing or too small", "path": image_path}

        if self.backend == "tflite" and self._tflite_ready:
            return self._analyze_tflite(image_path)
        if self.backend == "api" and self.api_url:
            return self._analyze_via_api(image_path)
        return self._mock_result(image_path)

    def classify_image(self, image_path: str) -> dict[str, Any]:
        """Generic image classification — same pipeline as egg analysis."""
        return self.analyze_egg_image(image_path)

    # ------------------------------------------------------------------
    # Backends
    # ------------------------------------------------------------------

    def _analyze_tflite(self, image_path: str) -> dict[str, Any]:
        try:
            import numpy as np  # type: ignore
            from PIL import Image  # type: ignore

            inp_details = self._interpreter.get_input_details()
            out_details = self._interpreter.get_output_details()
            h, w = inp_details[0]["shape"][1], inp_details[0]["shape"][2]

            img = Image.open(image_path).convert("RGB").resize((w, h))
            inp_data = np.expand_dims(np.array(img, dtype=np.float32) / 255.0, axis=0)

            self._interpreter.set_tensor(inp_details[0]["index"], inp_data)
            self._interpreter.invoke()
            output = self._interpreter.get_tensor(out_details[0]["index"])[0]

            top_idx = int(output.argmax())
            confidence = float(output[top_idx])
            label = self._labels[top_idx] if top_idx < len(self._labels) else "unknown"

            return {
                "ok": True,
                "backend": "tflite",
                "label": label,
                "confidence": round(confidence, 4),
                "above_threshold": confidence >= self.confidence_threshold,
                "all_scores": {
                    self._labels[i] if i < len(self._labels) else str(i): round(float(s), 4)
                    for i, s in enumerate(output)
                },
                "path": image_path,
            }
        except Exception as exc:
            logger.error("TFLite inference failed: %s", exc)
            return {"ok": False, "backend": "tflite", "error": str(exc), "path": image_path}

    def _analyze_via_api(self, image_path: str) -> dict[str, Any]:
        """POST image to a remote vision API endpoint.

        VISION API HOOK: Replace or extend this method to call your model
        server, GPT-4V, Claude claude-sonnet-4-6 Vision, Roboflow, etc.
        Expected response shape: {"label": str, "confidence": float, "details": dict}
        """
        try:
            import urllib.request
            import json

            img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
            body = json.dumps({"image_b64": img_b64, "mode": "egg"}).encode()
            req = urllib.request.Request(
                self.api_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            return {
                "ok": True,
                "backend": "api",
                "label": data.get("label", "unknown"),
                "confidence": float(data.get("confidence", 0.0)),
                "above_threshold": float(data.get("confidence", 0.0)) >= self.confidence_threshold,
                "details": data.get("details", {}),
                "path": image_path,
            }
        except Exception as exc:
            logger.error("Vision API call failed: %s", exc)
            return {"ok": False, "backend": "api", "error": str(exc), "path": image_path}

    def _mock_result(self, image_path: str) -> dict[str, Any]:
        return {
            "ok": True,
            "backend": "mock",
            "mock": True,
            "label": "fertile",
            "confidence": 0.82,
            "above_threshold": True,
            "message": "Vision model not configured. Set VISION_BACKEND=tflite or VISION_BACKEND=api.",
            "path": image_path,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _image_valid(self, path: str) -> bool:
        p = Path(path)
        return p.exists() and p.stat().st_size >= _MIN_IMAGE_BYTES
