from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import (
    authenticate,
    create_session,
    destroy_session,
    destroy_user_sessions,
    get_user_id_from_session,
    has_any_user,
    hash_password,
)
from ..config import settings
from ..database import get_db
from ..models import ActionLog, DeviceConfig, SensorLog, User
from ..schemas import HotspotSetupPayload
from ..services.ai_service import AIService
from ..settings_store import get_settings, update_settings

if TYPE_CHECKING:
    from ..services.alert_service import AlertService
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
_alert_service: AlertService | None = None


def set_runtime_services(
    setup_mode_service=None,
    wifi_service=None,
    onboarding_service=None,
    hardware_service=None,
    vision_service=None,
    alert_service=None,
) -> None:
    global _setup_mode_service, _wifi_service, _onboarding_service, _hardware_service, _vision_service, _alert_service
    _setup_mode_service = setup_mode_service
    _wifi_service = wifi_service
    _onboarding_service = onboarding_service
    _hardware_service = hardware_service
    _vision_service = vision_service
    _alert_service = alert_service


def _login_required(db: Session) -> bool:
    """Auth is enforced when explicitly required, or once an account exists.

    This gives the appliance its expected lifecycle: a fresh device is open so
    onboarding can run, but as soon as an owner account is created during setup
    every protected page and control API requires a valid session.
    """
    return settings.require_login or has_any_user(db)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.session_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")


def _auth_redirect(db: Session, session_token: str | None):
    if _login_required(db) and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return None


def _require_api_user(db: Session, session_token: str | None) -> None:
    """Raise 401 for control APIs when login is required and absent."""
    if _login_required(db) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def _render(request: Request, name: str, context: dict[str, Any]):
    return templates.TemplateResponse(request=request, name=name, context=context)


def _is_setup_complete(config: DeviceConfig | None) -> bool:
    # Account creation is optional — treat WiFi + device name as sufficient for "complete"
    return bool(config and config.device_name and config.wifi_ssid)


def _get_bool_setting(app_settings: dict[str, str], key: str, default: bool) -> bool:
    return app_settings.get(key, "true" if default else "false").lower() == "true"


