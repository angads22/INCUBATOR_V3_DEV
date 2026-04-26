from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, hash_password
from ..config import settings
from ..database import get_db
from ..models import ActionLog, DeviceConfig, User
from ..schemas import HotspotSetupPayload
from ..services.ai_service import AIService
from ..settings_store import get_settings, update_settings

if TYPE_CHECKING:
    from ..services.hardware_service import HardwareService
    from ..services.onboarding_service import OnboardingService
    from ..services.setup_mode_service import SetupModeService
    from ..services.vision_service import VisionService
    from ..services.wifi_service import WiFiService

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ai_service = AIService()

_setup_mode_service: SetupModeService | None = None
_wifi_service: WiFiService | None = None
_onboarding_service: OnboardingService | None = None
_hardware_service: HardwareService | None = None
_vision_service: VisionService | None = None


def set_runtime_services(
    setup_mode_service=None,
    wifi_service=None,
    onboarding_service=None,
    hardware_service=None,
    vision_service=None,
) -> None:
    global _setup_mode_service, _wifi_service, _onboarding_service, _hardware_service, _vision_service
    _setup_mode_service = setup_mode_service
    _wifi_service = wifi_service
    _onboarding_service = onboarding_service
    _hardware_service = hardware_service
    _vision_service = vision_service


def _auth_redirect(db: Session, session_token: str | None):
    if settings.require_login and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return None


def _render(request: Request, name: str, context: dict[str, Any]):
    return templates.TemplateResponse(request=request, name=name, context=context)


def _is_setup_complete(config: DeviceConfig | None) -> bool:
    # Account creation is optional — treat WiFi + device name as sufficient for "complete"
    return bool(config and config.device_name and config.wifi_ssid)


def _get_bool_setting(app_settings: dict[str, str], key: str, default: bool) -> bool:
    return app_settings.get(key, "true" if default else "false").lower() == "true"


# ------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect

    config = db.scalar(select(DeviceConfig).limit(1))
    app_settings = get_settings(db)
    setup_mode = _setup_mode_service.is_setup_mode() if _setup_mode_service else False
    hotspot_active = _onboarding_service.is_hotspot_active() if _onboarding_service else False
    ap_ssid = ""
    if _onboarding_service and config:
        ap_ssid = _onboarding_service.ap_ssid(config.device_id)

    device_name = "Pi Zero Incubator"
    device_id = "PI-UNCLAIMED"
    if config and config.device_name:
        device_name = config.device_name
    if config and config.device_id:
        device_id = config.device_id

    is_claimed = bool(config and config.claimed)
    wifi_ssid = config.wifi_ssid if config else None
    setup_complete = _is_setup_complete(config)

    if hotspot_active or setup_mode:
        network_state = "Hotspot active"
        network_detail = f"AP: {ap_ssid} — connect and open http://{settings.ap_ip}:8000"
    elif wifi_ssid:
        connected_ssid = _wifi_service.get_connected_ssid() if _wifi_service else None
        network_state = "Connected" if connected_ssid else "Configured"
        network_detail = f"SSID: {wifi_ssid}"
    else:
        network_state = "Not configured"
        network_detail = "No Wi-Fi configured. Restart or hold setup button."

    # Live sensor snapshot — falls back to targets when hardware unavailable
    if _hardware_service:
        env = _hardware_service.read_environment()
        live_temp = env.get("temperature_c") or float(app_settings.get("target_temp_c", "37.5"))
        live_hum = env.get("humidity_pct") or float(app_settings.get("target_humidity_pct", "55"))
    else:
        live_temp = float(app_settings.get("target_temp_c", "37.5"))
        live_hum = float(app_settings.get("target_humidity_pct", "55"))

    snapshot = {
        "temperature_c": live_temp,
        "humidity_pct": live_hum,
        "target_temp_c": float(app_settings.get("target_temp_c", "37.5")),
        "target_humidity_pct": float(app_settings.get("target_humidity_pct", "55")),
        "door_closed": _get_bool_setting(app_settings, "door_closed", default=True),
        "heater": _get_bool_setting(app_settings, "heater_enabled", default=False),
        "fan": _get_bool_setting(app_settings, "fan_enabled", default=True),
        "turner": _get_bool_setting(app_settings, "turner_enabled", default=True),
    }
    ai_insight = ai_service.generate_dashboard_insight(snapshot["temperature_c"], snapshot["humidity_pct"])

    # Determine next-step card
    if hotspot_active or setup_mode:
        current_state_summary = "Hotspot active — waiting for setup"
        state_detail = f"Connect to Wi-Fi '{ap_ssid}' (password: {settings.ap_password}) then open http://{settings.ap_ip}:8000"
        next_step = {
            "title": "Finish setup via hotspot",
            "body": f"Join '{ap_ssid}' on your phone or laptop and complete the wizard.",
            "cta_label": "Open wizard",
            "cta_href": "/onboarding",
        }
    elif not setup_complete:
        current_state_summary = "Setup pending"
        state_detail = "Device needs a name, Wi-Fi config, and optional account."
        next_step = {
            "title": "Start local setup",
            "body": "Use guided onboarding to name the device and connect to Wi-Fi.",
            "cta_label": "Start setup",
            "cta_href": "/onboarding",
        }
    elif not is_claimed:
        current_state_summary = "Running locally (unclaimed)"
        state_detail = "Fully functional locally. Add an account later if needed."
        next_step = {
            "title": "Link account (optional)",
            "body": "Create an account for login protection and future remote access.",
            "cta_label": "Link account",
            "cta_href": "/onboarding",
        }
    else:
        current_state_summary = "Running"
        state_detail = "Monitor health and adjust controls from Status and Settings."
        next_step = {
            "title": "Review incubator health",
            "body": "Check live readings and confirm heater, fan, and turner are behaving.",
            "cta_label": "Open status",
            "cta_href": "/status",
        }

    quick_actions = [
        {"label": "Start Setup", "detail": "Local hotspot onboarding", "href": "/onboarding", "tone": "primary"},
        {"label": "Open Status", "detail": "Live readings", "href": "/status", "tone": "secondary"},
        {"label": "Candle Eggs", "detail": "Capture + analyze image", "href": "/hardware#candle", "tone": "secondary"},
        {"label": "Help", "detail": "Guides and troubleshooting", "href": "/help", "tone": "ghost"},
        {"label": "Settings", "detail": "Targets and controls", "href": "/settings", "tone": "ghost"},
        {"label": "Hardware", "detail": "Manual GPIO test panel", "href": "/hardware", "tone": "ghost"},
    ]

    return _render(
        request=request,
        name="dashboard/index.html",
        context={
            "settings": app_settings,
            "version": settings.app_version,
            "live": snapshot,
            "ai_insight": ai_insight,
            "ai_findings": ai_service.recent_findings(),
            "home_summary": {
                "device_name": device_name,
                "device_id": device_id,
                "setup_complete": setup_complete,
                "is_claimed": is_claimed,
                "setup_mode": setup_mode,
                "hotspot_active": hotspot_active,
                "ap_ssid": ap_ssid,
                "ap_ip": settings.ap_ip,
                "network_state": network_state,
                "network_detail": network_detail,
                "current_state_summary": current_state_summary,
                "state_detail": state_detail,
            },
            "next_step": next_step,
            "quick_actions": quick_actions,
            "vision_backend": settings.vision_backend,
            "recent_activity": [
                row.action + (f": {row.payload}" if row.payload else "")
                for row in db.scalars(
                    select(ActionLog).order_by(desc(ActionLog.created_at)).limit(5)
                ).all()
            ],
        },
    )


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

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
    hw_state = _hardware_service.get_state() if _hardware_service else {}
    env = _hardware_service.read_environment() if _hardware_service else {}

    if not _hardware_service:
        sensor_status = "unavailable"
    elif env.get("ok"):
        sensor_status = "online"
    else:
        sensor_status = "error"

    return _render(
        request=request,
        name="status.html",
        context={
            "version": settings.app_version,
            "health": {
                "hardware": "online" if _hardware_service else "unavailable",
                "sensors": sensor_status,
                "alarms": "none",
                "gpio_mock": settings.gpio_mock,
                "setup_mode": "on" if setup_mode else "off",
                "vision_backend": settings.vision_backend,
                "camera_backend": settings.camera_backend,
            },
            "environment": env,
            "hw_state": hw_state,
        },
    )


