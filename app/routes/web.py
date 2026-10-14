from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Request, Response, status
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
from ..schemas import FactoryResetPayload, HotspotSetupPayload
from ..services.ai_service import AIService
from ..services.onboarding_service import candidate_urls, lan_ips, short_hostname
from ..services.setup_mode_service import PHYSICAL_REASONS
from ..settings_store import get_settings, update_settings
from ._auth import auth_redirect as _auth_redirect
from ._auth import require_api_user as _require_api_user

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

PING_PATH = "/api/setup/ping"
# A physical button hold opens the onboarding routes to the LAN for this long.
PRESENCE_WINDOW_SECONDS = 600
# Daemon state older than this is shown as "nothing asserted" (OFF).
_DAEMON_STATE_STALE_SECONDS = 120.0

_setup_mode_service: SetupModeService | None = None
_wifi_service: WiFiService | None = None
_onboarding_service: OnboardingService | None = None
_hardware_service: HardwareService | None = None
_vision_service: VisionService | None = None
_alert_service: AlertService | None = None
_gpio_service: Any = None
_camera_service: Any = None


def set_runtime_services(
    setup_mode_service=None,
    wifi_service=None,
    onboarding_service=None,
    hardware_service=None,
    vision_service=None,
    alert_service=None,
    gpio_service=None,
    camera_service=None,
) -> None:
    global _setup_mode_service, _wifi_service, _onboarding_service, _hardware_service, _vision_service, _alert_service
    global _gpio_service, _camera_service
    _setup_mode_service = setup_mode_service
    _wifi_service = wifi_service
    _onboarding_service = onboarding_service
    _hardware_service = hardware_service
    _vision_service = vision_service
    _alert_service = alert_service
    _gpio_service = gpio_service
    _camera_service = camera_service


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


def _render(request: Request, name: str, context: dict[str, Any]):
    return templates.TemplateResponse(request=request, name=name, context=context)


def _is_setup_complete(config: DeviceConfig | None) -> bool:
    # Account creation is optional — treat WiFi + device name as sufficient for "complete"
    return bool(config and config.device_name and config.wifi_ssid)


def _get_bool_setting(app_settings: dict[str, str], key: str, default: bool) -> bool:
    return app_settings.get(key, "true" if default else "false").lower() == "true"


def _hotspot_active() -> bool:
    return bool(_onboarding_service and _onboarding_service.is_hotspot_active())


def _setup_mode_on() -> bool:
    return bool(_setup_mode_service and _setup_mode_service.is_setup_mode())


