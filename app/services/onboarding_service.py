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
from typing import TYPE_CHECKING, Callable

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
        ap_password_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._wifi = wifi_service
        self._setup = setup_mode_service
        self._ap_ssid_prefix = ap_ssid_prefix
        self._ap_password = ap_password
        # When provided, this is the authoritative source of the setup-AP
        # password (read from the DB, default open). It overrides the static
        # ap_password so an OTA-updated unit never stays locked behind a stale
        # value baked into /etc/incubator.env.
        self._ap_password_resolver = ap_password_resolver
        self._ap_ip = ap_ip
        self._auto_hotspot = auto_hotspot
        self._hotspot_active = False
        self._captive = None  # CaptivePortalResponder, lazily created with the hotspot
        # Seconds to keep the AP up after "finish" so the wizard's auto-open (to
        # the hotspot IP) can load and be seen before the single radio switches
        # over to the home network.
        self._complete_grace_seconds = 12.0

    def _password(self) -> str:
        if self._ap_password_resolver is not None:
            try:
                return self._ap_password_resolver() or ""
            except Exception as exc:  # noqa: BLE001 — never block onboarding on this
                logger.warning("AP password resolver failed (%s) — using open network", exc)
                return ""
        return self._ap_password

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def boot(self, db: Session, device_id: str) -> None:
        """Called once at app startup.  Starts hotspot if device needs setup."""
        if not self._auto_hotspot:
            return
        config = db.scalar(select(DeviceConfig).limit(1))
        needs_setup = not config or not config.wifi_ssid
        if needs_setup:
            ssid = self._make_ssid(device_id)
            logger.info(
                "Device not configured — starting setup hotspot '%s' at %s",
                ssid,
                self._ap_ip,
            )
            self._setup.enter_setup_mode("auto_boot")
            ok = self._wifi.start_hotspot(ssid, self._password())
            self._hotspot_active = ok
            if ok:
                self._start_captive_responder()
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
        """Triggered by the setup button hold or /onboarding/start endpoint.

        Idempotent: if the setup AP is already broadcasting, we return its info
        WITHOUT restarting it. Restarting while a phone is connected drops the
        connection ("kicked out the moment I hit setup"), so a wizard/captive
        call to this must never tear the network down.
        """
        ssid = self._make_ssid(device_id)
        password = self._password()
        self._setup.enter_setup_mode("manual_trigger")
        already_up = self._hotspot_active or self._wifi.is_hotspot_up(ssid, bool(password))
        if already_up:
            self._hotspot_active = True
            self._start_captive_responder()
            return {
                "ok": True, "ssid": ssid, "password": password,
                "open": not bool(password), "already_active": True,
                "ap_url": f"http://{self._ap_ip}:8000",
            }
        ok = self._wifi.start_hotspot(ssid, password)
        self._hotspot_active = ok
        if ok:
            self._start_captive_responder()
        return {
            "ok": ok,
            "ssid": ssid,
            "password": password,
            "open": not bool(password),
            "already_active": False,
            "ap_url": f"http://{self._ap_ip}:8000",
        }

    def complete(self, ssid: str, password: str) -> bool:
        """Called after onboarding wizard finishes. Switches to client WiFi.

        The Pi Zero 2 W has a single radio, so the AP and the client connection
        cannot coexist on wlan0. Tear the hotspot (and captive responder) down
        FIRST, then join the client network — otherwise the connect attempt
        races the still-running AP and setup hangs. The HTTP handler schedules
        this as a background task so the response is already on the wire before
        the network flips out from under it.
        """
        self._setup.exit_setup_mode()
        self._stop_captive_responder()
        if self._hotspot_active:
            # Hold the AP up briefly so the wizard's auto-open can reach the
            # dashboard over the hotspot (<hostname>.local → 10.42.0.1) before we
            # switch the single radio over to the home network.
            import time

            time.sleep(self._complete_grace_seconds)
            self._wifi.stop_hotspot()
            self._hotspot_active = False
        connected = False
        if ssid:
            connected = self._wifi.connect_client(ssid, password)
        return connected

    # ------------------------------------------------------------------
    # Captive portal responder (best-effort; only while the hotspot is up)
    # ------------------------------------------------------------------

    def _start_captive_responder(self) -> None:
        try:
            from .captive_portal import CaptivePortalResponder

            if self._captive is None:
                self._captive = CaptivePortalResponder(f"http://{self._ap_ip}:8000/onboarding")
            self._captive.start()
        except Exception as exc:  # noqa: BLE001 — never let this break onboarding
            logger.warning("Could not start captive portal responder: %s", exc)

    def _stop_captive_responder(self) -> None:
        try:
            if self._captive is not None:
                self._captive.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("captive responder stop: %s", exc)

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
