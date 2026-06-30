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

==============================================================================
INCUBATION-STAGE MODEL CONTRACT  (predict_stage / VISION_STAGE_BACKEND=tflite)
==============================================================================
Drop a trained model at VISION_STAGE_MODEL (default
/var/incubator/models/vision/stage.tflite) and set VISION_STAGE_BACKEND=tflite.
No code changes are needed if the model conforms to this contract:

  INPUT
    * A single image tensor, shape [1, H, W, 3] (NHWC), 3 = RGB channels.
    * H and W are read from the interpreter — train at whatever size you like
      (e.g. 224x224). The image is resized to (W, H) and RGB-ordered.
    * dtype float32 → pixels normalised to [0.0, 1.0].
    * dtype uint8   → quantised input; the service quantises using the tensor's
      (scale, zero_point): q = round(value / scale + zero_point), clipped 0..255.

  OUTPUT — one of two heads (auto-detected from the output tensor size):
    * REGRESSION head: output shape [1, 1] — a single number = the estimated
      incubation day. If the value is in [0, 1] it is treated as a fraction of
      INCUBATION_DAYS; otherwise as an absolute day count.
    * CLASSIFICATION head: output shape [1, N] — class scores. argmax indexes
      into stage_labels.txt (one stage name per line, sitting next to the
      .tflite). Each stage maps to a representative day + range (see
      STAGE_DAY_FRACTION). Recognised stage names:
        early, mid, late, hatching, infertile, unclear
    * uint8 outputs are dequantised: value = scale * (q - zero_point).

  RETURN (identical for every backend):
    {ok, backend, day_estimate: float, day_range: [lo, hi], stage: str,
     confidence: float, features: dict, path: str}
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Recognised incubation stages.
STAGES = ("early", "mid", "late", "hatching", "infertile", "unclear")

# Representative position of each stage within the incubation window (fraction
# of total days). Used to turn a classification label into a day estimate.
STAGE_DAY_FRACTION: dict[str, float] = {
    "infertile": 0.0,
    "unclear": 0.0,
    "early": 0.18,
    "mid": 0.45,
    "late": 0.78,
    "hatching": 0.97,
}

