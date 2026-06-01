from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

MIN_HEARTBEAT_INTERVAL_SECONDS = 30
_REQUEST_TIMEOUT = 10.0  # seconds per outbound call


@dataclass(frozen=True)
class CloudServiceState:
    enabled: bool
    configured: bool
    api_base: str | None
    heartbeat_interval_seconds: int


class CloudService:
    """Optional cloud/domain hook service that never blocks local operation.

    All methods are synchronous and return a plain dict so callers need no
    special async handling.  On misconfiguration or network failure the method
    logs a warning and returns a safe error dict — it never raises.
    """

    def __init__(self) -> None:
        self._api_base = (settings.domain_api_base or "").rstrip("/") or None
        self._shared_secret = settings.device_shared_secret or None
        self._enabled = settings.enable_cloud_sync
        self._heartbeat_interval_seconds = max(
            MIN_HEARTBEAT_INTERVAL_SECONDS, settings.heartbeat_interval_seconds
        )
        self._device_id: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _device_id_value(self) -> str:
        if self._device_id:
            return self._device_id
        # Runtime: check env file written by firstboot script
        self._device_id = os.getenv("INCUBATOR_DEVICE_ID") or ""
        # Fallback: read from the file written by firstboot.sh
        if not self._device_id:
            try:
                with open("/etc/incubator-device-id") as fh:
                    self._device_id = fh.read().strip()
            except OSError:
                pass
        return self._device_id or "PI-UNKNOWN"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._shared_secret}",
            "X-Device-Id": self._device_id_value(),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._api_base}{path}"
        try:
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return {"ok": True, **resp.json()}
        except httpx.HTTPStatusError as exc:
            log.warning("Cloud GET %s → HTTP %s", path, exc.response.status_code)
            return {"ok": False, "error": f"HTTP {exc.response.status_code}", "path": path}
        except Exception as exc:  # noqa: BLE001
            log.warning("Cloud GET %s failed: %s", path, exc)
            return {"ok": False, "error": str(exc), "path": path}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._api_base}{path}"
        try:
            resp = httpx.post(url, headers=self._headers(), json=body, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return {"ok": True, **resp.json()}
        except httpx.HTTPStatusError as exc:
            log.warning("Cloud POST %s → HTTP %s", path, exc.response.status_code)
            return {"ok": False, "error": f"HTTP {exc.response.status_code}", "path": path}
        except Exception as exc:  # noqa: BLE001
            log.warning("Cloud POST %s failed: %s", path, exc)
            return {"ok": False, "error": str(exc), "path": path}

    def _guard(self, operation: str) -> dict[str, Any] | None:
        """Return an error dict if the service is not usable, else None."""
        state = self.state()
        if not state.enabled:
            return {
                "ok": False,
                "enabled": False,
                "operation": operation,
                "message": "Cloud sync disabled; local-first mode active.",
            }
        if not state.configured:
            return {
                "ok": False,
                "enabled": True,
                "configured": False,
                "operation": operation,
                "message": "Cloud sync enabled but domain settings are incomplete.",
            }
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def state(self) -> CloudServiceState:
        return CloudServiceState(
            enabled=self._enabled,
            configured=bool(self._api_base and self._shared_secret),
            api_base=self._api_base,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )

    def register_device(self, device_id: str | None = None) -> dict[str, Any]:
        """POST /api/v1/devices/register — claim a device slot on the server."""
        if (err := self._guard("register_device")) is not None:
            return err
        payload = {
            "device_id": device_id or self._device_id_value(),
            "app_version": settings.app_version,
            "registered_at": int(time.time()),
        }
        result = self._post("/api/v1/devices/register", payload)
        if result.get("ok"):
            log.info("Device registered: %s", payload["device_id"])
        return result

    def heartbeat(self, device_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST /api/v1/devices/{id}/heartbeat — send periodic status to server.

        Expected server response:
            {"ok": true, "commands": []}
        The optional ``commands`` list may contain strings like ``"restart"``
        or ``"update"`` that the caller can act on.
        """
        if (err := self._guard("heartbeat")) is not None:
            return err
        device_id = self._device_id_value()
        payload: dict[str, Any] = {
            "device_id": device_id,
            "app_version": settings.app_version,
            "timestamp": int(time.time()),
        }
        if device_state:
            payload.update(device_state)
        return self._post(f"/api/v1/devices/{device_id}/heartbeat", payload)

    def link_account(self, account_ref: str | None = None) -> dict[str, Any]:
        """POST /api/v1/devices/{id}/link — associate device with a user account."""
        if (err := self._guard("link_account")) is not None:
            return err
        device_id = self._device_id_value()
        return self._post(
            f"/api/v1/devices/{device_id}/link",
            {"device_id": device_id, "account_ref": account_ref or ""},
        )

    def fetch_remote_config(self) -> dict[str, Any]:
        """GET /api/v1/devices/{id}/config — pull remote overrides from server.

        Expected server response:
            {"ok": true, "config": {"SENSOR_POLL_INTERVAL": 60, ...}}
        """
        if (err := self._guard("fetch_remote_config")) is not None:
            return err
        device_id = self._device_id_value()
        return self._get(f"/api/v1/devices/{device_id}/config")

    def check_for_updates(self, current_version: str | None = None) -> dict[str, Any]:
        """GET /api/v1/ota/check — ask the server whether an update is available.

        Expected server response:
            {
              "ok": true,
              "update_available": false,
              "version": "1.31",
              "git_ref": null,
              "force_update": false
            }
        The OTA agent script (scripts/ota-agent.sh) uses the shell-level API
        directly for robustness.  This Python method is used by the dashboard
        and startup logic.
        """
        if (err := self._guard("check_for_updates")) is not None:
            return err
        version = current_version or settings.app_version
        device_id = self._device_id_value()
        return self._get(
            "/api/v1/ota/check",
            {"device_id": device_id, "version": version},
        )
