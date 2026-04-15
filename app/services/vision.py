"""TFLite egg-counting inference module.

Loads a local TensorFlow Lite object-detection model **once** and exposes a
single public function ``count_eggs`` that runs inference on a still image and
returns structured results (count, bounding boxes, confidence scores).

Designed for low-end Linux hardware (Raspberry Pi Zero 2 W) — no video
streaming, single-image inference only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default model paths (relative to project root)
# ---------------------------------------------------------------------------
_DEFAULT_MODEL_PATH = Path("ai/models/egg_counter.tflite")
_DEFAULT_LABELS_PATH = Path("ai/models/labels.txt")

# ---------------------------------------------------------------------------
# Lazy-loaded global interpreter
# ---------------------------------------------------------------------------
_interpreter: Any | None = None
_labels: list[str] = []
_model_loaded: bool = False
_load_error: str | None = None


@dataclass(frozen=True)
class Detection:
    """A single detected egg."""

    label: str
    confidence: float
    bbox: tuple[float, float, float, float]  # ymin, xmin, ymax, xmax (normalised)


@dataclass(frozen=True)
class EggCountResult:
    """Structured result from ``count_eggs``."""

    ok: bool
    count: int = 0
    detections: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    image_path: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_labels(path: Path) -> list[str]:
    """Read one label per line; return empty list on failure."""
    try:
        return [line.strip() for line in path.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        logger.warning("Labels file not found: %s — defaulting to ['egg']", path)
        return ["egg"]
    except Exception:
        logger.exception("Failed to read labels from %s", path)
        return ["egg"]


def _ensure_interpreter(
    model_path: Path = _DEFAULT_MODEL_PATH,
    labels_path: Path = _DEFAULT_LABELS_PATH,
) -> None:
    """Load the TFLite interpreter exactly once (lazy singleton)."""
    global _interpreter, _labels, _model_loaded, _load_error  # noqa: PLW0603

    if _model_loaded or _load_error is not None:
        return  # already attempted

    resolved_model = model_path if model_path.is_absolute() else Path.cwd() / model_path
    resolved_labels = labels_path if labels_path.is_absolute() else Path.cwd() / labels_path

    if not resolved_model.exists():
        _load_error = f"Model file not found: {resolved_model}"
        logger.warning(_load_error)
        return

    try:
        # tflite_runtime is the lightweight wheel for Pi; fall back to full TF.
        try:
            from tflite_runtime.interpreter import Interpreter  # type: ignore[import-untyped]
        except ImportError:
            from tensorflow.lite.python.interpreter import Interpreter  # type: ignore[import-untyped]

        _interpreter = Interpreter(model_path=str(resolved_model))
        _interpreter.allocate_tensors()
        _labels = _load_labels(resolved_labels)
        _model_loaded = True
        logger.info("TFLite egg-counter model loaded from %s (%d labels)", resolved_model, len(_labels))
    except Exception as exc:
        _load_error = f"Failed to load TFLite model: {exc}"
        logger.exception(_load_error)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def count_eggs(
    image_path: str | Path,
    confidence_threshold: float = 0.5,
    model_path: Path = _DEFAULT_MODEL_PATH,
    labels_path: Path = _DEFAULT_LABELS_PATH,
) -> dict[str, Any]:
    """Run egg-counting inference on a single image.

    Parameters
    ----------
    image_path:
        Path to a JPEG/PNG image file.
    confidence_threshold:
        Minimum confidence score to count a detection (0-1).
    model_path / labels_path:
        Override default model location (useful for tests).

    Returns
    -------
    dict
        ``{"ok": True, "count": N, "detections": [...], "image_path": "..."}``
        on success, or ``{"ok": False, "message": "...", ...}`` on error.
    """
    _ensure_interpreter(model_path, labels_path)

    if _load_error is not None:
        return {
            "ok": False,
            "count": 0,
            "detections": [],
            "message": _load_error,
            "image_path": str(image_path),
        }

    assert _interpreter is not None  # ensured by _model_loaded == True

    img_path = Path(image_path)
    if not img_path.exists():
        return {
            "ok": False,
            "count": 0,
            "detections": [],
            "message": f"Image file not found: {image_path}",
            "image_path": str(image_path),
        }

    try:
        import numpy as np
        from PIL import Image  # Pillow — lightweight, works on Pi

        # Determine model input size from the interpreter.
        input_details = _interpreter.get_input_details()
        output_details = _interpreter.get_output_details()
        _, height, width, _ = input_details[0]["shape"]

        # Pre-process: resize, convert to uint8/float as needed.
        img = Image.open(img_path).convert("RGB").resize((width, height))
        input_data = np.expand_dims(np.array(img), axis=0)

        if input_details[0]["dtype"] == np.float32:
            input_data = (input_data / 255.0).astype(np.float32)
        else:
            input_data = input_data.astype(input_details[0]["dtype"])

        _interpreter.set_tensor(input_details[0]["index"], input_data)
        _interpreter.invoke()

        # SSD-style output tensors: boxes, classes, scores, count.
        boxes = _interpreter.get_tensor(output_details[0]["index"])[0]
        classes = _interpreter.get_tensor(output_details[1]["index"])[0]
        scores = _interpreter.get_tensor(output_details[2]["index"])[0]
        num_detections = int(_interpreter.get_tensor(output_details[3]["index"])[0])

        detections: list[dict[str, Any]] = []
        for i in range(num_detections):
            score = float(scores[i])
            if score < confidence_threshold:
                continue
            class_idx = int(classes[i])
            label = _labels[class_idx] if class_idx < len(_labels) else f"class_{class_idx}"
            ymin, xmin, ymax, xmax = (float(v) for v in boxes[i])
            detections.append(
                {
                    "label": label,
                    "confidence": round(score, 4),
                    "bbox": {"ymin": ymin, "xmin": xmin, "ymax": ymax, "xmax": xmax},
                }
            )

        return {
            "ok": True,
            "count": len(detections),
            "detections": detections,
            "message": "",
            "image_path": str(image_path),
        }

    except Exception as exc:
        logger.exception("Inference failed for %s", image_path)
        return {
            "ok": False,
            "count": 0,
            "detections": [],
            "message": f"Inference error: {exc}",
            "image_path": str(image_path),
        }
