"""
Boot-time onboarding orchestrator for Pi Zero 2W.

On every startup this service checks whether the device is already configured.
If it is NOT claimed / has no WiFi configured, it automatically brings up a
WiFi Access Point so the user can connect and complete setup via the browser.

Flow:
  1. App starts → OnboardingService.boot(db) is called
  2. Checks DeviceConfig: if unclaimed or missing wifi_ssid → start_hotspot()
  3. Hotspot SSID: "{ap_ssid_prefix}-{last4_of_device_id}"  e.g. "Incubator-A3F2"
  4. User connects phone/laptop to that AP
  5. Browser navigates to http://{ap_ip}:8000  (displayed in README & serial log)
  6. Onboarding wizard runs: device name → WiFi network → optional account
  7. POST /onboarding/complete → OnboardingService.complete(ssid, password)
  8. Hotspot torn down → Pi joins home network → normal operation
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DeviceConfig

if TYPE_CHECKING:
    from .wifi_service import WiFiService
    from .setup_mode_service import SetupModeService

logger = logging.getLogger(__name__)


class OnboardingService:
    def __init__(
        self,
        wifi_service: "WiFiService",
        setup_mode_service: "SetupModeService",
        ap_ssid_prefix: str,
        ap_password: str,
        ap_ip: str,
        auto_hotspot: bool = True,
    ) -> None:
        self._wifi = wifi_service
        self._setup = setup_mode_service
        self._ap_ssid_prefix = ap_ssid_prefix
        self._ap_password = ap_password
        self._ap_ip = ap_ip
        self._auto_hotspot = auto_hotspot
        self._hotspot_active = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def boot(self, db: Session, device_id: str) -> None:
        """Called once at app startup.  Starts hotspot if device needs setup."""
        if not self._auto_hotspot:
            return
        config = db.scalar(select(DeviceConfig).limit(1))
        needs_setup = not config or not config.wifi_ssid or not config.claimed
        if needs_setup:
            ssid = self._make_ssid(device_id)
            logger.info(
                "Device not configured — starting setup hotspot '%s' at %s",
                ssid,
                self._ap_ip,
            )
            self._setup.enter_setup_mode("auto_boot")
            ok = self._wifi.start_hotspot(ssid, self._ap_password)
            self._hotspot_active = ok
            if ok:
                logger.info(
                    "Hotspot active. Connect to '%s' and open http://%s:8000",
                    ssid,
                    self._ap_ip,
                )
            else:
                logger.warning("Hotspot failed to start — onboarding only via existing network")
        else:
            logger.info("Device already configured (ssid=%s) — skipping hotspot", config.wifi_ssid)

    def start_manual_hotspot(self, device_id: str) -> dict:
        """Triggered by the setup button hold or /onboarding/start endpoint."""
        ssid = self._make_ssid(device_id)
        self._setup.enter_setup_mode("manual_trigger")
        ok = self._wifi.start_hotspot(ssid, self._ap_password)
        self._hotspot_active = ok
        return {
            "ok": ok,
            "ssid": ssid,
            "password": self._ap_password,
            "ap_url": f"http://{self._ap_ip}:8000",
        }

    def complete(self, ssid: str, password: str) -> bool:
        """Called after onboarding wizard finishes.  Switches to client WiFi."""
        self._setup.exit_setup_mode()
        connected = False
        if ssid:
            connected = self._wifi.connect_client(ssid, password)
        if self._hotspot_active:
            self._wifi.stop_hotspot()
            self._hotspot_active = False
        return connected

    def is_hotspot_active(self) -> bool:
        return self._hotspot_active

    def ap_ssid(self, device_id: str) -> str:
        return self._make_ssid(device_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_ssid(self, device_id: str) -> str:
        suffix = device_id[-4:].upper() if device_id else "XXXX"
        return f"{self._ap_ssid_prefix}-{suffix}"
