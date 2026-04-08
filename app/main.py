from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi.staticfiles import StaticFiles

from .auth import hash_password
from .config import settings
from .database import Base, engine, get_db
from .models import ActionLog, DeviceConfig, User
from .routes.web import router as web_router, set_runtime_services
from .schemas import HardwareCommand, OnboardingPayload, SetupStatus
from .services.button_service import SetupButtonService
from .services.camera_service import CameraService
from .services.esp32_link import ESP32Link
from .services.hardware_service import HardwareService
from .routes.web import router as web_router

app = FastAPI(title="Incubator v3 API")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(web_router)

link = ESP32Link(settings.serial_port, settings.serial_baud, settings.serial_timeout)
camera_service = CameraService(link)
hardware_service = HardwareService(link, camera_service)
setup_mode_service = SetupModeService()
wifi_service = WiFiService()
button_service = SetupButtonService(
    hold_seconds=settings.setup_button_hold_seconds,
    callback=lambda reason: setup_mode_service.enter_setup_mode(reason),
)
set_runtime_services(setup_mode_service=setup_mode_service, wifi_service=wifi_service)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        if not config:
            db.add(DeviceConfig(device_id="UNOQ-UNCLAIMED", claimed=False, claim_code="PAIR-1234"))
            db.commit()
    finally:
        db.close()

    button_service.start()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "incubator-v3", "setup_mode": setup_mode_service.is_setup_mode()}


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

    db.add(
        User(
            username=payload.username,
            email=payload.email,
            password_hash=hash_password(payload.password),
            role="owner",
        )
    )
    config.claimed = True
    config.device_name = payload.device_name
    config.farm_name = payload.farm_name
    config.wifi_ssid = payload.wifi_ssid

    db.add(ActionLog(action="setup_complete", payload=payload.model_dump_json(exclude={"password", "wifi_password"})))
    db.commit()
    return {"ok": True, "claimed": True, "device_name": config.device_name}


@app.post("/hardware/send")
def send_hardware_command(payload: HardwareCommand, db: Session = Depends(get_db)) -> dict:
    handler = {
        "open_lock": hardware_service.open_lock,
        "close_lock": hardware_service.close_lock,
        "open_door": hardware_service.open_door,
        "close_door": hardware_service.close_door,
        "move_motor": lambda: hardware_service.move_motor(payload.value if payload.value is not None else 0),
        "read_temp": hardware_service.read_temp,
        "read_humidity": hardware_service.read_humidity,
        "set_candle": lambda: hardware_service.set_candle(str(payload.value).lower() in {"1", "true", "on"}),
        "capture_image": hardware_service.capture_image,
    }.get(payload.action)

    if not handler:
        raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")

    response = handler()
    db.add(ActionLog(action=payload.action, payload=str(payload.value)))
    db.commit()
    return response
