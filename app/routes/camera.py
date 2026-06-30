"""
Camera API routes — live preview + stills.

  GET /api/camera/snapshot     → full-resolution JPEG (on demand)
  GET /api/camera/stream       → multipart/x-mixed-replace MJPEG (low-res, ~1 fps)
  GET /api/camera/egg/{id}     → cropped tile using the egg's ROI (full frame if unset)

Auth is gated exactly like POST /hardware/send: required once an owner account
exists (or when INCUBATOR_REQUIRE_LOGIN is set). The MJPEG stream is additionally
behind CAMERA_STREAM_ENABLED (default false) so nothing streams by default.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Cookie, Depends, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, has_any_user
from ..config import settings
from ..database import get_db
from ..models import Egg

if TYPE_CHECKING:
    from ..services.camera_service import CameraService

logger = logging.getLogger(__name__)

router = APIRouter()

# Wired at startup from main.py.
_camera_service: "CameraService | None" = None


def set_camera_service(camera) -> None:
    global _camera_service
    _camera_service = camera


def _require_camera_user(db: Session, session_token: str | None) -> "CameraService":
    """Mirror /hardware/send auth, then ensure the camera service is wired."""
    if (settings.require_login or has_any_user(db)) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if _camera_service is None:
        raise HTTPException(status_code=503, detail="Camera service unavailable")
    return _camera_service


@router.get("/api/camera/snapshot")
def camera_snapshot(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Response:
    camera = _require_camera_user(db, session_token)
    try:
        data = camera.snapshot_jpeg()
    except Exception as exc:  # noqa: BLE001
        logger.error("Snapshot failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Camera capture failed: {exc}")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/api/camera/stream")
def camera_stream(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    camera = _require_camera_user(db, session_token)
    if not settings.camera_stream_enabled:
        raise HTTPException(status_code=404, detail="Live stream disabled (set CAMERA_STREAM_ENABLED=true)")
    return StreamingResponse(
        camera.mjpeg_stream(),
        media_type=f"multipart/x-mixed-replace; boundary={camera.boundary}",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.get("/api/camera/egg/{egg_id}")
def camera_egg_tile(
    egg_id: int,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Response:
    camera = _require_camera_user(db, session_token)
    egg = db.scalar(select(Egg).where(Egg.id == egg_id))
    roi = None
    if egg and None not in (egg.roi_x, egg.roi_y, egg.roi_w, egg.roi_h):
        roi = (egg.roi_x, egg.roi_y, egg.roi_w, egg.roi_h)
    try:
        data = camera.egg_crop_jpeg(roi)
    except Exception as exc:  # noqa: BLE001
        logger.error("Egg tile failed: %s", exc)
        raise HTTPException(status_code=503, detail=f"Camera capture failed: {exc}")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
