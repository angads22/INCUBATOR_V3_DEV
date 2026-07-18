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
import threading
import time
import uuid
from typing import Any
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import create_session, get_user_id_from_session, has_any_user, hash_password
from .config import settings
from .database import Base, engine, ensure_sqlite_healthy, get_db
from .models import ActionLog, DeviceConfig, SensorLog, User
from .routes.ai import (
    router as ai_router,
    set_vision_hardware,
    set_storage_service as set_ai_storage,
    set_growth_service as set_ai_growth,
)
from .routes.camera import router as camera_router, set_camera_service
from .routes.captures import router as captures_router, set_capture_services
from .routes.growth import router as growth_router, set_growth_services
from .routes.testing import router as testing_router, set_testing_services
from .routes.web import router as web_router, set_runtime_services
from .schemas import HardwareCommand, OnboardingPayload, SetupStatus
from .services.alert_service import AlertService
from .services.button_service import SetupButtonService
from .services.camera_service import CameraService
from .services.cloud_service import CloudService
from .services.gpio_service import GPIOService
from .services.hardware_service import HardwareService
from .services.onboarding_service import OnboardingService
from .services.growth_service import GrowthService
from .services.setup_mode_service import SetupModeService
from .services.storage_service import StorageService
from .services.vision_service import VisionService
from .services.wifi_service import WiFiService
from .settings_store import ensure_defaults, get_settings

app = FastAPI(title="Incubator v3 — Pi Zero 2W")
logger = logging.getLogger(__name__)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline browser hardening + cross-site write protection.

    Headers: stop MIME sniffing, forbid framing (clickjacking), and keep
    referrers on-device. Origin check: browsers attach an Origin header to
    cross-site POST/PUT/PATCH/DELETE; if one is present and its host matches
    none of the hosts we're legitimately served as, the request came from
    another site's page and is refused — token-free CSRF protection that can't
    break same-origin fetches or non-browser clients (which send no Origin).

    Reverse-proxy aware: remote access via a proxy (a documented setup) sends
    the browser's Origin as the external domain while ``Host`` may be rewritten
    to the backend, so ``X-Forwarded-Host`` is accepted as an equally valid
    served-as host. Both are set by the trusted proxy, not the attacker's page.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin", "")
        if origin and origin != "null":
            from urllib.parse import urlsplit

            origin_host = urlsplit(origin).netloc.lower()
            allowed = {request.headers.get("host", "").lower()}
            # X-Forwarded-Host may be a comma-separated chain (proxy → proxy).
            for xfh in request.headers.get("x-forwarded-host", "").split(","):
                if xfh.strip():
                    allowed.add(xfh.strip().lower())
            allowed.discard("")
            if origin_host and allowed and origin_host not in allowed:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={"ok": False, "detail": "Cross-origin write refused"},
                )
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(web_router)
app.include_router(ai_router)
app.include_router(camera_router)
app.include_router(captures_router)
app.include_router(growth_router)
app.include_router(testing_router)

# Sign in with Google / Apple + linked accounts (buttons appear only when the
# fleet env configures a provider — see config.py).
from .routes.identity import router as identity_router  # noqa: E402

app.include_router(identity_router)

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
    heater_current_enabled=settings.heater_current_sense_enabled,
    heater_current_bus=settings.heater_current_i2c_bus,
    heater_current_addr=settings.heater_current_i2c_addr,
    heater_current_shunt_ohms=settings.heater_current_shunt_ohms,
)

camera_service = CameraService(
    backend=settings.camera_backend,
    image_dir=settings.camera_image_dir,
    resolution=(settings.camera_resolution_w, settings.camera_resolution_h),
    preview_resolution=(settings.camera_preview_w, settings.camera_preview_h),
    preview_fps=settings.camera_preview_fps,
    frame_dir=settings.camera_frame_dir,
)

hardware_service = HardwareService(gpio=gpio_service, camera=camera_service)

storage_service = StorageService(
    captures_dir=settings.captures_dir,
    enabled=settings.capture_storage_enabled,
    min_free_mb=settings.capture_min_free_mb,
    target_free_mb=settings.capture_target_free_mb,
    max_dir_mb=settings.capture_max_dir_mb,
    keep_min=settings.capture_keep_min,
    retention_days=settings.capture_retention_days,
)

alert_service = AlertService(gpio=gpio_service)