# ------------------------------------------------------------------
# Static pages
# ------------------------------------------------------------------

@router.get("/help", response_class=HTMLResponse)
def help_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect
    return _render(request=request, name="help.html", context={"version": settings.app_version})


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
    return _render(request=request, name="hardware.html", context={"version": settings.app_version})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _render(request=request, name="login.html", context={"version": settings.app_version})


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request):
    return _render(
        request=request,
        name="onboarding.html",
        context={
            "version": settings.app_version,
            "ap_ip": settings.ap_ip,
            "ap_password": settings.ap_password,
        },
    )


# ------------------------------------------------------------------
# Onboarding API
# ------------------------------------------------------------------

@router.post("/onboarding/start")
def onboarding_start(db: Session = Depends(get_db)) -> dict:
    config = db.scalar(select(DeviceConfig).limit(1))
    device_id = config.device_id if config else "PI-XXXX"
    if _onboarding_service:
        return _onboarding_service.start_manual_hotspot(device_id)
    # Fallback if onboarding service not wired
    if _setup_mode_service:
        _setup_mode_service.enter_setup_mode("manual_trigger")
    if _wifi_service:
        _wifi_service.start_hotspot(f"{settings.ap_ssid_prefix}-{device_id[-4:]}", settings.ap_password)
    return {"ok": True, "ap_url": f"http://{settings.ap_ip}:8000", "ssid": f"{settings.ap_ssid_prefix}-{device_id[-4:]}"}


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
        config = DeviceConfig(device_id=f"PI-{__import__('uuid').uuid4().hex[:8].upper()}", claimed=False)
        db.add(config)

    config.device_name = payload.device_name
    config.wifi_ssid = payload.ssid or None

    if payload.create_account and payload.username and payload.email and payload.password:
        if not _EMAIL_RE.match(payload.email):
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

    # Switch from AP to client WiFi
    if _onboarding_service:
        _onboarding_service.complete(payload.ssid or "", payload.wifi_password or "")
    elif _setup_mode_service:
        _setup_mode_service.exit_setup_mode()
        if _wifi_service and payload.ssid:
            _wifi_service.connect_client(payload.ssid, payload.wifi_password or "")

    return {"ok": True, "device_name": payload.device_name, "claimed": bool(config.claimed)}


# ------------------------------------------------------------------
# Settings API
# ------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    target_temp_c: float | None = Field(default=None, ge=20.0, le=42.0)
    target_humidity_pct: float | None = Field(default=None, ge=0.0, le=100.0)
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
        if _hardware_service:
            _hardware_service.set_heater(payload.heater_enabled)
    if payload.fan_enabled is not None:
        updates["fan_enabled"] = "true" if payload.fan_enabled else "false"
        if _hardware_service:
            _hardware_service.set_fan(payload.fan_enabled)
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