def _sensor_context(db: Session) -> dict[str, Any]:
    """Cached sensor state for page rendering — never reads the DHT22 directly.

    Falls back to the latest sensor_logs row when the in-process cache is
    empty (fresh boot), so the UI shows the last known reading rather than
    pretending the targets are live values.
    """
    if _alert_service:
        sensor = _alert_service.sensor_snapshot()
    else:
        sensor = {
            "online": False,
            "has_reading": False,
            "temperature_c": None,
            "humidity_pct": None,
            "read_at": None,
            "mock": False,
        }
    if not sensor["has_reading"]:
        last_log = db.scalar(select(SensorLog).order_by(desc(SensorLog.captured_at)).limit(1))
        if last_log:
            sensor.update(
                has_reading=True,
                temperature_c=last_log.temperature_c,
                humidity_pct=last_log.humidity_pct,
                read_at=last_log.captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
    return sensor


def _alerts_context() -> dict[str, Any]:
    if _alert_service:
        return _alert_service.alert_state()
    return {"active": [], "alarm_on": False, "silenced": False}


# Friendly labels for the recent-events list; unknown actions fall back to the
# raw action name and payload.
_FRIENDLY_ACTIONS = {
    "login": "User logged in",
    "onboarding_complete": "Device setup completed",
    "setup_complete": "Device claimed",
    "network_connect": "Connected to a new Wi-Fi network",
    "alarm_silenced": "Alarm silenced",
    "system.password_reset": "Account password was reset",
    "system.sensor_offline": "Alert: sensor went offline",
    "system.sensor_offline_cleared": "Sensor back online",
    "system.temp_low": "Alert: temperature below range",
    "system.temp_low_cleared": "Temperature back in range",
    "system.temp_high": "Alert: temperature above range",
    "system.temp_high_cleared": "Temperature back in range",
    "system.humidity_low": "Alert: humidity below range",
    "system.humidity_low_cleared": "Humidity back in range",
    "system.humidity_high": "Alert: humidity above range",
    "system.humidity_high_cleared": "Humidity back in range",
}


def _format_activity(row: ActionLog) -> str:
    label = _FRIENDLY_ACTIONS.get(row.action)
    if not label:
        label = row.action + (f": {row.payload}" if row.payload else "")
    return f"{row.created_at:%d %b %H:%M} — {label}"


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

    # Cached sensor state — shown honestly: an offline sensor renders as
    # offline with its last known reading, never as the target values.
    sensor = _sensor_context(db)
    alerts = _alerts_context()

    snapshot = {
        "target_temp_c": float(app_settings.get("target_temp_c", "37.5")),
        "target_humidity_pct": float(app_settings.get("target_humidity_pct", "55")),
        "door_closed": _get_bool_setting(app_settings, "door_closed", default=True),
        "heater": _get_bool_setting(app_settings, "heater_enabled", default=False),
        "fan": _get_bool_setting(app_settings, "fan_enabled", default=True),
        "turner": _get_bool_setting(app_settings, "turner_enabled", default=True),
    }
    ai_insight = None
    if sensor["temperature_c"] is not None and sensor["humidity_pct"] is not None:
        ai_insight = ai_service.generate_dashboard_insight(sensor["temperature_c"], sensor["humidity_pct"])

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
            "sensor": sensor,
            "alerts": alerts,
            "poll_interval": settings.sensor_poll_interval_seconds,
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
                _format_activity(row)
                for row in db.scalars(
                    select(ActionLog).order_by(desc(ActionLog.created_at)).limit(8)
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
    # Served from the poller's cache — the DHT22 is never read on the request path.
    env = _sensor_context(db)
    alerts = _alerts_context()

    if not _hardware_service:
        sensor_status = "unavailable"
    elif env["online"]:
        sensor_status = "online"
    else:
        sensor_status = "offline"

    if alerts["active"]:
        alarms = f"{len(alerts['active'])} active" + (" (silenced)" if alerts["silenced"] else "")
        alarms_tone = "warn"
    else:
        alarms = "none"
        alarms_tone = "info"

    return _render(
        request=request,
        name="status.html",
        context={
            "version": settings.app_version,
            "health": {
                "hardware": "online" if _hardware_service else "unavailable",
                "sensors": sensor_status,
                "alarms": alarms,
                "alarms_tone": alarms_tone,
                "gpio_mock": settings.gpio_mock,
                "setup_mode": "on" if setup_mode else "off",
                "vision_backend": settings.vision_backend,
                "camera_backend": settings.camera_backend,
                "dht_pin": settings.gpio_dht_pin,
            },
            "environment": env,
            "alerts": alerts,
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
    return _render(
        request=request,
        name="help.html",
        context={
            "version": settings.app_version,
            "ap_ip": settings.ap_ip,
            "ap_ssid_prefix": settings.ap_ssid_prefix,
            "hold_seconds": int(settings.setup_button_hold_seconds),
            "dht_pin": settings.gpio_dht_pin,
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
def login_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    if get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return _render(
        request=request,
        name="login.html",
        context={"version": settings.app_version, "has_account": has_any_user(db)},
    )


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
def onboarding_complete(payload: HotspotSetupPayload, response: Response, db: Session = Depends(get_db)) -> dict:
    config = db.scalar(select(DeviceConfig).limit(1))
    if not config:
        config = DeviceConfig(device_id=f"PI-{__import__('uuid').uuid4().hex[:8].upper()}", claimed=False)
        db.add(config)

    config.device_name = payload.device_name
    config.wifi_ssid = payload.ssid or None

    new_user: User | None = None
    if payload.create_account and payload.username and payload.email and payload.password:
        if not _EMAIL_RE.match(payload.email):
            raise HTTPException(status_code=422, detail="Invalid email address.")
        if len(payload.password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")
        existing = db.scalar(
            select(User).where((User.username == payload.username) | (User.email == payload.email))
        )
        if existing:
            raise HTTPException(status_code=409, detail="Username or email already exists.")
        new_user = User(
            username=payload.username,
            email=payload.email,
            password_hash=hash_password(payload.password),
            role="owner",
        )
        db.add(new_user)
        config.claimed = True

    db.add(ActionLog(action="onboarding_complete", payload=f"device={payload.device_name},ssid={payload.ssid}"))
    db.commit()

    # Log the new owner straight in so they are not locked out by the account
    # they just created.
    if new_user is not None:
        _set_session_cookie(response, create_session(db, new_user.id, settings.session_ttl_seconds))

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
    alert_temp_tolerance_c: float | None = Field(default=None, ge=0.1, le=10.0)
    alert_humidity_tolerance_pct: float | None = Field(default=None, ge=1.0, le=50.0)
    incubation_day: int | None = Field(default=None, ge=0, le=21)
    heater_enabled: bool | None = None
    fan_enabled: bool | None = None
    turner_enabled: bool | None = None
    alarm_enabled: bool | None = None


@router.post("/api/settings")
def api_settings_update(
    payload: SettingsUpdate,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    updates: dict[str, str] = {}
    if payload.target_temp_c is not None:
        updates["target_temp_c"] = str(payload.target_temp_c)
    if payload.target_humidity_pct is not None:
        updates["target_humidity_pct"] = str(payload.target_humidity_pct)
    if payload.alert_temp_tolerance_c is not None:
        updates["alert_temp_tolerance_c"] = str(payload.alert_temp_tolerance_c)
    if payload.alert_humidity_tolerance_pct is not None:
        updates["alert_humidity_tolerance_pct"] = str(payload.alert_humidity_tolerance_pct)
    if payload.incubation_day is not None:
        updates["incubation_day"] = str(payload.incubation_day)
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
        # Disabling the alarm should quiet a ringing buzzer immediately, not
        # on the next poll cycle.
        if not payload.alarm_enabled and _alert_service:
            _alert_service.silence()
    if updates:
        update_settings(db, updates)
    return {"ok": True}


class LoginPayload(BaseModel):
    username: str
    password: str


@router.post("/api/login")
def api_login(payload: LoginPayload, response: Response, db: Session = Depends(get_db)) -> dict:
    user = authenticate(db, payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    _set_session_cookie(response, create_session(db, user.id, settings.session_ttl_seconds))
    db.add(ActionLog(action="login", payload=user.username))
    db.commit()
    return {"ok": True, "username": user.username}


@router.post("/api/logout")
def api_logout(
    response: Response,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    destroy_session(db, session_token)
    _clear_session_cookie(response)
    return {"ok": True}


# ------------------------------------------------------------------
# Alerts API
# ------------------------------------------------------------------

@router.post("/api/alerts/silence")
def api_alerts_silence(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    state = _alerts_context()
    if _alert_service:
        state = _alert_service.silence()
        db.add(ActionLog(action="alarm_silenced"))
        db.commit()
    return {"ok": True, "alerts": state}


# ------------------------------------------------------------------
# Network API — change Wi-Fi after setup without re-running onboarding
# ------------------------------------------------------------------

class NetworkConnectPayload(BaseModel):
    ssid: str = Field(min_length=1, max_length=64)
    password: str = ""


@router.get("/api/network/status")
def api_network_status(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    config = db.scalar(select(DeviceConfig).limit(1))
    return {
        "ok": True,
        "connected_ssid": _wifi_service.get_connected_ssid() if _wifi_service else None,
        "configured_ssid": config.wifi_ssid if config else None,
        "hotspot_active": _onboarding_service.is_hotspot_active() if _onboarding_service else False,
    }


@router.post("/api/network/connect")
def api_network_connect(
    payload: NetworkConnectPayload,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    if not _wifi_service:
        raise HTTPException(status_code=503, detail="Wi-Fi service unavailable")
    connected = _wifi_service.connect_client(payload.ssid, payload.password or "")
    if not connected:
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to '{payload.ssid}'. Check the password and try again.",
        )
    config = db.scalar(select(DeviceConfig).limit(1))
    if config:
        config.wifi_ssid = payload.ssid
    db.add(ActionLog(action="network_connect", payload=payload.ssid))
    db.commit()
    return {"ok": True, "ssid": payload.ssid}


# ------------------------------------------------------------------
# Password reset — requires physical presence (setup mode)
# ------------------------------------------------------------------

class PasswordResetPayload(BaseModel):
    identifier: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request):
    setup_mode = _setup_mode_service.is_setup_mode() if _setup_mode_service else False
    return _render(
        request=request,
        name="reset_password.html",
        context={
            "version": settings.app_version,
            "setup_mode": setup_mode,
            "hold_seconds": int(settings.setup_button_hold_seconds),
        },
    )


@router.post("/api/reset-password")
def api_reset_password(payload: PasswordResetPayload, db: Session = Depends(get_db)) -> dict:
    """Reset an account password without a session.

    Gated on setup mode, which can only be entered by physically holding the
    device's setup button (or during first-boot onboarding) — that is the
    proof of presence.  The response is identical whether or not the account
    exists, to avoid account enumeration.
    """
    if not (_setup_mode_service and _setup_mode_service.is_setup_mode()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password reset requires setup mode. Hold the device's setup button, then try again.",
        )
    identifier = payload.identifier.strip()
    user = db.scalar(
        select(User).where((User.username == identifier) | (User.email == identifier)).limit(1)
    )
    if user:
        user.password_hash = hash_password(payload.new_password)
        destroy_user_sessions(db, user.id)
        db.add(ActionLog(action="system.password_reset", payload=user.username))
        db.commit()
        # Close the reset window unless the device is mid-onboarding on its hotspot.
        if not (_onboarding_service and _onboarding_service.is_hotspot_active()):
            _setup_mode_service.exit_setup_mode()
    return {"ok": True, "message": "If that account exists, its password has been reset. You can now log in."}
