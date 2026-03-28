from datetime import datetime
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import clear_session, create_session, get_user_id_from_session, hash_password, verify_password
from .config import settings
from .database import Base, engine, get_db
from .models import ActionLog, DeviceConfig, Incubator, SensorLog, User
from .schemas import ControlPayload, LoginPayload, OnboardingPayload, SettingsPayload, SetupStatus
from .services.camera_service import CameraService
from .services.esp32_link import ESP32Link
from .services.hardware_service import HardwareService
from .settings_store import ensure_defaults, get_settings, update_settings

app = FastAPI(title="Incubator v3 API")

link = ESP32Link(settings.serial_port, settings.serial_baud, settings.serial_timeout)
camera_service = CameraService(link)
hardware_service = HardwareService(link, camera_service)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _require_user(db: Session, token: str | None) -> User:
    user_id = get_user_id_from_session(db, token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return user


def _latest_environment(db: Session) -> tuple[dict, bool]:
    temp_resp = hardware_service.read_temp()
    humidity_resp = hardware_service.read_humidity()

    hw_online = bool(temp_resp.get("hardware_online", True) and humidity_resp.get("hardware_online", True))
    temp = temp_resp.get("value") if temp_resp.get("ok") else None
    humidity = humidity_resp.get("value") if humidity_resp.get("ok") else None

    if temp is None or humidity is None:
        latest = db.scalar(select(SensorLog).order_by(SensorLog.captured_at.desc()))
        if latest:
            temp = latest.temperature_c
            humidity = latest.humidity_pct
            captured_at = latest.captured_at.isoformat()
            stale = True
        else:
            temp = None
            humidity = None
            captured_at = None
            stale = True
    else:
        incubator = db.scalar(select(Incubator).limit(1))
        if incubator:
            db.add(SensorLog(incubator_id=incubator.id, temperature_c=float(temp), humidity_pct=float(humidity)))
            db.commit()
        captured_at = datetime.utcnow().isoformat()
        stale = False

    return {
        "temperature_c": temp,
        "humidity_pct": humidity,
        "captured_at": captured_at,
        "stale": stale,
        "hardware_online": hw_online,
    }, hw_online


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        if not config:
            db.add(DeviceConfig(device_id="UNOQ-UNCLAIMED", claimed=False, claim_code="PAIR-1234"))
        incubator = db.scalar(select(Incubator).limit(1))
        if not incubator:
            db.add(Incubator(name="Main Incubator", status="idle"))
        db.commit()
        ensure_defaults(db)
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    if not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    app_settings = get_settings(db)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "settings": app_settings,
            "poll_seconds": 5,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    if not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("settings.html", {"request": request, "settings": get_settings(db)})


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    if not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("status.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request):
    return templates.TemplateResponse("onboarding.html", {"request": request})


@app.post("/api/login")
def api_login(payload: LoginPayload, response: Response, db: Session = Depends(get_db)) -> dict:
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_session(db, user.id)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=settings.session_secure_cookie,
        samesite="lax",
        max_age=settings.session_hours * 3600,
    )
    return {"ok": True}


@app.post("/api/logout")
def api_logout(response: Response, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    clear_session(db, session_token)
    response.delete_cookie(settings.session_cookie_name)
    return {"ok": True}


@app.get("/api/health")
def api_health() -> dict:
    return {"ok": True, "service": "incubator-v3", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/status")
def api_status(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    env, hw_online = _latest_environment(db)
    app_settings = get_settings(db)
    alarms = []
    if env["stale"]:
        alarms.append("Sensor data is stale")
    if not hw_online:
        alarms.append("ESP32 link offline")

    return {
        "ok": True,
        "state": "warning" if alarms else "safe",
        "hardware_online": hw_online,
        "heater": app_settings.get("heater_enabled") == "true",
        "fan": app_settings.get("fan_enabled") == "true",
        "turner": app_settings.get("turner_enabled") == "true",
        "alarm_enabled": app_settings.get("alarm_enabled") == "true",
        "alarms": alarms,
        "last_updated": env.get("captured_at"),
    }


@app.get("/api/environment")
def api_environment(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    env, _ = _latest_environment(db)
    app_settings = get_settings(db)
    env["target_temp_c"] = float(app_settings.get("target_temp_c", "37.5"))
    env["target_humidity_pct"] = float(app_settings.get("target_humidity_pct", "55"))
    return env


@app.get("/api/settings")
def api_get_settings(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    return {"ok": True, "settings": get_settings(db)}


@app.post("/api/settings")
def api_set_settings(payload: SettingsPayload, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    updates = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in payload.model_dump().items()}
    updated = update_settings(db, updates)
    db.add(ActionLog(action="settings_update", payload=str(payload.model_dump())))
    db.commit()
    return {"ok": True, "settings": updated}


@app.post("/api/control/heater")
def api_control_heater(payload: ControlPayload, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    update_settings(db, {"heater_enabled": str(payload.enabled).lower()})
    resp = hardware_service.set_candle(payload.enabled)
    db.add(ActionLog(action="heater", payload=str(payload.enabled)))
    db.commit()
    return {"ok": True, "command": resp}


@app.post("/api/control/fan")
def api_control_fan(payload: ControlPayload, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    update_settings(db, {"fan_enabled": str(payload.enabled).lower()})
    resp = hardware_service.link.send_command("set_fan", "on" if payload.enabled else "off")
    db.add(ActionLog(action="fan", payload=str(payload.enabled)))
    db.commit()
    return {"ok": True, "command": resp}


@app.post("/api/control/turn")
def api_control_turn(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    resp = hardware_service.move_motor("turn_cycle")
    db.add(ActionLog(action="turn_cycle", payload="manual"))
    db.commit()
    return {"ok": True, "command": resp}


@app.get("/health")
def health_alias() -> dict:
    return api_health()


@app.get("/setup/status", response_model=SetupStatus)
def setup_status(db: Session = Depends(get_db)) -> SetupStatus:
    config = db.scalar(select(DeviceConfig).limit(1))
    if not config:
        raise HTTPException(status_code=500, detail="Device config missing")
    return SetupStatus(device_id=config.device_id, claimed=config.claimed)


@app.post("/setup/complete")
def complete_setup(payload: OnboardingPayload, db: Session = Depends(get_db)) -> dict:
    config = db.scalar(select(DeviceConfig).limit(1))
    if not config:
        raise HTTPException(status_code=500, detail="Device config missing")
    if config.claimed:
        raise HTTPException(status_code=409, detail="Device is already claimed")
    if payload.pairing_code != config.claim_code:
        raise HTTPException(status_code=401, detail="Invalid pairing code")

    existing = db.scalar(select(User).where((User.username == payload.username) | (User.email == payload.email)))
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")

    db.add(User(username=payload.username, email=payload.email, password_hash=hash_password(payload.password), role="owner"))
    config.claimed = True
    config.device_name = payload.device_name
    config.farm_name = payload.farm_name
    config.wifi_ssid = payload.wifi_ssid

    db.add(ActionLog(action="setup_complete", payload=payload.model_dump_json(exclude={"password", "wifi_password"})))
    db.commit()
    return {"ok": True, "claimed": True, "device_name": config.device_name}


@app.exception_handler(401)
def unauthorized_handler(_: Request, __):
    return JSONResponse(status_code=401, content={"ok": False, "error": "Unauthorized"})
