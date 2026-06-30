"""
Egg-photo storage API — labeled captures + disk-pressure auto-prune.

  GET    /api/captures              — list stored labeled photos (newest first)
  GET    /api/captures/storage      — disk usage + janitor thresholds
  GET    /api/captures/image?id=    — serve one stored photo
  POST   /api/captures/capture      — snapshot the camera and store it labeled
  POST   /api/captures/prune        — run the janitor now (free up space)
  POST   /api/captures/{id}/pin     — pin (protect from auto-delete) / unpin
  DELETE /api/captures/{id}         — delete one stored photo

Auth is gated exactly like the other control APIs: required once an operator
account exists (or when INCUBATOR_REQUIRE_LOGIN is set).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, has_any_user
from ..config import settings
from ..database import get_db
from ..models import EggPhoto

if TYPE_CHECKING:
    from ..services.camera_service import CameraService
    from ..services.storage_service import StorageService

logger = logging.getLogger(__name__)

router = APIRouter()

# Wired at startup from main.py.
_storage_service: "StorageService | None" = None
_camera_service: "CameraService | None" = None


def set_capture_services(storage, camera) -> None:
    global _storage_service, _camera_service
    _storage_service = storage
    _camera_service = camera


def _require_api_user(db: Session, session_token: str | None) -> None:
    if (settings.require_login or has_any_user(db)) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def _storage() -> "StorageService":
    if _storage_service is None:
        raise HTTPException(status_code=503, detail="Storage service unavailable")
    return _storage_service


def _row_to_dict(r: EggPhoto) -> dict[str, Any]:
    return {
        "id": r.id,
        "egg_id": r.egg_id,
        "label": r.label,
        "backend": r.backend,
        "confidence": r.confidence,
        "size_kb": round((r.size_bytes or 0) / 1024, 1),
        "pinned": r.pinned,
        "created_at": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
    }


@router.get("/api/captures")
def list_captures(
    limit: int = 100,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    limit = max(1, min(limit, 500))
    rows = db.scalars(
        select(EggPhoto).order_by(desc(EggPhoto.created_at), desc(EggPhoto.id)).limit(limit)
    ).all()
    return {"ok": True, "captures": [_row_to_dict(r) for r in rows], "total": len(rows)}


@router.get("/api/captures/storage")
def storage_status(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    return {"ok": True, **_storage().usage(db)}


@router.get("/api/captures/image")
def capture_image(
    id: int,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Response:
    _require_api_user(db, session_token)
    row = db.scalar(select(EggPhoto).where(EggPhoto.id == id))
    if row is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    p = Path(row.path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Photo file missing")
    media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return Response(content=p.read_bytes(), media_type=media, headers={"Cache-Control": "no-store"})


@router.post("/api/captures/capture")
def capture_now(
    label: str = "egg",
    egg_id: int | None = None,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    """Take a full-res snapshot and store it as a labeled egg photo."""
    _require_api_user(db, session_token)
    if _camera_service is None:
        raise HTTPException(status_code=503, detail="Camera service unavailable")
    try:
        jpeg = _camera_service.snapshot_jpeg()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Camera capture failed: {exc}")
    row = _storage().save_photo(db, jpeg, label=label, egg_id=egg_id, backend=_camera_service.backend)
    return {"ok": True, "photo": _row_to_dict(row)}


@router.post("/api/captures/prune")
def prune_now(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    return {"ok": True, **_storage().enforce(db)}


@router.post("/api/captures/{photo_id}/pin")
def pin_capture(
    photo_id: int,
    pinned: bool = True,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    row = _storage().set_pinned(db, photo_id, pinned)
    if row is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return {"ok": True, "photo": _row_to_dict(row)}


@router.delete("/api/captures/{photo_id}")
def delete_capture(
    photo_id: int,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    if not _storage().delete(db, photo_id):
        raise HTTPException(status_code=404, detail="Photo not found")
    return {"ok": True, "deleted": photo_id}
