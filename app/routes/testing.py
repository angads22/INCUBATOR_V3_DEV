"""
Testing tab — run the local vision model on egg images and measure how far
along each egg is (estimated incubation day + stage), with an effectiveness
readout (predicted vs actual, MAE in days).

Page:
  GET  /testing                     — Life Loop styled testing UI

API (all auth-gated like the rest of the control APIs):
  POST /api/testing/predict         — image(s) → stage predictions
  POST /api/testing/record          — persist actual_day + a prediction
  GET  /api/testing/results         — list saved tests + aggregate MAE
  POST /api/testing/clear           — delete all saved tests
  GET  /api/testing/results.csv     — CSV export of saved tests
  GET  /api/testing/captures        — list saved-capture images on disk
  GET  /api/testing/image?path=...  — serve an allowed image (thumbnails)

All prediction goes through VisionService.predict_stage() — the single vision
integration point. The default heuristic backend works with no trained model.
"""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, has_any_user
from ..config import settings
from ..database import get_db
from ..models import Egg, StageTest

if TYPE_CHECKING:
    from ..services.vision_service import VisionService

logger = logging.getLogger(__name__)

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Image extensions browsable as "saved captures" / servable as thumbnails.
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# Wired at startup from main.py.
_vision_service: "VisionService | None" = None


def set_testing_services(vision) -> None:
    global _vision_service
    _vision_service = vision


# ------------------------------------------------------------------
# Auth (mirrors web.py / hardware send)
# ------------------------------------------------------------------

def _login_required(db: Session) -> bool:
    return settings.require_login or has_any_user(db)


def _auth_redirect(db: Session, session_token: str | None):
    if _login_required(db) and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return None


def _require_api_user(db: Session, session_token: str | None) -> None:
    if _login_required(db) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


# ------------------------------------------------------------------
# Safe paths — uploads + browsable captures live under known roots only.
# ------------------------------------------------------------------

def _uploads_dir() -> Path:
    return Path(settings.captures_dir) / "testing_uploads"


def _allowed_roots() -> list[Path]:
    roots = {Path(settings.captures_dir), Path(settings.camera_image_dir), _uploads_dir()}
    resolved = []
    for r in roots:
        try:
            resolved.append(r.resolve())
        except Exception:  # noqa: BLE001
            resolved.append(r)
    return resolved


def _within_roots(resolved: Path, roots: list[Path]) -> bool:
    """True if a resolved path sits under any allowed root."""
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_allowed(path_str: str) -> Path | None:
    """Resolve a client-supplied path and confirm it sits under an allowed root.

    Returns the resolved Path, or None if it escapes the roots or is missing.
    Prevents path traversal / arbitrary file reads from the predict + image APIs.
    """
    if not path_str:
        return None
    try:
        candidate = Path(path_str).resolve()
    except Exception:  # noqa: BLE001
        return None
    if _within_roots(candidate, _allowed_roots()) and candidate.is_file():
        return candidate
    return None


def _save_upload(upload: UploadFile) -> Path:
    dest_dir = _uploads_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in _IMAGE_EXTS:
        suffix = ".jpg"
    dest = dest_dir / f"upload_{uuid.uuid4().hex[:10]}{suffix}"
    with dest.open("wb") as fh:
        fh.write(upload.file.read())
    return dest


# ------------------------------------------------------------------
# Pure effectiveness computation (unit-tested)
# ------------------------------------------------------------------