growth_service = GrowthService(
    incubation_days=settings.incubation_days,
    auto_actions=settings.vision_auto_actions,
    lockdown_humidity_pct=settings.vision_lockdown_humidity_pct,
    lockdown_days_before_hatch=settings.vision_lockdown_days_before_hatch,
)

vision_service = VisionService(
    backend=settings.vision_backend,
    tflite_model_path=settings.vision_tflite_model_path,
    api_url=settings.vision_api_url,
    api_key=settings.vision_api_key,
    confidence_threshold=settings.vision_confidence_threshold,
    stage_backend=settings.vision_stage_backend,
    stage_model_path=settings.vision_stage_model_path,
    incubation_days=settings.incubation_days,
)

setup_mode_service = SetupModeService()
wifi_service = WiFiService()


def _resolve_ap_password() -> str:
    """Authoritative setup-AP password: the DB value (default open), never the
    stale INCUBATOR_AP_PASSWORD baked into /etc/incubator.env at first boot."""
    from .settings_store import effective_ap_password

    db = next(get_db())
    try:
        return effective_ap_password(db)
    except Exception:  # noqa: BLE001 — open network is the safe fallback
        return ""
    finally:
        db.close()


onboarding_service = OnboardingService(
    wifi_service=wifi_service,
    setup_mode_service=setup_mode_service,
    ap_ssid_prefix=settings.ap_ssid_prefix,
    ap_password=settings.ap_password,
    ap_ip=settings.ap_ip,
    auto_hotspot=settings.auto_hotspot_on_unclaimed,
    ap_password_resolver=_resolve_ap_password,
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
    alert_service=alert_service,
)
set_vision_hardware(vision=vision_service, hardware=hardware_service)
set_ai_storage(storage_service)
set_ai_growth(growth_service)
set_camera_service(camera_service)
set_capture_services(storage_service, camera_service)
set_growth_services(growth_service, hardware_service)
set_testing_services(vision_service)


def _on_button_held(reason: str) -> None:
    """Callback from button_service — restart the AP for re-onboarding."""
    logger.info("Setup button held (%s) — starting manual hotspot", reason)
    # Short chirp so the operator knows the press registered.
    gpio_service.pulse_alarm(0.15)
    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        device_id = config.device_id if config else "PI-XXXX"
    finally:
        db.close()
    result = onboarding_service.start_manual_hotspot(device_id)
    if result.get("ok"):
        # Longer chirp: the hotspot is up and ready to join.
        gpio_service.pulse_alarm(0.5)


# Shared hardware command dispatch, used by both the HTTP endpoint and the
# fleet MQTT bus so a unit responds identically to a local action and a
# remote-over-bus command. Returns {"ok": False, "error": "unknown_action"}
# for an unrecognised action rather than raising.
def execute_hardware_action(action: str, value: Any = None) -> dict:
    # When the control daemon owns heater/fan/turner, route those commands to it
    # rather than driving the pins here — the web app must not fight the daemon.
    if settings.control_daemon_enabled:
        from .control.bridge import control_command_for, enqueue_command

        mapped = control_command_for(action, value)
        if mapped is not None:
            enqueue_command(settings.control_command_path, mapped[0], mapped[1])
            return {"ok": True, "queued": action, "via": "control-daemon"}

    def _alarm_off() -> dict:
        # A manual off also silences the alert engine so it does not
        # immediately re-assert the buzzer on the next poll.
        alert_service.silence()
        return hardware_service.set_alarm(False)

    handler = {
        "open_lock": hardware_service.open_lock,
        "close_lock": hardware_service.close_lock,
        "open_door": hardware_service.open_door,
        "close_door": hardware_service.close_door,
        "heater_on": lambda: hardware_service.set_heater(True),
        "heater_off": lambda: hardware_service.set_heater(False),
        "fan_on": lambda: hardware_service.set_fan(True),
        "fan_off": lambda: hardware_service.set_fan(False),
        "move_motor": lambda: hardware_service.move_motor(value if value is not None else 200),
        "read_temp": hardware_service.read_temp,
        "read_humidity": hardware_service.read_humidity,
        "read_environment": hardware_service.read_environment,
        "set_candle": lambda: hardware_service.set_candle(str(value).lower() in {"1", "true", "on"}),
        "capture_image": hardware_service.capture_image,
        "alarm_on": lambda: hardware_service.set_alarm(True),
        "alarm_off": _alarm_off,
        "alarm_test": alert_service.test_alarm,
    }.get(action)
    if not handler:
        return {"ok": False, "error": "unknown_action", "action": action}
    return handler()


