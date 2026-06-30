"""Real OTA wiring: GitHub Releases → git checkout → restart web → verify.

Invoked by scripts/ota-agent.sh on the systemd timer. The control daemon
(incubator-control.service) is deliberately NEVER restarted here.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time

import httpx

from ..config import settings
from .updater import OtaUpdater

logger = logging.getLogger("incubator-ota")

INSTALL_DIR = os.getenv("INCUBATOR_INSTALL_DIR", "/opt/incubator")
GITHUB_LATEST = "https://api.github.com/repos/angads22/INCUBATOR_V3_DEV/releases/latest"
HEALTH_URL = "http://127.0.0.1:8000/health"
WEB_SERVICE = "incubator.service"  # NOT incubator-control.service


def _get_latest() -> dict | None:
    try:
        resp = httpx.get(
            GITHUB_LATEST,
            timeout=10.0,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "incubator-ota"},
        )
        if resp.status_code != 200:
            return None
        tag = (resp.json().get("tag_name") or "").strip()
        return {"tag": tag, "ref": tag} if tag else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("OTA: GitHub query failed (offline?): %s", exc)
        return None


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", INSTALL_DIR, *args], text=True).strip()


def _current_ref() -> str:
    return _git("rev-parse", "HEAD")


def _apply_ref(ref: str) -> None:
    # Fetch the tag/sha then check it out, and reinstall deps.
    subprocess.run(["git", "-C", INSTALL_DIR, "fetch", "--depth", "1", "origin", ref], check=False)
    subprocess.run(["git", "-C", INSTALL_DIR, "checkout", "--force", ref], check=False) or \
        subprocess.run(["git", "-C", INSTALL_DIR, "checkout", "--force", "FETCH_HEAD"], check=False)
    subprocess.run(
        [f"{INSTALL_DIR}/.venv/bin/pip", "install", "--extra-index-url",
         "https://www.piwheels.org/simple", "-e", f"{INSTALL_DIR}[pi]", "--quiet"],
        check=False,
    )


def _restart_web() -> None:
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "restart", WEB_SERVICE], check=False)


def _verify() -> bool:
    """Healthy = web /health responds AND (if enabled) the control daemon's
    state file is fresh — i.e. the control loop is actually running."""
    ok_web = False
    for _ in range(10):
        try:
            if httpx.get(HEALTH_URL, timeout=5.0).status_code == 200:
                ok_web = True
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    if not ok_web:
        return False
    if settings.control_daemon_enabled:
        try:
            state = json.loads(open(settings.control_state_path).read())
            # Stale state file → control loop is not running.
            if time.time() - float(state.get("ts", 0)) > 120:
                return False
        except Exception:  # noqa: BLE001
            return False
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    current = "unknown"
    try:
        current = open(f"{INSTALL_DIR}/.git-ref").read().strip()
    except OSError:
        current = settings.app_version

    updater = OtaUpdater(
        current_version=current,
        get_latest=_get_latest,
        current_ref=_current_ref,
        apply_ref=_apply_ref,
        verify=_verify,
        restart_web=_restart_web,
        log=logger.info,
    )
    result = updater.run()
    logger.info("OTA result: %s", result)
    # Exit non-zero only if we ended up unhealthy after a rollback.
    return 1 if result.get("rolled_back") and not result.get("recovered") else 0


if __name__ == "__main__":
    sys.exit(main())
