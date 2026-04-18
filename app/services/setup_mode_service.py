from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class SetupModeService:
    """Tracks setup-mode state for local-first onboarding.

    State is persisted on disk so setup mode survives process restarts.
    """

    def __init__(self, state_file: str = "/tmp/incubator_setup_state.json") -> None:
        self._state_file = Path(state_file)
        self._lock = Lock()
        self._state = {"setup_mode": False, "reason": "normal"}
        self._load()

    def _load(self) -> None:
        if self._state_file.exists():
            try:
                self._state = json.loads(self._state_file.read_text())
            except json.JSONDecodeError as exc:
                logger.warning("Setup state file is corrupt, resetting: %s", exc)
                self._state = {"setup_mode": False, "reason": "normal"}
            except OSError as exc:
                logger.warning("Cannot read setup state file %s: %s", self._state_file, exc)
                self._state = {"setup_mode": False, "reason": "normal"}

    def _save(self) -> None:
        try:
            self._state_file.write_text(json.dumps(self._state))
        except OSError as exc:
            logger.error("Cannot persist setup state to %s: %s", self._state_file, exc)

    def enter_setup_mode(self, reason: str) -> None:
        with self._lock:
            self._state = {"setup_mode": True, "reason": reason}
            self._save()

    def exit_setup_mode(self) -> None:
        with self._lock:
            self._state = {"setup_mode": False, "reason": "normal"}
            self._save()

    def status(self) -> dict[str, str | bool]:
        with self._lock:
            return dict(self._state)

    def is_setup_mode(self) -> bool:
        return bool(self.status().get("setup_mode"))
