from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, hash_password
from ..config import settings
from ..database import get_db
from ..models import ActionLog, DeviceConfig, User
from ..schemas import HotspotSetupPayload
from ..services.ai_service import AIService
from ..settings_store import get_settings, update_settings

if TYPE_CHECKING:
    from ..services.setup_mode_service import SetupModeService
    from ..services.wifi_service import WiFiService

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ai_service = AIService()
_setup_mode_service: SetupModeService | None = None
_wifi_service: WiFiService | None = None


def set_runtime_services(setup_mode_service=None, wifi_service=None) -> None:
    global _setup_mode_service, _wifi_service
    _setup_mode_service = setup_mode_service
    _wifi_service = wifi_service


def _auth_redirect(db: Session, session_token: str | None):
    if settings.require_login and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return None


def _render(request: Request, name: str, context: dict[str, Any]):
    merged_context = {"request": request, **context}
    try:
        return templates.TemplateResponse(request=request, name=name, context=context)
    except TypeError:
        return templates.TemplateResponse(name=name, context=merged_context)


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

    return _render(
        request=request,
        name="dashboard/index.html",
        context={
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


@router.get("/status", response_class=HTMLResponse)
def status_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    setup_mode = _setup_mode_service.is_setup_mode() if _setup_mode_service else False
    return _render(
        request=request,
        name="status.html",
        context={
            "version": settings.app_version,
            "health": {
                "hardware": "online",
                "sensors": "online",
                "alarms": "none",
                "link": "uart-stable",
                "setup_mode": "on" if setup_mode else "off",
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
    return _render(
        request=request,
        name="help.html",
        context={"version": settings.app_version},
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
    return _render(
        request=request,
        name="settings.html",
        context={"settings": get_settings(db), "version": settings.app_version},
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
    return _render(
        request=request,
        name="hardware.html",
        context={"version": settings.app_version},
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _render(
        request=request,
        name="login.html",
        context={"version": settings.app_version},
    )


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request):
    return _render(
        request=request,
        name="onboarding.html",
        context={"version": settings.app_version},
    )


@router.post("/onboarding/start")
def onboarding_start() -> dict:
    if _setup_mode_service:
        _setup_mode_service.enter_setup_mode("manual_trigger")
    if _wifi_service:
        _wifi_service.start_hotspot("IncubatorSetup", "setup1234")
    return {"ok": True, "ap_url": "http://192.168.4.1", "ssid": "IncubatorSetup"}


@router.get("/onboarding/wifi-scan")
def onboarding_wifi_scan() -> dict:
    if _wifi_service:
        networks = _wifi_service.scan_networks()
        return {
            "ok": True,
            "networks": [{"ssid": n.ssid, "strength": n.strength, "secure": n.secure} for n in networks],
        }
    return {"ok": False, "networks": []}


@router.post("/onboarding/complete")
def onboarding_complete(payload: HotspotSetupPayload, db: Session = Depends(get_db)) -> dict:
    config = db.scalar(select(DeviceConfig).limit(1))
    if not config:
        config = DeviceConfig(device_id="UNOQ-UNCLAIMED", claimed=False)
        db.add(config)

    config.device_name = payload.device_name
    config.wifi_ssid = payload.ssid or None

    if payload.create_account and payload.username and payload.email and payload.password:
        if "@" not in payload.email or len(payload.email) < 5:
            raise HTTPException(status_code=422, detail="Invalid email address.")
        existing = db.scalar(
            select(User).where((User.username == payload.username) | (User.email == payload.email))
        )
        if existing:
            raise HTTPException(status_code=409, detail="Username or email already exists.")
        db.add(
            User(
                username=payload.username,
                email=payload.email,
                password_hash=hash_password(payload.password),
                role="owner",
            )
        )
        config.claimed = True

    db.add(ActionLog(action="onboarding_complete", payload=f"device={payload.device_name},ssid={payload.ssid}"))
    db.commit()

    if _setup_mode_service:
        _setup_mode_service.exit_setup_mode()
    if _wifi_service and payload.ssid:
        _wifi_service.connect_client(payload.ssid, payload.wifi_password)

    return {"ok": True, "device_name": payload.device_name, "claimed": bool(config.claimed)}


class SettingsUpdate(BaseModel):
    target_temp_c: float | None = None
    target_humidity_pct: float | None = None
    heater_enabled: bool | None = None
    fan_enabled: bool | None = None
    turner_enabled: bool | None = None
    alarm_enabled: bool | None = None


@router.post("/api/settings")
def api_settings_update(payload: SettingsUpdate, db: Session = Depends(get_db)) -> dict:
    updates: dict[str, str] = {}
    if payload.target_temp_c is not None:
        updates["target_temp_c"] = str(payload.target_temp_c)
    if payload.target_humidity_pct is not None:
        updates["target_humidity_pct"] = str(payload.target_humidity_pct)
    if payload.heater_enabled is not None:
        updates["heater_enabled"] = "true" if payload.heater_enabled else "false"
    if payload.fan_enabled is not None:
        updates["fan_enabled"] = "true" if payload.fan_enabled else "false"
    if payload.turner_enabled is not None:
        updates["turner_enabled"] = "true" if payload.turner_enabled else "false"
    if payload.alarm_enabled is not None:
        updates["alarm_enabled"] = "true" if payload.alarm_enabled else "false"
    if updates:
        update_settings(db, updates)
    return {"ok": True}


@router.post("/api/logout")
def api_logout() -> dict:
    return {"ok": True}