# How wide a day-range each stage spans (fraction of total days, ± each side).
_STAGE_SPREAD_FRACTION = 0.16

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
        stage_backend: str = "heuristic",
        stage_model_path: str = "/var/incubator/models/vision/stage.tflite",
        incubation_days: int = 21,
    ) -> None:
        # _configured_backend is what the operator asked for ("auto" detects a
        # dropped-in model); self.backend is what we resolve to at setup().
        self._configured_backend = backend
        self.backend = backend
        self.tflite_model_path = tflite_model_path
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.confidence_threshold = confidence_threshold
        self._interpreter: Any = None
        self._labels: list[str] = []
        self._tflite_ready = False
        self._tflite_attempted = False  # lazy-load guard: load on first command
        # --- Incubation-stage estimator (Testing tab) ---
        self.stage_backend = stage_backend
        self.stage_model_path = stage_model_path
        self.incubation_days = max(1, int(incubation_days))
        self._stage_interpreter: Any = None
        self._stage_labels: list[str] = []
        self._stage_loaded = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _resolve_backend(self) -> str:
        """Pick the backend at launch. "auto" (the default) is plug-and-play:
        a dropped-in TFLite model wins, else a configured API, else mock."""
        b = self._configured_backend
        if b == "auto":
            if Path(self.tflite_model_path).exists():
                return "tflite"
            if self.api_url:
                return "api"
            return "mock"
        return b

    def setup(self) -> None:
        """Resolve the backend at startup — but DO NOT load the model here.

        On a Pi Zero 2 W we keep idle CPU/RAM low: the TFLite interpreter is
        lazy-loaded on the first on-command inference (candle / analyze), and
        inference only ever runs on command — there is no background loop.
        """
        self.backend = self._resolve_backend()
        if self.backend == "api" and not self.api_url:
            logger.warning("VISION_BACKEND=api but VISION_API_URL is not set — falling back to mock")
            self.backend = "mock"
        model_present = Path(self.tflite_model_path).exists()
        logger.info(
            "Vision backend=%s (configured=%s, model_present=%s) — on-device inference is "
            "lazy + on-command only",
            self.backend, self._configured_backend, model_present,
        )

    def _ensure_tflite_loaded(self) -> bool:
        """Load the TFLite interpreter on first use (idempotent)."""
        if self._tflite_ready:
            return True
        if self._tflite_attempted:
            return False  # already tried and failed — don't thrash on every call
        self._tflite_attempted = True
        self._load_tflite()
        return self._tflite_ready

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

    def reload(self) -> dict[str, Any]:
        """Re-detect and reset models — call after dropping in / uploading one."""
        self._interpreter = None
        self._labels = []
        self._tflite_ready = False
        self._tflite_attempted = False
        self._stage_interpreter = None
        self._stage_labels = []
        self._stage_loaded = False
        self.setup()
        return self.status()

    def status(self) -> dict[str, Any]:
        """What's configured + available, without forcing a load."""
        cls_path = Path(self.tflite_model_path)
        stage_path = Path(self.stage_model_path)
        return {
            "ok": True,
            "configured_backend": self._configured_backend,
            "backend": self.backend,
            "ready": self.backend != "tflite" or self._tflite_ready or cls_path.exists(),
            "on_command_only": True,
            "classifier": {
                "path": str(cls_path),
                "available": cls_path.exists(),
                "loaded": self._tflite_ready,
                "labels": list(self._labels),
            },
            "api_configured": bool(self.api_url),
            "confidence_threshold": self.confidence_threshold,
            "stage": {
                "backend": self.stage_backend,
                "path": str(stage_path),
                "available": stage_path.exists(),
                "loaded": self._stage_interpreter is not None,
                "labels": list(self._stage_labels),
            },
            "incubation_days": self.incubation_days,
        }

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

        # On-command, lazy: load the interpreter the first time it's actually
        # needed (keeps the Pi idle until the operator asks for analysis).
        if self.backend == "tflite":
            if self._ensure_tflite_loaded():
                return self._analyze_tflite(image_path)
            return self._mock_result(image_path)  # model unavailable → safe default
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

    # ==================================================================
    # Incubation-stage estimation (Testing tab) — predict_stage()
    # ==================================================================

    def predict_stage(self, image_path: str) -> dict[str, Any]:
        """Estimate how far along an egg is.

        Returns: {ok, backend, day_estimate: float, day_range: [lo, hi],
                  stage: str, confidence: float, features: dict, path: str}
        stage ∈ {"early","mid","late","hatching","infertile","unclear"}

        VISION MODEL HOOK: drop a TFLite model in and set VISION_STAGE_BACKEND
        =tflite (see the model contract at the top of this file). The default
        'heuristic' backend works today with no trained model.
        """
        backend = self.stage_backend
        if backend == "mock":
            return self._predict_stage_mock(image_path)
        if backend == "tflite":
            result = self._predict_stage_tflite(image_path)
            if result is not None:
                return result
            # Model missing/unloadable — fall back to the heuristic so the
            # Testing tab always returns a prediction.
            logger.warning("Stage TFLite unavailable — falling back to heuristic")
        return self._predict_stage_heuristic(image_path)

    # -- heuristic backend (classical CV on a candled image) -----------

    def _predict_stage_heuristic(self, image_path: str) -> dict[str, Any]:
        try:
            features = extract_candle_features(image_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Heuristic feature extraction failed: %s", exc)
            return {
                "ok": False, "backend": "heuristic", "error": str(exc),
                "day_estimate": 0.0, "day_range": [0.0, 0.0], "stage": "unclear",
                "confidence": 0.0, "features": {}, "path": image_path,
            }
        mapped = heuristic_day_from_features(features, self.incubation_days)
        return {
            "ok": True,
            "backend": "heuristic",
            "day_estimate": mapped["day_estimate"],
            "day_range": mapped["day_range"],
            "stage": mapped["stage"],
            "confidence": mapped["confidence"],
            "features": features,
            "path": image_path,
        }

    # -- mock backend (fixed result for dev/CI) ------------------------

    def _predict_stage_mock(self, image_path: str) -> dict[str, Any]:
        day = round(self.incubation_days * 0.45, 1)
        spread = round(self.incubation_days * _STAGE_SPREAD_FRACTION, 1)
        return {
            "ok": True,
            "backend": "mock",
            "mock": True,
            "day_estimate": day,
            "day_range": [max(0.0, day - spread), min(float(self.incubation_days), day + spread)],
            "stage": "mid",
            "confidence": 0.5,
            "features": {"opaque_fraction": 0.45, "air_cell_fraction": 0.2, "vein_density": 0.3, "brightness_mean": 0.5},
            "path": image_path,
        }

    # -- tflite backend (dropped-in trained model) ---------------------

    def _load_stage_tflite(self) -> bool:
        if self._stage_loaded:
            return self._stage_interpreter is not None
        self._stage_loaded = True
        model_path = Path(self.stage_model_path)
        if not model_path.exists():
            logger.info("Stage model not found at %s", model_path)
            return False
        try:
            import tflite_runtime.interpreter as tflite  # type: ignore
        except ImportError:
            try:
                from tensorflow.lite.python.interpreter import Interpreter as _TFInterp  # type: ignore

                class tflite:  # type: ignore
                    Interpreter = _TFInterp
            except Exception:
                logger.warning("tflite_runtime not installed — cannot load stage model")
                return False
        try:
            interp = tflite.Interpreter(model_path=str(model_path))
            interp.allocate_tensors()
            self._stage_interpreter = interp
            labels_path = model_path.with_name("stage_labels.txt")
            if labels_path.exists():
                self._stage_labels = [
                    l.strip() for l in labels_path.read_text().splitlines() if l.strip()
                ]
            logger.info("Stage TFLite model loaded from %s", model_path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Stage TFLite load failed: %s", exc)
            self._stage_interpreter = None
            return False

    def _predict_stage_tflite(self, image_path: str) -> dict[str, Any] | None:
        if not self._load_stage_tflite() or self._stage_interpreter is None:
            return None
        try:
            import numpy as np  # type: ignore
            from PIL import Image  # type: ignore

            interp = self._stage_interpreter
            inp = interp.get_input_details()[0]
            out = interp.get_output_details()[0]
            _, h, w, _ = inp["shape"]

            img = Image.open(image_path).convert("RGB").resize((int(w), int(h)))
            arr = np.asarray(img)

            if inp["dtype"] == np.uint8:
                scale, zero = inp.get("quantization", (0.0, 0))
                if scale and scale > 0:
                    data = np.clip(np.round((arr / 255.0) / scale + zero), 0, 255).astype(np.uint8)
                else:
                    data = arr.astype(np.uint8)
            else:
                data = (arr.astype(np.float32) / 255.0)
            data = np.expand_dims(data, axis=0)

            interp.set_tensor(inp["index"], data)
            interp.invoke()
            output = interp.get_tensor(out["index"])[0]

            if out["dtype"] == np.uint8:
                scale, zero = out.get("quantization", (0.0, 0))
                if scale and scale > 0:
                    output = scale * (output.astype(np.float32) - zero)

            output = np.asarray(output).flatten()
            return self._stage_from_tflite_output(output, image_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Stage TFLite inference failed: %s", exc)
            return {
                "ok": False, "backend": "tflite", "error": str(exc),
                "day_estimate": 0.0, "day_range": [0.0, 0.0], "stage": "unclear",
                "confidence": 0.0, "features": {}, "path": image_path,
            }

    def _stage_from_tflite_output(self, output: Any, image_path: str) -> dict[str, Any]:
        days = self.incubation_days
        if len(output) == 1:
            # Regression head — single day value.
            raw = float(output[0])
            day = raw * days if 0.0 <= raw <= 1.0 else raw
            day = max(0.0, min(float(days), day))
            stage = stage_from_day(day, days)
            spread = max(1.0, (1.0 - 0.7) * days * 0.3)
            return {
                "ok": True, "backend": "tflite", "day_estimate": round(day, 1),
                "day_range": [round(max(0.0, day - spread), 1), round(min(float(days), day + spread), 1)],
                "stage": stage, "confidence": 0.7,
                "features": {"head": "regression", "raw_output": raw}, "path": image_path,
            }
        # Classification head — argmax over stage labels.
        labels = self._stage_labels or list(STAGES)
        idx = int(max(range(len(output)), key=lambda i: output[i]))
        # Softmax-ish confidence: top score over the sum of (clipped) scores.
        total = sum(max(0.0, float(s)) for s in output) or 1.0
        confidence = round(min(0.99, max(0.0, float(output[idx])) / total), 4)
        stage = labels[idx] if idx < len(labels) else "unclear"
        if stage not in STAGES:
            stage = "unclear"
        day, day_range = stage_to_day_range(stage, days)
        return {
            "ok": True, "backend": "tflite", "day_estimate": day, "day_range": day_range,
            "stage": stage, "confidence": confidence,
            "features": {"head": "classification", "scores": [round(float(s), 4) for s in output]},
            "path": image_path,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _image_valid(self, path: str) -> bool:
        p = Path(path)
        return p.exists() and p.stat().st_size >= _MIN_IMAGE_BYTES


# ======================================================================
# Pure functions — no I/O beyond reading the image in extract_candle_features.
# Unit-tested directly (heuristic mapping, stage boundaries).
# ======================================================================

def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def stage_from_day(day: float, incubation_days: int) -> str:
    """Developmental stage from an estimated day (no infertile/unclear here)."""
    f = day / incubation_days if incubation_days else 0.0
    if f < 0.30:
        return "early"
    if f < 0.62:
        return "mid"
    if f < 0.88:
        return "late"
    return "hatching"


def stage_to_day_range(stage: str, incubation_days: int) -> tuple[float, list[float]]:
    """Representative day estimate + [lo, hi] range for a named stage."""
    frac = STAGE_DAY_FRACTION.get(stage, 0.0)
    day = round(frac * incubation_days, 1)
    spread = round(_STAGE_SPREAD_FRACTION * incubation_days, 1)
    lo = round(max(0.0, day - spread), 1)
    hi = round(min(float(incubation_days), day + spread), 1)
    if stage in ("infertile", "unclear"):
        lo, hi = 0.0, round(min(float(incubation_days), spread), 1)
    return day, [lo, hi]


def heuristic_day_from_features(features: dict[str, Any], incubation_days: int = 21) -> dict[str, Any]:
    """Map candling features to a day estimate, range, stage and confidence.

    Pure and deterministic. ``features`` keys (all 0..1, missing → 0):
      opaque_fraction   — dark embryo mass; grows with day
      air_cell_fraction — bright air cell at the blunt end; grows with day
      vein_density      — edge/vein density; peaks mid-incubation
      brightness_mean   — overall transmitted light; falls with day

    Returns {day_estimate, day_range:[lo,hi], stage, confidence}.
    """
    days = max(1, int(incubation_days))
    opaque = _clamp01(float(features.get("opaque_fraction", 0.0)))
    air = _clamp01(float(features.get("air_cell_fraction", 0.0)))
    veins = _clamp01(float(features.get("vein_density", 0.0)))
    brightness = _clamp01(float(features.get("brightness_mean", 0.0)))

    signal = max(opaque, air, veins)

    # No usable structure at all → unclear (blank / blown-out / underexposed).
    if signal < 0.04:
        spread = round(days * _STAGE_SPREAD_FRACTION, 1)
        return {"day_estimate": 0.0, "day_range": [0.0, spread], "stage": "unclear", "confidence": 0.2}

    # Clear egg: translucent, negligible mass and veins → infertile (day 0).
    if opaque < 0.07 and veins < 0.06 and air < 0.10:
        spread = round(days * _STAGE_SPREAD_FRACTION, 1)
        confidence = round(min(0.8, 0.4 + 0.3 * brightness), 4)
        return {"day_estimate": 0.0, "day_range": [0.0, spread], "stage": "infertile", "confidence": confidence}

    # Development grows with opaque embryo mass and air-cell size.
    development = _clamp01(0.65 * opaque + 0.35 * air)
    day = round(development * days, 1)
    stage = stage_from_day(day, days)

    confidence = round(min(0.95, max(0.2, 0.30 + 0.55 * signal + 0.15 * veins)), 4)
    spread = round((1.0 - confidence) * days * 0.4 + 1.0, 1)
    lo = round(max(0.0, day - spread), 1)
    hi = round(min(float(days), day + spread), 1)
    return {"day_estimate": day, "day_range": [lo, hi], "stage": stage, "confidence": confidence}


def extract_candle_features(image_path: str) -> dict[str, float]:
    """Classical-CV feature extraction from a candled egg image (Pillow only).

    Returns the 0..1 feature dict consumed by heuristic_day_from_features.
    Uses only Pillow + stdlib so it runs with no numpy/OpenCV and no GPU.
    """
    from PIL import Image, ImageFilter  # type: ignore

    # Grayscale + downscale for stable, fast statistics.
    img = Image.open(image_path).convert("L").resize((128, 128))
    hist = img.histogram()  # 256 bins
    total = float(sum(hist)) or 1.0

    # Opaque embryo mass: darker pixels (transmitted light is blocked).
    opaque = sum(hist[0:96]) / total
    # Air cell / clear transmission: the brightest pixels.
    air_cell = sum(hist[205:256]) / total
    # Mean brightness (0..1).
    brightness = sum(i * c for i, c in enumerate(hist)) / total / 255.0

    # Vein / structure density via edge magnitude.
    edges = img.filter(ImageFilter.FIND_EDGES)
    ehist = edges.histogram()
    etotal = float(sum(ehist)) or 1.0
    edge_mean = sum(i * c for i, c in enumerate(ehist)) / etotal / 255.0
    veins = min(1.0, edge_mean * 5.0)

    return {
        "opaque_fraction": round(_clamp01(opaque), 4),
        "air_cell_fraction": round(_clamp01(air_cell), 4),
        "vein_density": round(_clamp01(veins), 4),
        "brightness_mean": round(_clamp01(brightness), 4),
    }