def _fleet_command_dispatch(action: str, value: Any = None) -> dict:
    """Run a bus command and log it; used as the FleetMqttService dispatcher."""
    result = execute_hardware_action(action, value)
    try:
        db = next(get_db())
        try:
            db.add(ActionLog(action=f"mqtt.{action}", payload=str(value)))
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Fleet command logging failed: %s", exc)
    return result


# Constructed in startup() once the device_id is known.
fleet_service = None


# ------------------------------------------------------------------
# Sensor poller background thread
# ------------------------------------------------------------------

class _SensorPoller(threading.Thread):
    """Reads DHT22 every N seconds and persists to sensor_logs."""

    def __init__(self, interval: int) -> None:
        super().__init__(daemon=True, name="sensor-poller")
        self.interval = interval
        self._stop_flag = threading.Event()
        self._last_retention_sweep = 0.0

    def run(self) -> None:
        # Wait for a settled reading before the first log
        time.sleep(5)
        while not self._stop_flag.wait(self.interval):
            self._poll()
            # Heartbeat AFTER a completed poll: if this thread dies or the
            # process wedges, pings stop and systemd's WatchdogSec restarts
            # the app — the hang-coverage Restart=always cannot provide.
            from .sd_notify import sd_watchdog_ping

            sd_watchdog_ping()

    def _retention_sweep(self, db) -> None:
        """Once a day, prune old sensor/action log rows (SD-card longevity)."""
        if time.time() - self._last_retention_sweep < 24 * 3600:
            return
        self._last_retention_sweep = time.time()
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import delete

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            if settings.sensor_log_retention_days > 0:
                cutoff = now - timedelta(days=settings.sensor_log_retention_days)
                db.execute(delete(SensorLog).where(SensorLog.captured_at < cutoff))
            if settings.action_log_retention_days > 0:
                cutoff = now - timedelta(days=settings.action_log_retention_days)
                db.execute(delete(ActionLog).where(ActionLog.created_at < cutoff))
            db.commit()
        except Exception as exc:  # noqa: BLE001 — retention must never break polling
            logger.debug("retention sweep skipped: %s", exc)

    def _poll(self) -> None:
        db = next(get_db())
        try:
            # Keep the SD card from filling up: prune oldest egg photos whenever
            # free space drops below the configured headroom. Cheap (a disk stat
            # + a query) and a no-op unless under pressure.
            try:
                storage_service.enforce(db)
            except Exception as exc:  # noqa: BLE001 — never let the janitor break polling
                logger.debug("storage enforce skipped: %s", exc)
            # Daily DB retention sweep (sensor/action logs) — SD-card longevity.
            self._retention_sweep(db)
            # Never-unreachable invariant: if the device has no Wi-Fi AND no
            # hotspot, raise the fallback setup AP (and retry the saved Wi-Fi
            # periodically while in fallback). See OnboardingService.ensure_reachable.
            try:
                onboarding_service.ensure_reachable(db)
            except Exception as exc:  # noqa: BLE001 — watchdog must never break polling
                logger.debug("reachability watchdog skipped: %s", exc)
            if settings.control_daemon_enabled:
                # The control daemon owns the DHT — read its published state
                # instead of touching the sensor (avoids single-radio/pin races).
                from .control.bridge import daemon_reading
                reading = daemon_reading(settings.control_state_path)
            else:
                reading = gpio_service.read_temperature_humidity()
            # Failed reads must reach the alert engine too — it tracks the
            # offline state, caches the last good values, and drives the buzzer.
            events = alert_service.record_reading(reading, get_settings(db))
            for event in events:
                logger.info("Alert event: %s", event["type"])
                db.add(ActionLog(action=f"system.{event['type']}", payload=event["message"]))
            if events:
                db.commit()
            # Heater-circuit integrity (optional INA219 current sense): a tripped
            # thermal cutoff (commanded ON, ~0 A) or a welded relay (current while
            # OFF) latches an alarm. Runs independently of the DHT read so it still
            # fires when the temp sensor is offline. No-op unless a sensor is fitted.
            if settings.heater_current_sense_enabled:
                try:
                    commanded_heat = (
                        bool(reading.get("heater_on"))
                        if settings.control_daemon_enabled
                        else bool(gpio_service.get_state().get("heater"))
                    )
                    current = gpio_service.read_heater_current()
                    hevents = alert_service.evaluate_heater(
                        commanded_heat, current.get("present", False),
                        current.get("amps"), get_settings(db),
                    )
                    for event in hevents:
                        logger.warning("Heater alert: %s", event["type"])
                        db.add(ActionLog(action=f"hardware.{event['type']}", payload=event["message"]))
                    if hevents:
                        db.commit()
                except Exception as exc:  # noqa: BLE001 — never let it break polling
                    logger.debug("heater current check skipped: %s", exc)
            # Push the freshest snapshot (online or offline) onto the fleet bus.
            if fleet_service is not None:
                fleet_service.publish_telemetry()
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

