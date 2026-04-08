from __future__ import annotations

import json
from pathlib import Path
from threading import Lock


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
            except Exception:
                self._state = {"setup_mode": False, "reason": "normal"}

    def _save(self) -> None:
        self._state_file.write_text(json.dumps(self._state))

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
