from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import settings

MIN_HEARTBEAT_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class CloudServiceState:
    enabled: bool
    configured: bool
    api_base: str | None
    heartbeat_interval_seconds: int


class CloudService:
    """Optional cloud/domain hook service that never blocks local operation."""

    def __init__(self) -> None:
        self._api_base = settings.domain_api_base or None
        self._shared_secret = settings.device_shared_secret or None
        self._enabled = settings.enable_cloud_sync
        self._heartbeat_interval_seconds = max(MIN_HEARTBEAT_INTERVAL_SECONDS, settings.heartbeat_interval_seconds)

    def state(self) -> CloudServiceState:
        return CloudServiceState(
            enabled=self._enabled,
            configured=bool(self._api_base and self._shared_secret),
            api_base=self._api_base,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )

    def register_device(self, device_id: str | None = None) -> dict[str, Any]:
        return self._safe_placeholder("register_device", {"device_id": device_id})

    def heartbeat(self, device_state: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._safe_placeholder("heartbeat", {"device_state": device_state or {}})

    def link_account(self, account_ref: str | None = None) -> dict[str, Any]:
        return self._safe_placeholder("link_account", {"account_ref": account_ref})

    def fetch_remote_config(self) -> dict[str, Any]:
        return self._safe_placeholder("fetch_remote_config")

    def check_for_updates(self, current_version: str | None = None) -> dict[str, Any]:
        return self._safe_placeholder("check_for_updates", {"current_version": current_version})

    def _safe_placeholder(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self.state()
        if not state.enabled:
            return {
                "ok": False,
                "enabled": False,
                "configured": state.configured,
                "operation": operation,
                "message": "Cloud sync disabled; local-first mode active.",
                "payload": payload or {},
            }
        if not state.configured:
            return {
                "ok": False,
                "enabled": True,
                "configured": False,
                "operation": operation,
                "message": "Cloud sync enabled but domain settings are incomplete.",
                "payload": payload or {},
            }
        return {
            "ok": False,
            "enabled": True,
            "configured": True,
            "operation": operation,
            "message": "Cloud integration hook not configured yet.",
            "payload": payload or {},
        }
