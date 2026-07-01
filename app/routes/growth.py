"""
Growth-tracking + vision-action API.

  GET  /api/vision/growth              — per-egg growth status (all eggs)
  GET  /api/vision/growth/{egg_id}     — trajectory, observations, recommendations
  POST /api/vision/growth/{egg_id}/apply — run the recommended (or given) actions

Auth-gated once an operator account exists (mirrors the other control APIs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, has_any_user
from ..config import settings
from ..database import get_db
from ..models import GrowthObservation

if TYPE_CHECKING:
    from ..services.growth_service import GrowthService
    from ..services.hardware_service import HardwareService

router = APIRouter()

_growth_service: "GrowthService | None" = None
_hardware_service: "HardwareService | None" = None


def set_growth_services(growth, hardware) -> None:
    global _growth_service, _hardware_service
    _growth_service = growth
    _hardware_service = hardware


def _require_api_user(db: Session, session_token: str | None) -> None:
    if (settings.require_login or has_any_user(db)) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def _growth() -> "GrowthService":
    if _growth_service is None:
        raise HTTPException(status_code=503, detail="Growth service unavailable")
    return _growth_service


class ApplyPayload(BaseModel):
    # Omit to apply the engine's recommended actions; pass a list to force some.
    actions: list[str] | None = None


@router.get("/api/vision/growth")
def growth_summary(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    return {"ok": True, "eggs": _growth().summary(db)}


@router.get("/api/vision/growth/{egg_id}")
def growth_detail(
    egg_id: int,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    assessment = _growth().assess(db, egg_id)
    rows = db.scalars(
        select(GrowthObservation)
        .where(GrowthObservation.egg_id == egg_id)
        .order_by(desc(GrowthObservation.created_at))
        .limit(50)
    ).all()
    assessment["observations"] = [
        {
            "day_estimate": r.day_estimate, "stage": r.stage, "label": r.label,
            "confidence": r.confidence, "backend": r.backend, "source": r.source,
            "created_at": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
        }
        for r in rows
    ]
    return {"ok": True, **assessment}


@router.post("/api/vision/growth/{egg_id}/apply")
def growth_apply(
    egg_id: int,
    payload: ApplyPayload = ApplyPayload(),
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    result = _growth().apply_actions(db, egg_id, _hardware_service, actions=payload.actions)
    return {"ok": True, **result}
