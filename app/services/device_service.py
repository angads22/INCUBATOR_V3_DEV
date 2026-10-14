"""Device-level operations: factory reset and the Settings → Device card.

``factory_reset`` is shared by the 15 s setup-button hold (button handler in
main.py) and ``POST /api/device/factory-reset``; ``device_info`` feeds
``GET /api/device/info`` and the Status page chips.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ActionLog, AppSetting, DeviceConfig, LinkedIdentity, Session as AuthSession, User
from ..settings_store import ensure_defaults
from .onboarding_service import lan_ips, short_hostname

logger = logging.getLogger(__name__)


def factory_reset(
    db: Session,
    *,
    wifi_service: Any,
    onboarding_service: Any,
    setup_mode_service: Any,
    device_id: str,
) -> dict:
    """Wipe everything the owner entered and restart in setup mode.

    Deleted: users, sessions, linked identities, all app settings (defaults are
    re-seeded, which also returns the setup AP to *open*), the device name /
    farm / saved Wi-Fi SSID, and every saved Wi-Fi profile in NetworkManager.
    Kept: device_id + claim_code (hardware identity), the action log (a
    ``system.factory_reset`` entry is written) and stored egg photos.
    Finally the setup AP is raised so the device is immediately reachable.
    """
    wiped: list[str] = []
    db.execute(delete(AuthSession))
    wiped.append("sessions")
    db.execute(delete(LinkedIdentity))
    wiped.append("linked_identities")
    db.execute(delete(User))
    wiped.append("users")
    config = db.scalar(select(DeviceConfig).limit(1))
    if config is not None:
        device_id = device_id or config.device_id
        config.claimed = False
        config.device_name = None
        config.farm_name = None
        config.wifi_ssid = None
        wiped.append("device_config")
    db.execute(delete(AppSetting))
    wiped.append("settings")
    db.commit()
    ensure_defaults(db)
    db.add(ActionLog(action="system.factory_reset", payload=f"device={device_id or '?'}"))
    db.commit()

    forgotten = 0
    forget = getattr(wifi_service, "forget_all_networks", None)
    if callable(forget):
        try:
            forgotten = int(forget() or 0)
        except Exception as exc:  # noqa: BLE001 — the reset must still complete
            logger.warning("factory reset: could not forget Wi-Fi networks: %s", exc)
    wiped.append("wifi_profiles")

    hotspot: dict | None = None
    if onboarding_service is not None:
        try:
            hotspot = onboarding_service.start_manual_hotspot(device_id or "PI-XXXX", source="factory_reset")
        except TypeError:   # an older/fake service without the ``source`` kwarg
            hotspot = onboarding_service.start_manual_hotspot(device_id or "PI-XXXX")
        except Exception as exc:  # noqa: BLE001
            logger.warning("factory reset: could not raise the setup AP: %s", exc)
    elif setup_mode_service is not None:
        try:
            setup_mode_service.enter_setup_mode("factory_reset")
        except Exception as exc:  # noqa: BLE001
            logger.debug("factory reset: setup mode not entered: %s", exc)

    logger.warning("Factory reset complete (device=%s): wiped %s; forgot %d Wi-Fi profile(s)", device_id, wiped, forgotten)
    return {
        "ok": True,
        "wiped": wiped,
        "device_id": device_id,
        "wifi_profiles_forgotten": forgotten,
        "hotspot": hotspot,
    }


def _attr(obj: Any, name: str) -> Any:
    """getattr that also survives a property raising before hardware setup."""
    if obj is None:
        return None
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001
        return None


def device_info(gpio_service: Any = None, camera_service: Any = None) -> dict:
    """What this unit is running on — board, drivers, network identity."""
    board: dict = {}
    try:
        from ..hal import detect_board

        board = detect_board().as_dict()
    except Exception as exc:  # noqa: BLE001 — a missing /proc entry must not break the card
        logger.debug("detect_board failed: %s", exc)

    gpio_backend = _attr(gpio_service, "backend_name") or ("mock" if settings.gpio_mock else settings.gpio_backend)
    sensor_backend = _attr(gpio_service, "sensor_name") or ("mock" if settings.gpio_mock else settings.sensor_backend)
    camera_backend = (
        _attr(camera_service, "backend_resolved")
        or _attr(camera_service, "backend")
        or settings.camera_backend
    )
    try:
        from ..hal.wifi import wifi_interface

        wifi_iface = settings.wifi_iface or wifi_interface()
    except Exception:  # noqa: BLE001
        wifi_iface = settings.wifi_iface or "wlan0"

    return {
        "ok": True,
        "board": board,
        "gpio_backend": str(gpio_backend),
        "sensor_backend": str(sensor_backend),
        "camera_backend": str(camera_backend),
        "wifi_iface": str(wifi_iface),
        "hostname": short_hostname(),
        "ips": lan_ips(),
        "version": settings.app_version,
        "control_daemon_enabled": bool(settings.control_daemon_enabled),
        "gpio_mock": bool(settings.gpio_mock),
    }