def compute_mae(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean absolute error (days) over rows that have a recorded actual_day.

    Pure: ``rows`` are plain dicts with predicted_day / actual_day. Returns
    {mae, count} where count is the number of rows that contributed and mae is
    None when there are none.
    """
    errors = [
        abs(float(r["predicted_day"]) - float(r["actual_day"]))
        for r in rows
        if r.get("actual_day") is not None and r.get("predicted_day") is not None
    ]
    n = len(errors)
    return {"mae": round(sum(errors) / n, 3) if n else None, "count": n}


def _row_to_dict(row: StageTest) -> dict[str, Any]:
    err = None
    if row.actual_day is not None and row.predicted_day is not None:
        err = round(abs(row.predicted_day - row.actual_day), 2)
    return {
        "id": row.id,
        "image_path": row.image_path,
        "predicted_day": row.predicted_day,
        "stage": row.stage,
        "confidence": row.confidence,
        "actual_day": row.actual_day,
        "backend": row.backend,
        "error": err,
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else None,
    }


# ------------------------------------------------------------------
# Page
# ------------------------------------------------------------------

@router.get("/testing", response_class=HTMLResponse)
def testing_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    eggs = db.scalars(select(Egg).order_by(Egg.id)).all()
    egg_options = [
        {
            "id": e.id,
            "label": e.label or f"Egg {e.id}",
            "has_roi": None not in (e.roi_x, e.roi_y, e.roi_w, e.roi_h),
        }
        for e in eggs
    ]
    return templates.TemplateResponse(
        request=request,
        name="testing.html",
        context={
            "version": settings.app_version,
            "stage_backend": _vision_service.stage_backend if _vision_service else settings.vision_stage_backend,
            "incubation_days": settings.incubation_days,
            "camera_backend": settings.camera_backend,
            "camera_stream_enabled": settings.camera_stream_enabled,
            "eggs": egg_options,
        },
    )


# ------------------------------------------------------------------
# Predict
# ------------------------------------------------------------------

@router.post("/api/testing/predict")
async def api_testing_predict(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    files: list[UploadFile] = File(default=[]),
    paths: list[str] = Form(default=[]),
) -> dict[str, Any]:
    """Run predict_stage on any uploaded files and/or allowed server paths."""
    _require_api_user(db, session_token)
    if _vision_service is None:
        raise HTTPException(status_code=503, detail="Vision service unavailable")

    targets: list[Path] = []
    for upload in files or []:
        if not upload.filename:
            continue
        try:
            targets.append(_save_upload(upload))
        except Exception as exc:  # noqa: BLE001
            logger.error("Upload save failed: %s", exc)
    for raw in paths or []:
        resolved = _resolve_allowed(raw)
        if resolved is not None:
            targets.append(resolved)

    if not targets:
        raise HTTPException(status_code=400, detail="No images supplied (upload a file or pick a saved capture).")

    predictions = []
    for path in targets:
        result = _vision_service.predict_stage(str(path))
        predictions.append({**result, "path": str(path), "name": path.name})
    return {"ok": True, "count": len(predictions), "predictions": predictions}


# ------------------------------------------------------------------
# Record (persist predicted vs actual)
# ------------------------------------------------------------------

class RecordPayload(BaseModel):
    image_path: str
    predicted_day: float
    stage: str = "unclear"
    confidence: float = 0.0
    backend: str = "unknown"
    actual_day: float | None = None


@router.post("/api/testing/record")
def api_testing_record(
    payload: RecordPayload,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    row = StageTest(
        image_path=payload.image_path,
        predicted_day=float(payload.predicted_day),
        stage=payload.stage,
        confidence=float(payload.confidence),
        actual_day=float(payload.actual_day) if payload.actual_day is not None else None,
        backend=payload.backend,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    rows = [_row_to_dict(r) for r in db.scalars(select(StageTest)).all()]
    return {"ok": True, "id": row.id, **compute_mae(rows)}


# ------------------------------------------------------------------
# Results + MAE
# ------------------------------------------------------------------

@router.get("/api/testing/results")
def api_testing_results(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    rows = [
        _row_to_dict(r)
        for r in db.scalars(select(StageTest).order_by(desc(StageTest.created_at), desc(StageTest.id))).all()
    ]
    summary = compute_mae(rows)
    return {"ok": True, "results": rows, "total": len(rows), **summary}


@router.post("/api/testing/clear")
def api_testing_clear(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    count = len(db.scalars(select(StageTest)).all())
    for row in db.scalars(select(StageTest)).all():
        db.delete(row)
    db.commit()
    return {"ok": True, "cleared": count}


@router.get("/api/testing/results.csv")
def api_testing_results_csv(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Response:
    _require_api_user(db, session_token)
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["image_path", "predicted_day", "stage", "confidence", "actual_day", "error", "backend", "created_at"])
    for r in db.scalars(select(StageTest).order_by(desc(StageTest.created_at), desc(StageTest.id))).all():
        d = _row_to_dict(r)
        writer.writerow([
            d["image_path"], d["predicted_day"], d["stage"], d["confidence"],
            d["actual_day"], d["error"], d["backend"], d["created_at"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=stage_tests.csv"},
    )


# ------------------------------------------------------------------
# Saved captures + thumbnail serving
# ------------------------------------------------------------------

@router.get("/api/testing/captures")
def api_testing_captures(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict[str, Any]:
    _require_api_user(db, session_token)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    roots = _allowed_roots()
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() not in _IMAGE_EXTS or not p.is_file():
                continue
            try:
                resolved = p.resolve()
            except OSError:
                continue
            # Skip symlinks that escape the allowed roots — never disclose paths
            # to files outside the capture directories.
            if not _within_roots(resolved, roots):
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0
            items.append({"path": key, "name": p.name, "mtime": mtime})
    items.sort(key=lambda d: d["mtime"], reverse=True)
    return {"ok": True, "captures": items[:200]}


@router.get("/api/testing/image")
def api_testing_image(
    path: str,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Response:
    _require_api_user(db, session_token)
    resolved = _resolve_allowed(path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Image not found")
    media = "image/png" if resolved.suffix.lower() == ".png" else "image/jpeg"
    return Response(content=resolved.read_bytes(), media_type=media, headers={"Cache-Control": "no-store"})
