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
from .models import ActionLog, DeviceConfig, Incubator, User
from .providers.factory import build_provider
from .schemas import (
    ControlPayload,
    DeviceModeResponse,
    EnvironmentResponse,
    HealthResponse,
    LoginPayload,
    OnboardingPayload,
    SettingsPayload,
    SetupStatus,
    StatusResponse,
)
from .services.camera_service import CameraService
from .services.esp32_link import ESP32Link
from .services.hardware_service import HardwareService
from .services.inference_service import InferenceService
from .settings_store import ensure_defaults, get_settings, update_settings

app = FastAPI(title="Incubator v3 API")

link = ESP32Link(settings.serial_port, settings.serial_baud, settings.serial_timeout)
camera_service = CameraService(link)
hardware_service = HardwareService(link, camera_service)
hardware_provider = build_provider(hardware_service)
inference_service = InferenceService()

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _require_user(db: Session, token: str | None) -> User | None:
    if not settings.require_login:
        return None
    user_id = get_user_id_from_session(db, token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return user


def _read_state(db: Session) -> dict:
    env = hardware_provider.read_environment()
    current_settings = get_settings(db)

    # keep provider targets synced from persisted settings
    try:
        hardware_provider.set_targets(  # type: ignore[attr-defined]
            float(current_settings.get("target_temp_c", env.target_temp_c)),
            float(current_settings.get("target_humidity_pct", env.target_humidity_pct)),
        )
    except Exception:
        pass

    stale = (datetime.utcnow() - env.last_updated).total_seconds() > int(current_settings.get("refresh_interval_sec", "5")) * 3
    temp_delta_limit = float(current_settings.get("alarm_temp_delta_c", "1.0"))
    hum_delta_limit = float(current_settings.get("alarm_humidity_delta_pct", "8.0"))

    alarms = []
    if stale:
        alarms.append("Data is stale")
    if not env.hardware_online:
        alarms.append("Hardware bridge offline")
    if not env.sensor_online:
        alarms.append("Sensor read unavailable")
    if abs(env.temperature_c - env.target_temp_c) > temp_delta_limit:
        alarms.append("Temperature outside threshold")
    if abs(env.humidity_pct - env.target_humidity_pct) > hum_delta_limit:
        alarms.append("Humidity outside threshold")
    if env.alarm_active:
        alarms.append("Provider alarm state active")

    return {
        "env": env,
        "alarms": alarms,
        "stale": stale,
    }


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        if not db.scalar(select(DeviceConfig).limit(1)):
            db.add(DeviceConfig(device_id="UNOQ-UNCLAIMED", claimed=False, claim_code="PAIR-1234"))
        if not db.scalar(select(Incubator).limit(1)):
            db.add(Incubator(name="Main Incubator", status="idle"))
        db.commit()
        ensure_defaults(db)
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    if settings.require_login and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    try:
        return templates.TemplateResponse("dashboard.html", {"request": request, "settings": get_settings(db)})
    except Exception:
        # Never return 404 for root if templates are mis-packaged; fall back to minimal dashboard shell.
        return HTMLResponse("<html><body><h1>Incubator Dashboard</h1><p>Template load failed. Check deployment packaging.</p></body></html>", status_code=200)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    if settings.require_login and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("settings.html", {"request": request, "settings": get_settings(db)})


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)):
    if settings.require_login and not get_user_id_from_session(db, session_token):
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


@app.get("/api/health", response_model=HealthResponse)
def api_health() -> HealthResponse:
    return HealthResponse(ok=True, service=f"incubator-v3-{settings.app_version}", timestamp=datetime.utcnow().isoformat())


@app.get("/api/device-mode", response_model=DeviceModeResponse)
def api_device_mode() -> DeviceModeResponse:
    simulated = settings.device_mode != "hardware"
    return DeviceModeResponse(mode=settings.device_mode, simulated_mode=simulated)


@app.get("/api/status", response_model=StatusResponse)
def api_status(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> StatusResponse:
    _require_user(db, session_token)
    state = _read_state(db)
    env = state["env"]
    app_settings = get_settings(db)
    return StatusResponse(
        ok=True,
        state="warning" if state["alarms"] else "safe",
        hardware_online=env.hardware_online,
        sensor_online=env.sensor_online,
        simulated_mode=env.simulated_mode,
        heater=env.heater_on,
        fan=env.fan_on,
        turner=env.turner_on,
        alarm_enabled=app_settings.get("alarm_enabled", "true") == "true",
        alarms=state["alarms"],
        last_updated=env.last_updated.isoformat(),
    )


@app.get("/api/environment", response_model=EnvironmentResponse)
def api_environment(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> EnvironmentResponse:
    _require_user(db, session_token)
    state = _read_state(db)
    env = state["env"]
    return EnvironmentResponse(
        temperature_c=env.temperature_c,
        humidity_pct=env.humidity_pct,
        target_temp_c=env.target_temp_c,
        target_humidity_pct=env.target_humidity_pct,
        stale=state["stale"],
        hardware_online=env.hardware_online,
        sensor_online=env.sensor_online,
        simulated_mode=env.simulated_mode,
        captured_at=env.last_updated.isoformat(),
    )


@app.get("/api/settings")
def api_get_settings(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    settings_data = get_settings(db)
    settings_data["device_mode"] = settings.device_mode
    return {"ok": True, "settings": settings_data, "inference": inference_service.health(), "app_version": settings.app_version}


@app.post("/api/settings")
def api_set_settings(payload: SettingsPayload, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    updates = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in payload.model_dump().items()}
    updated = update_settings(db, updates)
    try:
        hardware_provider.set_targets(payload.target_temp_c, payload.target_humidity_pct)  # type: ignore[attr-defined]
    except Exception:
        pass
    db.add(ActionLog(action="settings_update", payload=str(payload.model_dump())))
    db.commit()
    return {"ok": True, "settings": updated}




@app.post("/api/viability/predict")
def api_viability_predict() -> dict:
    return {"ok": False, "enabled": False, "detail": "Inference module not enabled yet"}

@app.post("/api/control/heater")
def api_control_heater(payload: ControlPayload, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    result = hardware_provider.set_heater(payload.enabled)
    update_settings(db, {"heater_enabled": str(payload.enabled).lower()})
    db.add(ActionLog(action="heater", payload=str(payload.enabled)))
    db.commit()
    return {"ok": result.ok, "message": result.message}


@app.post("/api/control/fan")
def api_control_fan(payload: ControlPayload, db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    result = hardware_provider.set_fan(payload.enabled)
    update_settings(db, {"fan_enabled": str(payload.enabled).lower()})
    db.add(ActionLog(action="fan", payload=str(payload.enabled)))
    db.commit()
    return {"ok": result.ok, "message": result.message}


@app.post("/api/control/turn")
def api_control_turn(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    result = hardware_provider.run_turn_cycle()
    db.add(ActionLog(action="turn_cycle", payload="manual"))
    db.commit()
    return {"ok": result.ok, "message": result.message}


@app.post("/api/control/reset-alarm")
def api_reset_alarm(db: Session = Depends(get_db), session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name)) -> dict:
    _require_user(db, session_token)
    result = hardware_provider.reset_alarm()
    db.add(ActionLog(action="alarm_reset", payload="manual"))
    db.commit()
    return {"ok": result.ok, "message": result.message}


@app.get("/health")
def health_alias() -> HealthResponse:
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