def _ensure_egg_roi_columns() -> None:
    """Add eggs.roi_* columns to a pre-existing SQLite DB if missing.

    ``Base.metadata.create_all`` creates new tables (e.g. stage_tests) but never
    alters existing ones, so a DB created before the ROI columns existed needs a
    tiny add-column-if-missing pass. No-op when the columns already exist.
    """
    from sqlalchemy import inspect, text

    try:
        inspector = inspect(engine)
        if "eggs" not in inspector.get_table_names():
            return  # create_all will build it with the columns already present
        existing = {col["name"] for col in inspector.get_columns("eggs")}
        with engine.begin() as conn:
            for col in ("roi_x", "roi_y", "roi_w", "roi_h"):
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE eggs ADD COLUMN {col} INTEGER"))
    except Exception as exc:  # noqa: BLE001 — never block startup on a schema patch
        logger.warning("ROI column check skipped: %s", exc)


def _detect_clock_rollback(db: Session) -> None:
    """Record if the system clock booted earlier than our newest log entry.

    An RTC-less Pi Zero freezes its clock across a power cut, so on boot it can
    read *earlier* than events we already persisted. This does not fix the clock
    (the optional DS3231 RTC / NTP does that) — it just leaves a breadcrumb so a
    backwards jump in the timeline is explainable and an operator knows an RTC is
    worth fitting. Reuses existing log timestamps: one cheap query, no new writes
    on the SD card unless a rollback is actually detected.
    """
    from datetime import datetime as _dt

    from sqlalchemy import text

    try:
        row = db.execute(
            text(
                "SELECT MAX(ts) FROM ("
                "  SELECT MAX(created_at) AS ts FROM action_logs"
                "  UNION ALL SELECT MAX(captured_at) AS ts FROM sensor_logs"
                ")"
            )
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 — never block startup on this check
        logger.debug("clock-rollback check skipped: %s", exc)
        return

    newest_raw = row[0] if row else None
    if not newest_raw:
        return
    try:
        newest = _dt.fromisoformat(str(newest_raw))
    except ValueError:
        return

    now = _utcnow_naive()
    # Only flag a meaningful rollback (>60s) so ordinary sub-second write/read
    # skew never trips it.
    drift = (newest - now).total_seconds()
    if drift > 60:
        mins = round(drift / 60.0, 1)
        logger.warning(
            "System clock is %.1f min behind the newest log entry — likely a "
            "power-cut clock reset on a unit without an RTC.", mins,
        )
        db.add(ActionLog(
            action="system.clock_rolled_back",
            payload=f"system clock ~{mins} min behind last record; RTC/NTP will correct it",
        ))
        db.commit()


def _utcnow_naive() -> "Any":
    from datetime import datetime as _dt, timezone as _tz

    return _dt.now(_tz.utc).replace(tzinfo=None)


@app.on_event("startup")
def startup() -> None:
    # Under Type=notify systemd waits for READY — send it first so a slow
    # first boot (fs expand, big DB) can never be killed as a start timeout.
    from .sd_notify import sd_ready

    sd_ready()

    # Ensure database directory exists
    Path("./database").mkdir(exist_ok=True)

    # A power cut mid-write can corrupt the SQLite file; quarantine it (and
    # restore the newest snapshot) instead of crash-looping forever.
    ensure_sqlite_healthy()

    Base.metadata.create_all(bind=engine)
    _ensure_egg_roi_columns()

    # Daily snapshot AFTER the health check passed: corruption later restores
    # from this instead of factory-resetting the farmer's device.
    from .database import snapshot_sqlite

    snapshot_sqlite()

    db = next(get_db())
    try:
        config = db.scalar(select(DeviceConfig).limit(1))
        # Prefer the identity provisioned by firstboot (INCUBATOR_DEVICE_ID,
        # derived from the Pi serial). Falling back to a random UUID here is what
        # made a single unit look like "two incubators": firstboot named it
        # PI-<serial> in the env / hostname / cloud while the app minted an
        # unrelated UUID for the setup AP. Only generate a UUID when no provisioned
        # id exists (e.g. a dev laptop with no env file).
        provisioned_id = settings.device_id or f"PI-{uuid.uuid4().hex[:8].upper()}"
        if not config:
            claim_code = f"PAIR-{secrets.token_hex(3).upper()}"
            config = DeviceConfig(device_id=provisioned_id, claimed=False, claim_code=claim_code)
            db.add(config)
            db.commit()
            logger.info("First boot — device_id=%s claim_code=%s", provisioned_id, claim_code)
        elif settings.device_id and not config.claimed and config.device_id != settings.device_id:
            # An already-flashed unit that diverged before this fix: realign the
            # DB to the provisioned id while it is still unclaimed so the AP,
            # hostname, and cloud all agree on one identity.
            logger.info("Reconciling device_id %s -> %s", config.device_id, settings.device_id)
            config.device_id = settings.device_id
            db.commit()

        device_id = config.device_id
        device_name = config.device_name or ""
        ensure_defaults(db)

        # Note (don't fix) a power-cut clock reset on an RTC-less unit.
        _detect_clock_rollback(db)

        # Boot sequence — starts AP if device is unconfigured
        onboarding_service.boot(db, device_id)

        # Optional cloud registration
        cloud_result = cloud_service.register_device(device_id)
        if cloud_result.get("enabled"):
            logger.info("cloud_register: ok=%s", cloud_result.get("ok"))

        # Clear any photo backlog at boot (e.g. card filled while powered off).
        try:
            storage_service.enforce(db)
        except Exception as exc:  # noqa: BLE001
            logger.debug("startup storage enforce skipped: %s", exc)
    finally:
        db.close()

    gpio_service.setup()
    vision_service.setup()
    button_service.start()
    # Prime the sensor cache once synchronously so the dashboard has data
    # before the poller's first scheduled run.
    _sensor_poller._poll()
    _sensor_poller.start()

    # Fleet MQTT bus — each unit publishes telemetry + accepts commands keyed on
    # its device_id. Disabled unless MQTT_ENABLED; never blocks local operation.
    global fleet_service
    if settings.mqtt_enabled:
        from .services.fleet_service import FleetMqttService

        fleet_service = FleetMqttService(
            device_id,
            base_topic=settings.mqtt_base_topic,
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            username=settings.mqtt_username,
            password=settings.mqtt_password,
            command_dispatch=_fleet_command_dispatch,
            telemetry_source=lambda: alert_service.sensor_snapshot(),
            device_name=device_name,
        )
        fleet_service.start()

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
    if fleet_service is not None:
        fleet_service.stop()
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
def complete_setup(payload: OnboardingPayload, response: Response, db: Session = Depends(get_db)) -> dict:
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

    user = User(
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="owner",
    )
    db.add(user)
    config.claimed = True
    config.device_name = payload.device_name
    config.farm_name = payload.farm_name
    config.wifi_ssid = payload.wifi_ssid
    db.add(ActionLog(
        action="setup_complete",
        payload=payload.model_dump_json(exclude={"password", "wifi_password"}),
    ))
    db.commit()

    # Auto-login the new owner so the claim flow lands authenticated.
    response.set_cookie(
        key=settings.session_cookie_name,
        value=create_session(db, user.id, settings.session_ttl_seconds),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.session_secure,
        path="/",
    )
    return {"ok": True, "claimed": True, "device_name": config.device_name}


@app.get("/api/sensors/latest")
def sensors_latest() -> dict:
    """Cached sensor state from the poller thread, plus active alerts.

    The DHT22 is never read on the request path — a failed read blocks ~3s
    holding the GPIO lock.  ``online`` reflects the most recent poll; the
    cached values are the last good reading.
    """
    snapshot = alert_service.sensor_snapshot()
    return {"ok": True, **snapshot, "alerts": alert_service.alert_state()}


@app.post("/hardware/send")
def send_hardware_command(
    payload: HardwareCommand,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    # Physical actuator control requires a session once an account exists.
    if (settings.require_login or has_any_user(db)) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    response = execute_hardware_action(payload.action, payload.value)
    if response.get("error") == "unknown_action":
        raise HTTPException(status_code=400, detail=f"Unknown action: {payload.action}")

    db.add(ActionLog(action=payload.action, payload=str(payload.value)))
    db.commit()
    return response
