from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services.llm_service import LLMService
from ..services.vision_service import VisionService

router = APIRouter()
vision_service = VisionService()
llm_service = LLMService()


class VisionAnalyzeRequest(BaseModel):
    image_path: str
    mode: str = "egg"


class EggCountRequest(BaseModel):
    image_path: str
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class AIChatRequest(BaseModel):
    question: str
    device_state: dict[str, Any] | None = None


class ExplainStatusRequest(BaseModel):
    device_state: dict[str, Any] | None = None


@router.post("/api/vision/analyze")
def analyze_vision(payload: VisionAnalyzeRequest) -> dict[str, Any]:
    result = (
        vision_service.classify_image(payload.image_path)
        if payload.mode == "classify"
        else vision_service.analyze_egg_image(payload.image_path)
    )
    return {"endpoint": "vision.analyze", **result}


@router.post("/api/vision/count-eggs")
def count_eggs_endpoint(payload: EggCountRequest) -> dict[str, Any]:
    """Egg-counting endpoint — returns count, bounding boxes, and confidence scores."""
    result = vision_service.analyze_egg_image(
        payload.image_path,
        confidence_threshold=payload.confidence_threshold,
    )
    return {"endpoint": "vision.count_eggs", **result}


@router.post("/api/ai/chat")
def ai_chat(payload: AIChatRequest) -> dict[str, Any]:
    result = llm_service.answer_help_question(payload.question, payload.device_state)
    return {"endpoint": "ai.chat", **result}


@router.post("/api/ai/explain-status")
def explain_status(payload: ExplainStatusRequest) -> dict[str, Any]:
    result = llm_service.explain_status(payload.device_state)
    return {"endpoint": "ai.explain_status", **result}
