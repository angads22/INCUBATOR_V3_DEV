"""
AI/Vision API routes.

Endpoints:
  POST /api/vision/analyze          — Analyze an already-captured image
  POST /api/vision/candle           — Capture + analyze in one shot (candling flow)
  POST /api/ai/chat                 — LLM help chat
  POST /api/ai/explain-status       — LLM status explanation

VISION INTEGRATION NOTES
------------------------
The candle endpoint is the primary entry point for the egg candling workflow:
  1. Hardware triggers candle LED (set_candle ON)
  2. CameraService captures image
  3. VisionService.analyze_egg_image() runs inference
  4. Candle LED is turned off
  5. Result is returned (and optionally persisted to model_results table)

To swap in a cloud vision model (GPT-4V, Claude claude-sonnet-4-6, etc.) set VISION_BACKEND=api
and point VISION_API_URL at your proxy endpoint.  No code changes needed here.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ModelResult
from ..services.llm_service import LLMService
from ..services.vision_service import VisionService

if TYPE_CHECKING:
    from ..services.hardware_service import HardwareService

router = APIRouter()

# Populated at startup via set_runtime_services in web.py
_vision_service: VisionService = VisionService()  # default mock — replaced at startup
_hardware_service: "HardwareService | None" = None
llm_service = LLMService()


def set_vision_hardware(vision: VisionService, hardware: "HardwareService | None") -> None:
    """Called from main.py after services are wired."""
    global _vision_service, _hardware_service
    _vision_service = vision
    _hardware_service = hardware


# ------------------------------------------------------------------
# Request / response schemas
# ------------------------------------------------------------------

class VisionAnalyzeRequest(BaseModel):
    image_path: str
    mode: str = "egg"       # "egg" | "classify"
    egg_id: int | None = None


class CandleRequest(BaseModel):
    egg_id: int | None = None
    persist: bool = True    # Save result to model_results table


class AIChatRequest(BaseModel):
    question: str
    device_state: dict[str, Any] | None = None


class ExplainStatusRequest(BaseModel):
    device_state: dict[str, Any] | None = None


# ------------------------------------------------------------------
# Vision endpoints
# ------------------------------------------------------------------

@router.post("/api/vision/analyze")
def analyze_vision(payload: VisionAnalyzeRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Analyze an image that has already been captured and saved to disk."""
    result = (
        _vision_service.classify_image(payload.image_path)
        if payload.mode == "classify"
        else _vision_service.analyze_egg_image(payload.image_path)
    )
    if result.get("ok") and payload.egg_id and payload.mode != "classify":
        _persist_result(db, payload.egg_id, payload.image_path, result)
    return {"endpoint": "vision.analyze", **result}


@router.post("/api/vision/candle")
def candle_and_analyze(payload: CandleRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Full candling workflow:
      1. Turn candle LED on
      2. Capture image via camera
      3. Run vision inference
      4. Turn candle LED off  (always, even on failure)
      5. Optionally persist result

    VISION MODEL HOOK — this is the primary automated candling entry point.
    """
    if _hardware_service is None:
        return {"ok": False, "error": "Hardware service not available", "endpoint": "vision.candle"}

    # 1. Light up candling LED
    _hardware_service.set_candle(True)

    try:
        # 2. Capture image
        capture = _hardware_service.capture_image()
        if not capture.get("ok"):
            return {"ok": False, "error": capture.get("error", "Capture failed"), "endpoint": "vision.candle"}

        image_path = capture["image_path"]

        # 3. Run inference — VISION MODEL HOOK
        vision_result = _vision_service.analyze_egg_image(image_path)
    finally:
        # 4. Always turn off candle LED regardless of outcome
        _hardware_service.set_candle(False)

    # 5. Persist
    if payload.persist and vision_result.get("ok") and payload.egg_id:
        _persist_result(db, payload.egg_id, image_path, vision_result)

    return {
        "endpoint": "vision.candle",
        "image_path": image_path,
        **vision_result,
    }


# ------------------------------------------------------------------
# LLM endpoints
# ------------------------------------------------------------------

@router.post("/api/ai/chat")
def ai_chat(payload: AIChatRequest) -> dict[str, Any]:
    result = llm_service.answer_help_question(payload.question, payload.device_state)
    return {"endpoint": "ai.chat", **result}


@router.post("/api/ai/explain-status")
def explain_status(payload: ExplainStatusRequest) -> dict[str, Any]:
    result = llm_service.explain_status(payload.device_state)
    return {"endpoint": "ai.explain_status", **result}


# ------------------------------------------------------------------
# Internal
# ------------------------------------------------------------------

def _persist_result(db: Session, egg_id: int, image_path: str, result: dict) -> None:
    try:
        db.add(ModelResult(
            egg_id=egg_id,
            image_path=image_path,
            model_backend=result.get("backend", "unknown"),
            predicted_label=result.get("label", "unknown"),
            confidence=float(result.get("confidence", 0.0)),
            raw_output=__import__("json").dumps(result),
        ))
        db.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to persist model result: %s", exc)
