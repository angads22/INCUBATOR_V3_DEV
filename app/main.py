"""
Incubator v3 — Raspberry Pi Zero 2W edition.

Startup sequence:
  1. Ensure SQLite DB exists and tables are created.
  2. Generate device_id / claim_code if first boot.
  3. Ensure app_settings defaults are seeded.
  4. OnboardingService.boot() — if device is unclaimed, auto-starts WiFi AP.
  5. SetupButtonService.start() — watches physical button for manual AP trigger.
  6. GPIOService.setup() — initialises all pins.
  7. VisionService.setup() — loads TFLite model if configured.
  8. SensorPoller.start() — background thread reading DHT22 every N seconds.
  9. CloudService.register_device() — optional heartbeat to remote API.
"""

import logging
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .config import settings
from .database import Base, engine, get_db
from .models import ActionLog, DeviceConfig, SensorLog, User
from .routes.ai import router as ai_router, set_vision_hardware
from .routes.web import router as web_router, set_runtime_services
from .schemas import HardwareCommand, OnboardingPayload, SetupStatus
from .services.button_service import SetupButtonService
from .services.camera_service import CameraService
from .services.cloud_service import CloudService
from .services.gpio_service import GPIOService
from .services.hardware_service import HardwareService
from .services.onboarding_service import OnboardingService
from .services.setup_mode_service import SetupModeService
from .services.vision_service import VisionService
from .services.wifi_service import WiFiService
from .settings_store import ensure_defaults

app = FastAPI(title="Incubator v3 — Pi Zero 2W")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(web_router)
app.include_router(ai_router)

# ------------------------------------------------------------------
# Service wiring
# ------------------------------------------------------------------

gpio_service = GPIOService(
    dht_pin=settings.gpio_dht_pin,
    heater_pin=settings.gpio_heater_pin,
    fan_pin=settings.gpio_fan_pin,
    turner_pin=settings.gpio_turner_pin,
    turner_dir_pin=settings.gpio_turner_dir_pin,
    candle_pin=settings.gpio_candle_pin,
    alarm_pin=settings.gpio_alarm_pin,
    lock_pin=settings.gpio_lock_pin,
    door_pin=settings.gpio_door_pin,
    setup_button_pin=settings.gpio_setup_button_pin,
    relay_active_low=settings.gpio_relay_active_low,
    mock=settings.gpio_mock,
)

camera_service = CameraService(
    backend=settings.camera_backend,
    image_dir=settings.camera_image_dir,
    resolution=(settings.camera_resolution_w, settings.camera_resolution_h),
)

hardware_service = HardwareService(gpio=gpio_service, camera=camera_service)

vision_service = VisionService(
    backend=settings.vision_backend,
    tflite_model_path=settings.vision_tflite_model_path,
    api_url=settings.vision_api_url,
    api_key=settings.vision_api_key,
    confidence_threshold=settings.vision_confidence_threshold,
)

setup_mode_service = SetupModeService()
wifi_service = WiFiService()

onboarding_service = OnboardingService(
    wifi_service=wifi_service,
    setup_mode_service=setup_mode_service,
    ap_ssid_prefix=settings.ap_ssid_prefix,
    ap_password=settings.ap_password,
    ap_ip=settings.ap_ip,
    auto_hotspot=settings.auto_hotspot_on_unclaimed,
)

button_service = SetupButtonService(
    hold_seconds=settings.setup_button_hold_seconds,
    callback=lambda reason: _on_button_held(reason),
    gpio_pin=settings.gpio_setup_button_pin,
    mock_file=settings.button_mock_file,
)

cloud_service = CloudService()

set_runtime_services(
    setup_mode_service=setup_mode_service,
    wifi_service=wifi_service,
    onboarding_service=onboarding_service,
    hardware_service=hardware_service,
    vision_service=vision_service,
)
set_vision_hardware(vision=vision_service, hardware=hardware_service)


def _on_button_held(reason: str) -> None:
    """Callback from button_service — restart the AP for re-onboarding."""
    logger.info("Setup button held (%s) — starting manual hotspot", reason)
    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        device_id = config.device_id if config else "PI-XXXX"
    finally:
        db.close()
    onboarding_service.start_manual_hotspot(device_id)


# ------------------------------------------------------------------
# Sensor poller background thread
# ------------------------------------------------------------------

class _SensorPoller(threading.Thread):
    """Reads DHT22 every N seconds and persists to sensor_logs."""

    def __init__(self, interval: int) -> None:
        super().__init__(daemon=True, name="sensor-poller")
        self.interval = interval
        self._stop_flag = threading.Event()

    def run(self) -> None:
        # Wait for a settled reading before the first log
        time.sleep(5)
        while not self._stop_flag.wait(self.interval):
            self._poll()

    def _poll(self) -> None:
        db = next(get_db())
        try:
            reading = gpio_service.read_temperature_humidity()
            if not reading.get("ok"):
                return
            if not settings.sensor_log_to_db:
                return
            # Only log if at least one incubator row exists
            from sqlalchemy import text
            row = db.execute(text("SELECT id FROM incubators LIMIT 1")).fetchone()
            if row:
                db.add(SensorLog(
                    incubator_id=row[0],
                    temperature_c=reading["temperature_c"],
                    humidity_pct=reading["humidity_pct"],
                ))
                db.commit()
        except Exception as exc:
            logger.warning("Sensor poll error: %s", exc)
        finally:
            db.close()

    def stop(self) -> None:
        self._stop_flag.set()


