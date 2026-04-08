from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session
from ..config import settings
from ..database import get_db
from ..services.ai_service import AIService
from ..services.setup_mode_service import SetupModeService
from ..services.wifi_service import WiFiService
from ..settings_store import get_settings, update_settings

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ai_service = AIService()

_setup_mode_service = SetupModeService()
_wifi_service = WiFiService()


def set_runtime_services(setup_mode_service: SetupModeService, wifi_service: WiFiService) -> None:
    global _setup_mode_service, _wifi_service
    _setup_mode_service = setup_mode_service
    _wifi_service = wifi_service


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
        request=request,
        name="dashboard/index.html",
        context={
            "settings": app_settings,
            "version": settings.app_version,
            "mock": mock_snapshot,
            "ai_insight": ai_insight,
            "ai_findings": ai_service.recent_findings(),
            "recent_activity": [
                "Device booted in local mode.",
                "Hotspot setup available via pin 2 long press.",
                "Hardware bridge running in placeholder mode.",
            ],
            "setup_state": _setup_mode_service.status(),
        },
    )


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
        request=request,
        name="status.html",
        context={
            "version": settings.app_version,
            "health": {
                "hardware": "online",
                "sensors": "online",
                "alarms": "none",
                "link": "uart-stable",
                "setup_mode": "on" if _setup_mode_service.is_setup_mode() else "off",
            },
        },
    )


@router.get("/help", response_class=HTMLResponse)
def help_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return templates.TemplateResponse(request=request, name="help.html", context={"version": settings.app_version})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return templates.TemplateResponse(request=request, name="settings.html", context={"settings": get_settings(db), "version": settings.app_version})


@router.get("/hardware", response_class=HTMLResponse)
def hardware_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return templates.TemplateResponse(request=request, name="hardware.html", context={"version": settings.app_version})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"version": settings.app_version})


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="onboarding.html",
        context={
            "version": settings.app_version,
            "setup_state": _setup_mode_service.status(),
            "networks": _wifi_service.scan_networks(),
        },
    )


class OnboardingCompletePayload(BaseModel):
    ssid: str
    password: str = ""
    device_name: str = "Incubator"
    create_account: bool = False
    username: str = ""


@router.post("/onboarding/start")
def onboarding_start() -> JSONResponse:
    _setup_mode_service.enter_setup_mode("manual_web_trigger")
    _wifi_service.start_hotspot(settings.setup_hotspot_ssid, settings.setup_hotspot_password)
    return JSONResponse({"ok": True, "setup_mode": True, "ap_url": "http://192.168.4.1"})


@router.get("/onboarding/networks")
def onboarding_networks() -> JSONResponse:
    networks = [n.__dict__ for n in _wifi_service.scan_networks()]
    return JSONResponse({"ok": True, "networks": networks})


@router.post("/onboarding/complete")
def onboarding_complete(payload: OnboardingCompletePayload, db: Session = Depends(get_db)) -> JSONResponse:
    update_settings(
        db,
        {
            "wifi_ssid": payload.ssid,
            "device_name": payload.device_name,
            "account_pending_link": "false" if payload.create_account else "true",
        },
    )
    _wifi_service.connect_client(payload.ssid, payload.password)
    _wifi_service.stop_hotspot()
    _setup_mode_service.exit_setup_mode()
    return JSONResponse({"ok": True, "next": "normal_mode", "account_created": payload.create_account})
