from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session
from ..config import settings
from ..database import get_db
from ..services.ai_service import AIService
from ..settings_store import get_settings

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ai_service = AIService()


def _auth_redirect(db: Session, session_token: str | None):
    if settings.require_login and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return None


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect

    app_settings = get_settings(db)
    mock_snapshot = {
        "temperature_c": 37.4,
        "humidity_pct": 54.8,
        "target_temp_c": float(app_settings.get("target_temp_c", "37.5")),
        "target_humidity_pct": float(app_settings.get("target_humidity_pct", "55")),
        "heater": app_settings.get("heater_enabled", "false") == "true",
        "fan": app_settings.get("fan_enabled", "true") == "true",
        "turner": app_settings.get("turner_enabled", "true") == "true",
    }
    ai_insight = ai_service.generate_dashboard_insight(mock_snapshot["temperature_c"], mock_snapshot["humidity_pct"])

    return templates.TemplateResponse(
        "dashboard/index.html",
        {
            "request": request,
            "settings": app_settings,
            "version": settings.app_version,
            "mock": mock_snapshot,
            "ai_insight": ai_insight,
            "ai_findings": ai_service.recent_findings(),
            "recent_activity": [
                "Setup completed by owner account.",
                "Heater toggled ON (manual).",
                "Door lock command acknowledged.",
            ],
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return templates.TemplateResponse("settings.html", {"request": request, "settings": get_settings(db), "version": settings.app_version})


@router.get("/status", response_class=HTMLResponse)
def status_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "version": settings.app_version,
            "health": {"hardware": "online", "sensors": "online", "alarms": "none", "link": "uart-stable"},
        },
    )


@router.get("/hardware", response_class=HTMLResponse)
def hardware_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return templates.TemplateResponse("hardware.html", {"request": request, "version": settings.app_version})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "version": settings.app_version})


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request):
    return templates.TemplateResponse("onboarding.html", {"request": request, "version": settings.app_version})