def _daemon_output_state() -> dict[str, Any] | None:
    """Heater/fan/turner as published by the control daemon, or None when the
    web process owns those pins itself.

    OFF on any doubt: a missing or stale state file means the daemon is not
    asserting anything we can honestly display.
    """
    if not settings.control_daemon_enabled:
        return None
    try:
        from ..control.bridge import read_control_state

        state = read_control_state(settings.control_state_path) or {}
    except Exception as exc:  # noqa: BLE001 — never break a page on this
        logger.debug("control state unreadable: %s", exc)
        state = {}
    fresh = bool(state) and (time.time() - float(state.get("ts") or 0.0)) <= _DAEMON_STATE_STALE_SECONDS
    return {
        "heater": bool(state.get("heater_on")) if fresh else False,
        "fan": bool(state.get("fan_on")) if fresh else False,
        "turned": bool(state.get("turned")) if fresh else False,
        "last_turn_ts": state.get("last_turn_ts") if fresh else None,
        "fresh": fresh,
        "source": "control-daemon",
    }


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
    "system.factory_reset": "Factory reset performed",
    "system.factory_reset_requested": "Factory reset requested",
    "system.wifi_join_failed": "Could not join the Wi-Fi network",
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
    from ..settings_store import effective_ap_password

    redirect = _auth_redirect(db, session_token)
    if redirect:
        return redirect

    config = db.scalar(select(DeviceConfig).limit(1))
    app_settings = get_settings(db)
    setup_mode = _setup_mode_on()
    hotspot_active = _hotspot_active()
    ap_ssid = ""
    if _onboarding_service and config:
        ap_ssid = _onboarding_service.ap_ssid(config.device_id)

    device_name = "Incubator"
    device_id = "unclaimed"
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
        "heater_enabled": _get_bool_setting(app_settings, "heater_enabled", default=False),
        "fan_enabled": _get_bool_setting(app_settings, "fan_enabled", default=True),
    }
    # When the control daemon owns heater/fan, show what it is REALLY doing
    # (its published state), not this process's idle GPIO flags / the toggles.
    daemon_state = _daemon_output_state()
    if daemon_state is not None:
        snapshot["heater"] = daemon_state["heater"]
        snapshot["fan"] = daemon_state["fan"]
        snapshot["last_turn_ts"] = daemon_state["last_turn_ts"]
        snapshot["daemon_fresh"] = daemon_state["fresh"]
    ai_insight = None
    if sensor["temperature_c"] is not None and sensor["humidity_pct"] is not None:
        ai_insight = ai_service.generate_dashboard_insight(sensor["temperature_c"], sensor["humidity_pct"])

    # Determine next-step card
    if hotspot_active or setup_mode:
        current_state_summary = "Hotspot active — waiting for setup"
        # DB-resolved password: the one the AP actually uses (blank = open).
        ap_password = effective_ap_password(db)
        _ap_cred = f"(password: {ap_password}) " if ap_password else "(open network) "
        state_detail = f"Connect to Wi-Fi '{ap_ssid}' {_ap_cred}then open http://{settings.ap_ip}:8000"
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

    quick_actions = []
    if not setup_complete or hotspot_active or setup_mode:
        quick_actions.append(
            {"label": "Start Setup", "detail": "Local hotspot onboarding", "href": "/onboarding", "tone": "primary"}
        )
    quick_actions += [
        {"label": "Open Status", "detail": "Live readings", "href": "/status", "tone": "primary" if setup_complete else "secondary"},
        {"label": "Candle Eggs", "detail": "Capture + analyze image", "href": "/hardware#candle", "tone": "secondary"},
        {"label": "Controls", "detail": "Lock, door, turner, candling", "href": "/hardware", "tone": "secondary"},
        {"label": "Help", "detail": "Guides and troubleshooting", "href": "/help", "tone": "ghost"},
        {"label": "Settings", "detail": "Targets and controls", "href": "/settings", "tone": "ghost"},
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
                # Durable address to reach this incubator on the home network
                # (Avahi advertises the first hostname label only).
                "access_url": f"http://{short_hostname()}.local:8000",
                "network_state": network_state,
                "network_detail": network_detail,
                "current_state_summary": current_state_summary,
                "state_detail": state_detail,
            },
            "next_step": next_step,
            "quick_actions": quick_actions,
            "vision_backend": settings.vision_backend,
            "camera_stream_enabled": settings.camera_stream_enabled,
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

    setup_mode = _setup_mode_on()
    hw_state: dict[str, Any] = dict(_hardware_service.get_state()) if _hardware_service else {}
    # The web process never drives heater/fan/turner under the control daemon,
    # so its own flags would read OFF forever — merge the daemon's state.
    daemon_state = _daemon_output_state()
    if daemon_state is not None:
        hw_state.update(
            heater=daemon_state["heater"],
            fan=daemon_state["fan"],
            turned=daemon_state["turned"],
            last_turn_ts=daemon_state["last_turn_ts"],
            daemon_fresh=daemon_state["fresh"],
            source="control-daemon",
        )
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

    health: dict[str, Any] = {
        "hardware": "online" if _hardware_service else "unavailable",
        "sensors": sensor_status,
        "alarms": alarms,
        "alarms_tone": alarms_tone,
        "gpio_mock": bool(getattr(_gpio_service, "is_mock", settings.gpio_mock)),
        "setup_mode": "on" if setup_mode else "off",
        "vision_backend": settings.vision_backend,
        "camera_backend": settings.camera_backend,
        "dht_pin": settings.gpio_dht_pin,
        "control_daemon": settings.control_daemon_enabled,
    }
    try:
        from ..services.device_service import device_info

        info = device_info(gpio_service=_gpio_service, camera_service=_camera_service)
        health["board"] = info.get("board") or {}
        health["gpio_backend"] = info.get("gpio_backend")
        health["sensor_backend"] = info.get("sensor_backend")
        health["camera_backend"] = info.get("camera_backend") or settings.camera_backend
    except Exception as exc:  # noqa: BLE001 — chips are optional
        logger.debug("device_info for status page skipped: %s", exc)

    return _render(
        request=request,
        name="status.html",
        context={
            "version": settings.app_version,
            "health": health,
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
            "factory_reset_hold_seconds": int(settings.factory_reset_hold_seconds),
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


# ------------------------------------------------------------------
# Onboarding access rule
# ------------------------------------------------------------------
#
# The wizard routes (/onboarding, /onboarding/start, /onboarding/wifi-scan,
# /onboarding/complete) are usable WITHOUT a login only when
#   * no account exists yet (first run), or
#   * the setup hotspot is actually up (the caller is physically on the AP), or
#   * setup mode was entered by the physical button / first boot within the
#     last PRESENCE_WINDOW_SECONDS.
# Otherwise they require a valid session like every other API. Without this,
# anyone on the home LAN could create a second owner account, knock the device
# off Wi-Fi, or flip setup mode (which unlocks /api/reset-password).

def _onboarding_open(db: Session) -> bool:
    if not has_any_user(db):
        return True
    if _hotspot_active():
        return True
    recent = getattr(_setup_mode_service, "entered_recently", None)
    if callable(recent):
        try:
            if recent(PRESENCE_WINDOW_SECONDS, PHYSICAL_REASONS):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("entered_recently failed: %s", exc)
    return False


def _onboarding_allowed(db: Session, session_token: str | None) -> bool:
    return _onboarding_open(db) or bool(get_user_id_from_session(db, session_token))


def _require_onboarding_access(db: Session, session_token: str | None) -> None:
    if not _onboarding_allowed(db, session_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in, or hold the setup button on the device, to run setup again.",
        )


def _touch_onboarding() -> None:
    """Every wizard request counts as activity: fallback retries wait, the idle
    timeout is pushed back, and the AP is never dropped under a phone mid-wizard."""
    touch = getattr(_onboarding_service, "touch_activity", None)
    if callable(touch):
        try:
            touch()
        except Exception as exc:  # noqa: BLE001
            logger.debug("touch_activity failed: %s", exc)


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(
    request: Request,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
):
    from ..settings_store import effective_ap_password

    if not _onboarding_allowed(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    _touch_onboarding()

    # Show the REAL setup-network name so the operator can tell it apart from any
    # stale "Incubator-…" networks their phone remembers from earlier flashes.
    ap_ssid = settings.ap_ssid_prefix
    config = db.scalar(select(DeviceConfig).limit(1))
    if _onboarding_service and config and config.device_id:
        ap_ssid = _onboarding_service.ap_ssid(config.device_id)

    has_account = has_any_user(db)
    # Recovery mode: an account exists but no Wi-Fi is saved — the last join
    # failed (a failed join clears wifi_ssid) and the setup AP came back. The
    # wizard then asks ONLY for the Wi-Fi, with the failed network preselected,
    # instead of walking the whole first-run flow again.
    retry_mode = bool(config and not config.wifi_ssid and has_account)
    retry_ssid = ""
    if retry_mode:
        last_fail = db.scalar(
            select(ActionLog)
            .where(ActionLog.action == "system.wifi_join_failed")
            .order_by(ActionLog.id.desc())
            .limit(1)
        )
        retry_ssid = (last_fail.payload or "") if last_fail else ""

    hotspot_active = _hotspot_active()   # accurate: the wizard POSTs /onboarding/start on load only when in setup
    # A device that has never been set up (no Wi-Fi saved, no account) is
    # always "in setup" — the wizard must run even when it is reached over
    # Ethernet or on a dev laptop with the auto-hotspot disabled.
    never_set_up = not (config and config.wifi_ssid) and not has_account
    setup_mode = _setup_mode_on() or never_set_up
    hostname = short_hostname()

    return _render(
        request=request,
        name="onboarding.html",
        context={
            "version": settings.app_version,
            "ap_ip": settings.ap_ip,
            # DB-authoritative: blank means the setup network is open. Only
            # revealed to a client that is already on the AP (it needed the key
            # to get here) — never to the home LAN.
            "ap_password": effective_ap_password(db) if hotspot_active else "",
            "ap_ssid_prefix": settings.ap_ssid_prefix,
            "ap_ssid": ap_ssid,
            "retry_mode": retry_mode,
            "retry_ssid": retry_ssid,
            "device_name": (config.device_name or "") if config else "",
            "hotspot_active": hotspot_active,
            "setup_mode": setup_mode,
            "has_account": has_account,
            "hostname": hostname,
            "candidate_urls": candidate_urls(hostname),
            "ping_path": PING_PATH,
        },
    )


# ------------------------------------------------------------------
# Onboarding API
# ------------------------------------------------------------------

@router.post("/onboarding/start")
def onboarding_start(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_onboarding_access(db, session_token)
    _touch_onboarding()
    config = db.scalar(select(DeviceConfig).limit(1))
    device_id = config.device_id if config else "PI-XXXX"
    if _onboarding_service:
        return _onboarding_service.start_manual_hotspot(device_id, source="web")
    # Fallback if onboarding service not wired
    if _setup_mode_service:
        _setup_mode_service.enter_setup_mode("manual_trigger")
    if _wifi_service:
        _wifi_service.start_hotspot(f"{settings.ap_ssid_prefix}-{device_id[-4:]}", settings.ap_password)
    return {"ok": True, "ap_url": f"http://{settings.ap_ip}:8000", "ssid": f"{settings.ap_ssid_prefix}-{device_id[-4:]}"}


@router.get("/api/debug/network")
def api_debug_network(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    """NetworkManager snapshot for diagnosing duplicate/locked APs.

    Reachable during setup mode (no account exists yet) or when authenticated,
    so a field tech can see exactly how many APs exist and which are locked.
    """
    in_setup = _setup_mode_on()
    if not in_setup:
        _require_api_user(db, session_token)
    info = _wifi_service.network_debug() if _wifi_service else {"nmcli": False}
    return {"ok": True, "setup_mode": in_setup, "hotspot_active": _hotspot_active(), **info}


@router.get("/onboarding/wifi-scan")
def onboarding_wifi_scan(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_onboarding_access(db, session_token)
    _touch_onboarding()
    if _wifi_service:
        networks = _wifi_service.scan_networks()
        return {
            "ok": True,
            "networks": [{"ssid": n.ssid, "strength": n.strength, "secure": n.secure} for n in networks],
        }
    return {"ok": False, "networks": []}


def _complete_with_recovery(ssid: str, password: str) -> None:
    """Run the AP→client switch and undo the config if the join fails.

    The wizard saves wifi_ssid BEFORE the join attempt (the response must go
    out before the radio flips). If the join then fails, a device that kept
    that ssid would boot forever thinking it is configured and never raise its
    setup AP — a stranded box. Clearing the ssid keeps boot()'s "needs setup"
    logic truthful; OnboardingService.complete() additionally re-raises the AP
    immediately so recovery doesn't even need a power cycle.
    """
    connected = _onboarding_service.complete(ssid, password) if _onboarding_service else False
    if ssid and not connected:
        db = next(get_db())
        try:
            config = db.scalar(select(DeviceConfig).limit(1))
            if config is not None and config.wifi_ssid == ssid:
                config.wifi_ssid = None
                db.add(ActionLog(action="system.wifi_join_failed", payload=ssid))
                db.commit()
        except Exception:  # noqa: BLE001 — recovery bookkeeping must never raise
            pass
        finally:
            db.close()


@router.post("/onboarding/complete")
def onboarding_complete(
    payload: HotspotSetupPayload,
    response: Response,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_onboarding_access(db, session_token)
    _touch_onboarding()
    config = db.scalar(select(DeviceConfig).limit(1))
    if not config:
        config = DeviceConfig(device_id=f"PI-{__import__('uuid').uuid4().hex[:8].upper()}", claimed=False)
        db.add(config)

    new_user: User | None = None
    if payload.create_account and payload.username and payload.email and payload.password:
        # Once an owner exists, only a signed-in owner may add another account
        # — never an anonymous caller, even one standing on the setup AP.
        if has_any_user(db) and not get_user_id_from_session(db, session_token):
            raise HTTPException(
                status_code=409,
                detail="This incubator already has an account. Sign in to add another one.",
            )
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

    config.device_name = payload.device_name
    config.wifi_ssid = payload.ssid or None

    db.add(ActionLog(action="onboarding_complete", payload=f"device={payload.device_name},ssid={payload.ssid}"))
    db.commit()

    # Log the new owner straight in so they are not locked out by the account
    # they just created.
    if new_user is not None:
        _set_session_cookie(response, create_session(db, new_user.id, settings.session_ttl_seconds))

    # Switch from AP to client Wi-Fi AFTER this response is sent. On the
    # single-radio Pi the switch tears down the very hotspot serving this
    # request, so doing it inline would drop the reply and hang the wizard.
    # A background task runs once the response is on the wire.
    if _onboarding_service:
        background_tasks.add_task(
            _complete_with_recovery, payload.ssid or "", payload.wifi_password or ""
        )
    elif _setup_mode_service:
        _setup_mode_service.exit_setup_mode()
        if _wifi_service and payload.ssid:
            background_tasks.add_task(
                _wifi_service.connect_client, payload.ssid, payload.wifi_password or ""
            )

    # Where to reach the incubator once it's on the home network. Avahi
    # advertises <hostname>.local (first label only), so these URLs stay valid
    # after the setup hotspot goes away and the operator rejoins their Wi-Fi.
    hostname = short_hostname()
    return {
        "ok": True,
        "device_name": payload.device_name,
        "claimed": bool(config.claimed),
        # Reliable target for the immediate auto-open: the phone is still on the
        # setup hotspot right now, so the fixed AP IP always resolves (no mDNS,
        # which Android browsers often can't do).
        "open_url": f"http://{settings.ap_ip}:8000",
        # Durable address for daily use once the device is on the home network
        # (Bonjour/Avahi name). This is the one to bookmark.
        "app_url": f"http://{hostname}.local:8000",
        "hostname": hostname,
        "candidate_urls": candidate_urls(hostname),
        "ping_path": PING_PATH,
        "wifi_ssid": payload.ssid or "",
    }


# ------------------------------------------------------------------
# Device discovery ping — probed cross-origin by the wizard after the switch
# ------------------------------------------------------------------

_PING_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "600",
    "Cache-Control": "no-store",
}


@router.get(PING_PATH)
def api_setup_ping(response: Response, db: Session = Depends(get_db)) -> dict:
    """No auth: the setup wizard (served from the AP origin) polls this on the
    home-network candidates to find the device. Reveals only what the mDNS
    advertisement and the login page already show."""
    config = db.scalar(select(DeviceConfig).limit(1))
    for key, value in _PING_HEADERS.items():
        response.headers[key] = value
    return {
        "ok": True,
        "device_id": config.device_id if config else None,
        "device_name": (config.device_name or "") if config else "",
        "hostname": short_hostname(),
        "version": settings.app_version,
        "ips": lan_ips(),
        "setup_mode": _setup_mode_on(),
        "hotspot_active": _hotspot_active(),
    }


@router.options(PING_PATH)
def api_setup_ping_options() -> Response:
    return Response(status_code=204, headers=dict(_PING_HEADERS))


# ------------------------------------------------------------------
# Device: info card + factory reset
# ------------------------------------------------------------------

@router.get("/api/device/info")
def api_device_info(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    from ..services.device_service import device_info

    return device_info(gpio_service=_gpio_service, camera_service=_camera_service)


def _run_factory_reset(device_id: str) -> None:
    """Background task: wipe the device AFTER the response went out (the AP
    flip drops the connection that asked for it)."""
    from ..services.device_service import factory_reset

    db = next(get_db())
    try:
        factory_reset(
            db,
            wifi_service=_wifi_service,
            onboarding_service=_onboarding_service,
            setup_mode_service=_setup_mode_service,
            device_id=device_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Factory reset failed: %s", exc)
    finally:
        db.close()


@router.post("/api/device/factory-reset")
def api_device_factory_reset(
    payload: FactoryResetPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    if (payload.confirm or "").strip() != "RESET":
        raise HTTPException(status_code=400, detail="Type RESET to confirm a factory reset.")
    config = db.scalar(select(DeviceConfig).limit(1))
    device_id = (config.device_id if config and config.device_id else "") or settings.device_id or "PI-XXXX"
    db.add(ActionLog(action="system.factory_reset_requested", payload=f"device={device_id}"))
    db.commit()
    background_tasks.add_task(_run_factory_reset, device_id)
    return {
        "ok": True,
        "device_id": device_id,
        "message": "Factory reset started — the incubator wipes its name, login, Wi-Fi and settings "
                   "and restarts in setup mode on its own Wi-Fi.",
    }


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


def _apply_output_toggle(output: str, enabled: bool) -> None:
    """Act on a heater/fan toggle right away — through the shared dispatcher,
    never by writing a relay pin from this process.

    When the control daemon owns those pins it re-reads ``*_enabled`` every
    iteration, so persisting the flag is enough; only an OFF is forwarded
    (fail-safe direction, immediate). Forcing ON from here would override the
    daemon's thermostat. Without the daemon the dispatcher drives the pin as
    before.
    """
    if settings.control_daemon_enabled and enabled:
        return
    try:
        from .. import main as _main   # lazy: main imports this module

        dispatch = getattr(_main, "execute_hardware_action", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("hardware dispatcher unavailable: %s", exc)
        return
    if not callable(dispatch):
        return
    try:
        dispatch(f"{output}_{'on' if enabled else 'off'}")
    except Exception as exc:  # noqa: BLE001 — the setting is saved either way
        logger.warning("Could not apply %s=%s: %s", output, enabled, exc)


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
    if payload.fan_enabled is not None:
        updates["fan_enabled"] = "true" if payload.fan_enabled else "false"
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
    # Persist first, then act: the daemon (or the dispatcher) sees the saved flag.
    if payload.heater_enabled is not None:
        _apply_output_toggle("heater", payload.heater_enabled)
    if payload.fan_enabled is not None:
        _apply_output_toggle("fan", payload.fan_enabled)
    return {"ok": True}


class APPasswordPayload(BaseModel):
    # 8–63 printable chars (WPA2 rule), or empty to make the setup network open.
    password: str = ""


@router.post("/api/settings/ap-password")
def api_set_ap_password(
    payload: APPasswordPayload,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    """Set (or clear) the setup-AP Wi-Fi password — optional, after setup.

    The setup network ships OPEN so any farmer can connect with one tap. Once
    things work, the operator can secure future setup sessions with a password
    here; clearing it returns the AP to open.
    """
    from ..settings_store import set_ap_password

    _require_api_user(db, session_token)
    pw = (payload.password or "").strip()
    if pw and not (8 <= len(pw) <= 63):
        raise HTTPException(status_code=400, detail="Wi-Fi password must be 8–63 characters (or blank for an open network).")
    set_ap_password(db, pw)
    return {"ok": True, "open": not bool(pw)}


class LoginPayload(BaseModel):
    username: str
    password: str


@router.post("/api/login")
def api_login(payload: LoginPayload, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    from ..auth import login_throttled, record_login_result

    client_ip = request.client.host if request.client else "unknown"
    wait = login_throttled(client_ip, payload.username)
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts — try again in {int(wait) + 1} seconds",
        )
    user = authenticate(db, payload.username, payload.password)
    record_login_result(client_ip, payload.username, success=user is not None)
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
# Update check — newer GitHub release available?
# ------------------------------------------------------------------

_GITHUB_LATEST_RELEASE = "https://api.github.com/repos/angads22/INCUBATOR_V3_DEV/releases/latest"
_UPDATE_TTL_SECONDS = 6 * 3600
# Process-wide cache so we poll GitHub at most a few times a day, and never on
# the request hot path once warm.
_update_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def _is_newer_version(latest: str, current: str) -> bool:
    """True when `latest` is a strictly newer M.mm version than `current`."""
    from ..version import parse_version

    try:
        return parse_version(latest) > parse_version(current)
    except (ValueError, AttributeError):
        return False


@router.get("/api/update-check")
def api_update_check() -> dict:
    """Report whether a newer release exists on GitHub.

    Cached ~6h and fully offline-safe: any network error (no internet on a
    first-boot appliance, rate limit, etc.) returns update_available=false so
    the UI simply stays quiet.
    """
    now = time.time()
    cached = _update_cache.get("data")
    if cached is not None and now - _update_cache["ts"] < _UPDATE_TTL_SECONDS:
        return cached

    result = {"update_available": False, "latest": None, "url": None, "notes": ""}
    try:
        import httpx

        resp = httpx.get(
            _GITHUB_LATEST_RELEASE,
            timeout=4.0,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "incubator-v3"},
        )
        if resp.status_code == 200:
            data = resp.json()
            tag = (data.get("tag_name") or "").strip()
            # Release tags look like "img-1.40-20260628"; pull the M.mm version.
            match = re.search(r"(\d+\.\d{2})", tag)
            latest = match.group(1) if match else tag
            result = {
                "update_available": _is_newer_version(latest, settings.app_version),
                "latest": latest or None,
                "url": data.get("html_url"),
                "notes": (data.get("body") or "").strip()[:280],
            }
            # Only cache successful lookups so a transient outage retries soon.
            _update_cache["data"] = result
            _update_cache["ts"] = now
    except Exception as exc:  # noqa: BLE001 — offline must never raise to the UI
        logger.debug("update-check skipped (offline or unavailable): %s", exc)
    return result


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
        "hotspot_active": _hotspot_active(),
        "switching": _network_switch_in_progress(),
    }


def _network_switch_in_progress() -> bool:
    probe = getattr(_onboarding_service, "transition_in_progress", None)
    try:
        return bool(probe()) if callable(probe) else False
    except Exception:  # noqa: BLE001
        return False


def _switch_with_recovery(ssid: str, password: str, previous_ssid: str | None) -> None:
    """Background: switch networks from the AP, then keep the DB truthful.

    The route saved the new ssid before answering. If the join fails and the
    device is back on its previous network, restore that ssid; otherwise clear
    it so the wizard opens in recovery mode on the returning AP.
    """
    connected = (
        _onboarding_service.switch_network(ssid, password, previous_ssid=previous_ssid)
        if _onboarding_service else False
    )
    if connected:
        return
    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        if config is not None:
            now_on = _wifi_service.get_connected_ssid() if _wifi_service else None
            config.wifi_ssid = previous_ssid if (previous_ssid and now_on == previous_ssid) else None
            db.add(ActionLog(action="system.wifi_join_failed", payload=ssid))
            db.commit()
    except Exception:  # noqa: BLE001 — recovery bookkeeping must never raise
        pass
    finally:
        db.close()


@router.post("/api/network/connect")
def api_network_connect(
    payload: NetworkConnectPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    _require_api_user(db, session_token)
    if _onboarding_service is None and _wifi_service is None:
        raise HTTPException(status_code=503, detail="Wi-Fi service unavailable")
    config = db.scalar(select(DeviceConfig).limit(1))
    previous_ssid = config.wifi_ssid if config else None

    if _onboarding_service is not None and _onboarding_service.is_hotspot_active():
        # The operator reached us over the setup AP. Tearing it down kills this
        # very connection, so answer first and do the AP → join → fallback
        # dance (OnboardingService.switch_network) once the reply is out.
        if config is not None:
            config.wifi_ssid = payload.ssid
        db.add(ActionLog(action="network_connect", payload=payload.ssid))
        db.commit()
        background_tasks.add_task(_switch_with_recovery, payload.ssid, payload.password or "", previous_ssid)
        return {
            "ok": True,
            "ssid": payload.ssid,
            "switching": True,
            "message": f"Switching to '{payload.ssid}' — the setup Wi-Fi turns off now. "
                       "If the join fails it comes back by itself.",
        }

    if _onboarding_service is not None:
        connected = _onboarding_service.switch_network(payload.ssid, payload.password or "", previous_ssid=previous_ssid)
    else:
        connected = _wifi_service.connect_client(payload.ssid, payload.password or "")
    if not connected:
        raise HTTPException(
            status_code=502,
            detail=f"Could not connect to '{payload.ssid}'. Check the password and try again.",
        )
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


def _current_ap_ssid(db: Session) -> str:
    """The setup-network name this device broadcasts (prefix-XXXX)."""
    config = db.scalar(select(DeviceConfig).limit(1))
    if _onboarding_service and config and config.device_id:
        return _onboarding_service.ap_ssid(config.device_id)
    return f"{settings.ap_ssid_prefix}-XXXX"


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, db: Session = Depends(get_db)):
    return _render(
        request=request,
        name="reset_password.html",
        context={
            "version": settings.app_version,
            "setup_mode": _setup_mode_on(),
            "hold_seconds": int(settings.setup_button_hold_seconds),
            # The reset page tells the operator which setup Wi-Fi to join and
            # where to reopen it from there.
            "ap_ssid": _current_ap_ssid(db),
            "ap_ip": settings.ap_ip,
        },
    )


@router.post("/api/reset-password")
def api_reset_password(payload: PasswordResetPayload, db: Session = Depends(get_db)) -> dict:
    """Reset an account password without a session.

    Gated on setup mode, which is entered by physically holding the device's
    setup button, during first-boot onboarding, or by a *signed-in* owner from
    /onboarding/start — that is the proof of presence. The response is
    identical whether or not the account exists, to avoid account enumeration.
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
        if not _hotspot_active():
            _setup_mode_service.exit_setup_mode()
    return {"ok": True, "message": "If that account exists, its password has been reset. You can now log in."}
