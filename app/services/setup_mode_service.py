from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Iterable

logger = logging.getLogger(__name__)

# Reasons that prove somebody is physically at the device (or that the device
# itself raised the AP on a cold start). Setup mode entered from a browser
# ("manual_trigger") does not count.
PHYSICAL_REASONS = ("button_hold", "auto_boot", "factory_reset")

_NORMAL: dict = {"setup_mode": False, "reason": "normal", "entered_at": None}


class SetupModeService:
    """Tracks setup-mode state for local-first onboarding.

    State is persisted on disk so setup mode survives a process restart. The
    default location comes from ``settings.setup_state_path``: a tmpfs file
    (``/run/incubator/setup-state.json``) on an appliance — cleared on every
    boot, so a stale "setup mode on" can never outlive a power cycle — with a
    temp-dir fallback on a dev laptop.
    """

    def __init__(self, state_file: str | None = None) -> None:
        if state_file is None:
            from ..config import settings

            state_file = settings.setup_state_path
        self._state_file = Path(state_file)
        self._lock = Lock()
        self._state: dict = dict(_NORMAL)
        self._load()

    @property
    def state_file(self) -> Path:
        return self._state_file

    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
            if not isinstance(data, dict):
                raise ValueError("setup state is not an object")
            self._state = {**_NORMAL, **data}
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Setup state file is corrupt, resetting: %s", exc)
            self._state = dict(_NORMAL)
        except OSError as exc:
            logger.warning("Cannot read setup state file %s: %s", self._state_file, exc)
            self._state = dict(_NORMAL)

    def _save(self) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.debug("Cannot create %s: %s", self._state_file.parent, exc)
        try:
            self._state_file.write_text(json.dumps(self._state))
        except OSError as exc:
            logger.error("Cannot persist setup state to %s: %s", self._state_file, exc)

    def enter_setup_mode(self, reason: str) -> None:
        with self._lock:
            self._state = {
                "setup_mode": True,
                "reason": reason or "manual_trigger",
                "entered_at": time.time(),
            }
            self._save()

    def exit_setup_mode(self) -> None:
        with self._lock:
            self._state = dict(_NORMAL)
            self._save()

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def is_setup_mode(self) -> bool:
        return bool(self.status().get("setup_mode"))

    def reason(self) -> str:
        return str(self.status().get("reason") or "normal")

    def entered_at(self) -> float | None:
        ts = self.status().get("entered_at")
        return float(ts) if isinstance(ts, (int, float)) else None

    def entered_recently(
        self,
        within_seconds: float,
        reasons: Iterable[str] | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        """True if setup mode is on, was entered at most ``within_seconds`` ago
        and (when ``reasons`` is given) for one of those reasons.

        Used as a time-boxed proof of physical presence: a button hold opens
        the onboarding routes to the LAN for a few minutes, not forever.
        """
        state = self.status()
        if not state.get("setup_mode"):
            return False
        if reasons is not None and state.get("reason") not in set(reasons):
            return False
        ts = state.get("entered_at")
        if not isinstance(ts, (int, float)):
            return False
        now = time.time() if now is None else now
        return 0 <= now - float(ts) <= within_seconds
