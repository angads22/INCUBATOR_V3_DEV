from __future__ import annotations

from pathlib import Path
from typing import Any

from .vision import count_eggs


class VisionService:
    """Vision inference integration — delegates to the TFLite egg-counter."""

    def __init__(
        self,
        model_path: Path | None = None,
        labels_path: Path | None = None,
    ) -> None:
        from .vision import _DEFAULT_MODEL_PATH, _DEFAULT_LABELS_PATH

        self._model_path = model_path or _DEFAULT_MODEL_PATH
        self._labels_path = labels_path or _DEFAULT_LABELS_PATH

    def classify_image(self, path: str) -> dict[str, Any]:
        """General image classification — delegates to egg counter for now."""
        result = count_eggs(
            path,
            model_path=self._model_path,
            labels_path=self._labels_path,
        )
        return {**result, "configured": result["ok"]}

    def analyze_egg_image(self, path: str, confidence_threshold: float = 0.5) -> dict[str, Any]:
        """Run egg-counting inference on a single image.

        Returns structured data with ``count``, ``detections`` (bounding boxes +
        confidence), and ``image_path``.
        """
        result = count_eggs(
            path,
            confidence_threshold=confidence_threshold,
            model_path=self._model_path,
            labels_path=self._labels_path,
        )
        return {**result, "configured": result["ok"]}
