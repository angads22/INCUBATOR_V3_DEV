"""device_service: factory reset wipes the owner's data and raises the setup
AP; device_info describes the stack without assuming any hardware."""

from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.services.device_service import device_info, factory_reset


class FakeWiFi:
    def __init__(self):
        self.forget_calls = 0

    def forget_all_networks(self):
        self.forget_calls += 1
        return 3


class FakeOnboarding:
    def __init__(self):
        self.calls = []

    def start_manual_hotspot(self, device_id, source="button"):
        self.calls.append((device_id, source))
        return {"ok": True, "ssid": f"Incubator-{device_id[-4:]}"}


class LegacyOnboarding:
    """An older service without the ``source`` keyword."""

    def __init__(self):
        self.calls = []

    def start_manual_hotspot(self, device_id):
        self.calls.append(device_id)
        return {"ok": True}


class FakeSetup:
    def __init__(self):
        self.reasons = []

    def enter_setup_mode(self, reason):
        self.reasons.append(reason)


def _db():
    from app.database import get_db
    return next(get_db())


def _seed(db):
    from app.auth import create_session, hash_password
    from app.models import DeviceConfig, LinkedIdentity, User
    from app.settings_store import set_ap_password, update_settings

    user = User(username="farmer", email="farmer@example.com", password_hash=hash_password("hunter2pass"), role="owner")
    db.add(user)
    db.add(DeviceConfig(device_id="PI-ABCD1234", claimed=True, claim_code="PAIR-ABC123",
                        device_name="Coop 1", farm_name="Hill Farm", wifi_ssid="HomeNet"))
    db.commit()
    db.add(LinkedIdentity(user_id=user.id, provider="google", subject="sub-1", email="farmer@example.com"))
    db.commit()
    create_session(db, user.id, 3600)
    update_settings(db, {"target_temp_c": "39.0"})
    set_ap_password(db, "barnpass1")


def test_factory_reset_wipes_everything_and_raises_the_ap(client):
    from app.models import ActionLog, AppSetting, DeviceConfig, LinkedIdentity, Session as AuthSession, User
    from app.settings_store import effective_ap_password, get_settings

    db = _db()
    try:
        _seed(db)
        wifi, onb, setup = FakeWiFi(), FakeOnboarding(), FakeSetup()
        result = factory_reset(db, wifi_service=wifi, onboarding_service=onb,
                               setup_mode_service=setup, device_id="PI-ABCD1234")
        assert result["ok"] is True
        assert {"users", "sessions", "linked_identities", "settings", "device_config", "wifi_profiles"} <= set(result["wiped"])
        assert db.scalars(select(User)).all() == []
        assert db.scalars(select(AuthSession)).all() == []
        assert db.scalars(select(LinkedIdentity)).all() == []
        cfg = db.scalar(select(DeviceConfig).limit(1))
        assert cfg.device_id == "PI-ABCD1234" and cfg.claim_code == "PAIR-ABC123"   # identity kept
        assert cfg.claimed is False and cfg.device_name is None and cfg.farm_name is None and cfg.wifi_ssid is None
        assert get_settings(db)["target_temp_c"] == "37.5"                         # defaults re-seeded
        assert effective_ap_password(db) == ""                                      # setup AP open again
        assert db.scalar(select(AppSetting).where(AppSetting.key == "target_temp_c")) is not None
        assert db.scalar(select(ActionLog).where(ActionLog.action == "system.factory_reset")) is not None
        assert wifi.forget_calls == 1 and result["wifi_profiles_forgotten"] == 3
        assert onb.calls == [("PI-ABCD1234", "factory_reset")]
        assert setup.reasons == []            # the onboarding service owns setup mode
    finally:
        db.close()


def test_factory_reset_without_onboarding_service_enters_setup_mode(client):
    db = _db()
    try:
        _seed(db)
        setup = FakeSetup()
        result = factory_reset(db, wifi_service=None, onboarding_service=None, setup_mode_service=setup, device_id="PI-ABCD1234")
        assert result["ok"] and setup.reasons == ["factory_reset"] and result["wifi_profiles_forgotten"] == 0
    finally:
        db.close()


def test_factory_reset_tolerates_legacy_start_manual_hotspot(client):
    db = _db()
    try:
        _seed(db)
        onb = LegacyOnboarding()
        factory_reset(db, wifi_service=FakeWiFi(), onboarding_service=onb, setup_mode_service=FakeSetup(), device_id="")
        assert onb.calls == ["PI-ABCD1234"]   # device id recovered from the config row
    finally:
        db.close()


def test_device_info_reads_services_with_defaults():
    class Gpio:
        backend_name = "gpiod"
        sensor_name = "sht3x"

    class Camera:
        backend_resolved = "opencv"
        backend = "auto"

    info = device_info(gpio_service=Gpio(), camera_service=Camera())
    for key in ("board", "gpio_backend", "sensor_backend", "camera_backend", "wifi_iface", "hostname", "ips", "version", "control_daemon_enabled"):
        assert key in info
    assert info["ok"] is True
    assert info["gpio_backend"] == "gpiod" and info["sensor_backend"] == "sht3x" and info["camera_backend"] == "opencv"
    assert isinstance(info["board"], dict) and isinstance(info["ips"], list)
    assert info["version"] == settings.app_version and "." not in info["hostname"]

    bare = device_info()
    assert bare["gpio_backend"] == "mock" and bare["sensor_backend"] == "mock"   # GPIO_MOCK=true in tests
    assert bare["camera_backend"] == settings.camera_backend
    assert bare["wifi_iface"]

    class Broken:
        @property
        def backend_name(self):
            raise RuntimeError("not set up yet")

        sensor_name = None

    partial = device_info(gpio_service=Broken(), camera_service=None)
    assert partial["gpio_backend"] == "mock" and partial["sensor_backend"] == "mock"
