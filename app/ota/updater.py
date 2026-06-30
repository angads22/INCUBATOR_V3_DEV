"""OTA decision + apply/verify/rollback orchestration.

All side effects (querying GitHub, git checkout, pip, restarting the service,
health checks) are injected so the orchestration — including the
forced-failure rollback path — is unit-tested without touching the system.
``app/ota/__main__.py`` wires the real implementations.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

from ..version import parse_version

logger = logging.getLogger(__name__)

# Release tags look like "img-1.40-20260628" → capture the M.mm version.
_VERSION_RE = re.compile(r"(\d+\.\d{2})")


def extract_version(tag: str) -> str | None:
    if not tag:
        return None
    m = _VERSION_RE.search(tag)
    return m.group(1) if m else None


def is_newer(latest: str, current: str) -> bool:
    """True iff ``latest`` is a strictly newer M.mm version than ``current``."""
    try:
        return parse_version(latest) > parse_version(current)
    except (ValueError, AttributeError, TypeError):
        return False


class OtaUpdater:
    def __init__(
        self,
        *,
        current_version: str,
        get_latest: Callable[[], dict | None],
        current_ref: Callable[[], str],
        apply_ref: Callable[[str], None],
        verify: Callable[[], bool],
        restart_web: Callable[[], None],
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._current_version = current_version
        self._get_latest = get_latest
        self._current_ref = current_ref
        self._apply_ref = apply_ref
        self._verify = verify
        self._restart_web = restart_web
        self._log = log or logger.info

    def run(self) -> dict:
        latest = self._get_latest()
        if not latest or not latest.get("tag"):
            # Offline / no release / rate-limited — stay put, try again later.
            return {"checked": False, "reason": "no_release_or_offline"}

        tag = latest["tag"]
        version = extract_version(tag) or tag
        ref = latest.get("ref") or tag

        if not is_newer(version, self._current_version):
            return {"update_available": False, "current": self._current_version, "latest": version}

        prev = self._current_ref()
        self._log(f"OTA: applying {self._current_version} -> {version} ({ref})")
        self._apply_ref(ref)
        self._restart_web()

        if self._verify():
            self._log(f"OTA: update to {version} healthy.")
            return {"updated": True, "version": version, "ref": ref, "previous": prev}

        # Forced-failure path: the new version did not come up healthy. Restore
        # the previous code and restart — no manual intervention. The control
        # daemon was never restarted, so incubation kept running throughout.
        self._log(f"OTA: {version} failed health check — rolling back to {prev}")
        self._apply_ref(prev)
        self._restart_web()
        recovered = self._verify()
        self._log(f"OTA: rollback to {prev} {'recovered' if recovered else 'STILL UNHEALTHY'}")
        return {
            "updated": False,
            "rolled_back": True,
            "restored": prev,
            "recovered": recovered,
            "failed_version": version,
        }
