from __future__ import annotations

from typing import Any


class VisionService:
    """Vision inference integration surface for future local runtime models."""

    def classify_image(self, path: str) -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "message": "Vision model runtime is not configured yet.",
            "path": path,
        }

    def analyze_egg_image(self, path: str) -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "message": "Egg image analysis model is not configured yet.",
            "path": path,
        }