_sensor_poller = _SensorPoller(settings.sensor_poll_interval_seconds)


# ------------------------------------------------------------------
# App lifecycle
# ------------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    # Ensure database directory exists
    Path("./database").mkdir(exist_ok=True)

    Base.metadata.create_all(bind=engine)

    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        if not config:
            device_id = f"PI-{uuid.uuid4().hex[:8].upper()}"
            claim_code = f"PAIR-{secrets.token_hex(3).upper()}"
            config = DeviceConfig(device_id=device_id, claimed=False, claim_code=claim_code)
            db.add(config)
            db.commit()
            logger.info("First boot — device_id=%s claim_code=%s", device_id, claim_code)

        device_id = config.device_id
        ensure_defaults(db)

        # Boot sequence — starts AP if device is unconfigured
        onboarding_service.boot(db, device_id)

        # Optional cloud registration
        cloud_result = cloud_service.register_device(device_id)
        if cloud_result.get("enabled"):
            logger.info("cloud_register: ok=%s", cloud_result.get("ok"))
    finally:
        db.close()

    gpio_service.setup()
    vision_service.setup()
    button_service.start()
    _sensor_poller.start()

    logger.info(
        "Incubator v3 started — version=%s gpio_mock=%s vision_backend=%s camera=%s",
        settings.app_version,
        settings.gpio_mock,
        settings.vision_backend,
        settings.camera_backend,
    )


@app.on_event("shutdown")
def shutdown() -> None:
    _sensor_poller.stop()
    button_service.stop()
    camera_service.cleanup()
    gpio_service.cleanup()


# ------------------------------------------------------------------
# Core API endpoints
# ------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "incubator-v3",
        "version": settings.app_version,
        "setup_mode": setup_mode_service.is_setup_mode(),
        "hotspot_active": onboarding_service.is_hotspot_active(),
        "gpio_mock": settings.gpio_mock,
    }


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
    existing = db.scalar(
        select(User).where((User.username == payload.username) | (User.email == payload.email))
    )
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
    db.add(ActionLog(
        action="setup_complete",
        payload=payload.model_dump_json(exclude={"password", "wifi_password"}),
    ))
    db.commit()
    return {"ok": True, "claimed": True, "device_name": config.device_name}


@app.get("/api/system/version")
def system_version() -> dict:
    """Current installed version and latest available commit on the configured branch."""
    version_file = Path("/opt/incubator/.version")
    local_sha = version_file.read_text().strip() if version_file.exists() else "unknown"

    repo_url = settings.update_repo_url
    branch = settings.update_branch

    remote_sha = "unknown"
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0 and result.stdout:
            remote_sha = result.stdout.split()[0][:8]
    except Exception:
        pass

    return {
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "branch": branch,
        "up_to_date": local_sha == remote_sha,
        "auto_update": settings.auto_update_enabled,
    }


@app.post("/api/system/update")
def trigger_update() -> dict:
    """Kick off an immediate update check in the background."""
    update_script = Path("/opt/incubator/scripts/auto_update.sh")
    if not update_script.exists():
        raise HTTPException(status_code=404, detail="Update script not found")
    try:
        subprocess.Popen(
            ["bash", str(update_script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"ok": True, "message": "Update started — service will restart if a new version is found"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/sensors/latest")
def sensors_latest() -> dict:
    """Live sensor reading from DHT22.  Used by dashboard for real-time display."""
    return gpio_service.read_temperature_humidity()


@app.post("/hardware/send")
def send_hardware_command(payload: HardwareCommand, db: Session = Depends(get_db)) -> dict:
    handler = {
        "open_lock": hardware_service.open_lock,
        "close_lock": hardware_service.close_lock,
        "open_door": hardware_service.open_door,
        "close_door": hardware_service.close_door,
        "heater_on": lambda: hardware_service.set_heater(True),
        "heater_off": lambda: hardware_service.set_heater(False),
        "fan_on": lambda: hardware_service.set_fan(True),
        "fan_off": lambda: hardware_service.set_fan(False),
        "move_motor": lambda: hardware_service.move_motor(payload.value if payload.value is not None else 200),
        "read_temp": hardware_service.read_temp,
        "read_humidity": hardware_service.read_humidity,
        "read_environment": hardware_service.read_environment,
        "set_candle": lambda: hardware_service.set_candle(str(payload.value).lower() in {"1", "true", "on"}),
        "capture_image": hardware_service.capture_image,
    }.get(payload.action)

    if not handler:
        raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")

    response = handler()
    db.add(ActionLog(action=payload.action, payload=str(payload.value)))
    db.commit()
    return response
