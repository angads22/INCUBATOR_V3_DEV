"""
AI/Vision API routes.

Endpoints:
  POST /api/vision/analyze          — Analyze an already-captured image
  POST /api/vision/candle           — Capture + analyze in one shot (candling flow)
  GET  /api/vision/status           — What model is configured/available/loaded
  GET  /api/vision/results          — Recent candling analyses
  POST /api/vision/reload           — Re-detect a model dropped onto the device
  POST /api/vision/model            — Upload a .tflite (+labels) — plug and play
  POST /api/ai/chat                 — LLM help chat
  POST /api/ai/explain-status       — LLM status explanation

Inference is ON-COMMAND ONLY (no background loop) and the model is lazy-loaded
on first use, so an idle Pi Zero isn't taxed.

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

from pathlib import Path
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, has_any_user
from ..config import settings
from ..database import get_db
from ..models import ModelResult
from ..services.llm_service import LLMService
from ..services.vision_service import VisionService

if TYPE_CHECKING:
    from ..services.hardware_service import HardwareService
    from ..services.storage_service import StorageService
    from ..services.growth_service import GrowthService

router = APIRouter()


def _require_user(db: Session, session_token: str | None) -> None:
    """Gate model management once an operator account exists (mirrors web routes)."""
    if (settings.require_login or has_any_user(db)) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

# Populated at startup via set_runtime_services in web.py
_vision_service: VisionService = VisionService()  # default mock — replaced at startup
_hardware_service: "HardwareService | None" = None
_storage_service: "StorageService | None" = None
_growth_service: "GrowthService | None" = None
llm_service = LLMService()


def set_growth_service(growth: "GrowthService | None") -> None:
    """Wire the growth engine so candling records development + drives actions."""
    global _growth_service
    _growth_service = growth


def set_vision_hardware(vision: VisionService, hardware: "HardwareService | None") -> None:
    """Called from main.py after services are wired."""
    global _vision_service, _hardware_service
    _vision_service = vision
    _hardware_service = hardware


def set_storage_service(storage: "StorageService | None") -> None:
    """Wire the egg-photo store so candling captures are saved + auto-pruned."""
    global _storage_service
    _storage_service = storage


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

    # 5. Persist the classifier result.
    if payload.persist and vision_result.get("ok") and payload.egg_id:
        _persist_result(db, payload.egg_id, image_path, vision_result)

    # 6. Store the candling photo as a labeled, auto-pruned egg photo so the
    #    captured frame is kept (and the SD card protected) without extra steps.
    if payload.persist and _storage_service is not None and image_path:
        try:
            _storage_service.register(
                db,
                image_path,
                label=str(vision_result.get("label", "egg")),
                egg_id=payload.egg_id,
                backend=str(vision_result.get("backend", "unknown")),
                confidence=float(vision_result.get("confidence", 0.0) or 0.0),
            )
        except Exception as exc:  # noqa: BLE001 — storage must never break candling
            import logging

            logging.getLogger(__name__).warning("Could not store candling photo: %s", exc)

    # 7. Estimate how far along the egg is and record it on the growth timeline,
    #    then assess development and (optionally) drive incubator actions.
    stage_result = _vision_service.predict_stage(image_path)
    growth: dict[str, Any] | None = None
    if _growth_service is not None and payload.egg_id and stage_result.get("ok"):
        try:
            _growth_service.record(
                db,
                payload.egg_id,
                day_estimate=float(stage_result.get("day_estimate", 0.0) or 0.0),
                stage=str(stage_result.get("stage", "unclear")),
                confidence=float(stage_result.get("confidence", 0.0) or 0.0),
                label=vision_result.get("label"),
                backend=str(stage_result.get("backend", "unknown")),
            )
            if _growth_service.auto_actions:
                growth = _growth_service.apply_actions(db, payload.egg_id, _hardware_service)
            else:
                growth = _growth_service.assess(db, payload.egg_id)
        except Exception as exc:  # noqa: BLE001 — growth must never break candling
            import logging

            logging.getLogger(__name__).warning("Growth tracking failed: %s", exc)

    return {
        "endpoint": "vision.candle",
        "image_path": image_path,
        "stage": stage_result,
        "growth": growth,
        **vision_result,
    }


# ------------------------------------------------------------------
# Vision model management — plug and play
# ------------------------------------------------------------------

@router.get("/api/vision/status")
def vision_status(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    """What model is configured/available, and whether it's loaded. Inference
    is on-command only (no background loop), so this never triggers a load."""
    _require_user(db, session_token)
    return _vision_service.status()


@router.get("/api/vision/results")
def vision_results(
    limit: int = 20,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    """Recent candling/vision analyses from the model_results table."""
    _require_user(db, session_token)
    limit = max(1, min(limit, 100))
    rows = db.scalars(select(ModelResult).order_by(desc(ModelResult.created_at)).limit(limit)).all()
    return {
        "ok": True,
        "results": [
            {
                "id": r.id,
                "egg_id": r.egg_id,
                "label": r.predicted_label,
                "confidence": r.confidence,
                "backend": r.model_backend,
                "created_at": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for r in rows
        ],
    }


@router.post("/api/vision/reload")
def vision_reload(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    """Re-detect models after one is dropped onto the device (scp / SD card)."""
    _require_user(db, session_token)
    return {"endpoint": "vision.reload", **_vision_service.reload()}


@router.post("/api/vision/model")
async def vision_install_model(
    kind: str = Form("classifier"),
    model: UploadFile = File(...),
    labels: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    """Plug-and-play installer: upload a .tflite (+ optional labels.txt) and the
    device picks it up immediately — no env edits, no restart.

    kind: "classifier" (egg analysis) | "stage" (incubation-day estimator).
    """
    _require_user(db, session_token)
    if kind not in ("classifier", "stage"):
        raise HTTPException(status_code=400, detail="kind must be 'classifier' or 'stage'")
    if not model.filename or not model.filename.endswith(".tflite"):
        raise HTTPException(status_code=400, detail="model must be a .tflite file")

    target = Path(_vision_service.tflite_model_path if kind == "classifier" else _vision_service.stage_model_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await model.read())

    if labels is not None and labels.filename:
        labels_path = target.with_suffix(".txt") if kind == "classifier" else target.with_name("stage_labels.txt")
        labels_path.write_bytes(await labels.read())

    # If a stage model was installed, flip the stage backend on so it's used.
    if kind == "stage":
        _vision_service.stage_backend = "tflite"

    status_after = _vision_service.reload()
    return {"endpoint": "vision.model.install", "installed": kind, "path": str(target), **status_after}


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
