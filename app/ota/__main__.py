"""Real OTA wiring: GitHub Releases → git checkout → restart web → verify.

Invoked by scripts/ota-agent.sh on the systemd timer. The control daemon
(incubator-control.service) is deliberately NEVER restarted here.
"""

from __future__ import annotations

import json
import logging
import os
import platform
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
PIWHEELS_INDEX = "https://www.piwheels.org/simple"


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().replace(b"\x00", b"").decode("utf-8", "replace")
    except OSError:
        return ""


def _is_raspberry_pi(model_path: str = "/proc/device-tree/model") -> bool:
    return "Raspberry Pi" in _read_text(model_path)


def _is_debian_family(os_release_path: str = "/etc/os-release") -> bool:
    text = _read_text(os_release_path).lower()
    for line in text.splitlines():
        if line.startswith(("id=", "id_like=")):
            value = line.split("=", 1)[1].strip().strip('"')
            if any(tok in value.split() for tok in ("debian", "raspbian", "ubuntu")):
                return True
    return False


def _use_piwheels(machine: str | None = None, os_release_path: str = "/etc/os-release") -> bool:
    """piwheels only serves ARM wheels built against Raspberry Pi OS / Debian —
    the same rule install.sh applies."""
    arch = machine or platform.machine()
    return arch in {"armv6l", "armv7l", "aarch64"} and _is_debian_family(os_release_path)


def _pip_extras(model_path: str = "/proc/device-tree/model") -> str:
    """`[hardware]` (libgpiod + smbus2) everywhere; the legacy `[pi]` extras
    (RPi.GPIO, adafruit-dht) only on a Raspberry Pi."""
    extras = ["hardware"]
    if _is_raspberry_pi(model_path):
        extras.append("pi")
    return ",".join(extras)


def _pip_install_cmd(install_dir: str = INSTALL_DIR) -> list[str]:
    cmd = [f"{install_dir}/.venv/bin/pip", "install"]
    if _use_piwheels():
        cmd += ["--extra-index-url", PIWHEELS_INDEX]
    cmd += ["-e", f"{install_dir}[{_pip_extras()}]", "--quiet"]
    return cmd


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
    # Forward updates: fetch the release tag from GitHub and check it out via
    # FETCH_HEAD (a bare `git fetch origin <tag>` doesn't create a local ref).
    # Rollback: ref is a local sha already in the repo, so `git checkout <sha>`
    # succeeds directly and the fetch is a harmless no-op.
    subprocess.run(["git", "-C", INSTALL_DIR, "fetch", "--depth", "1", "origin", ref], check=False)
    checked_out = subprocess.run(
        ["git", "-C", INSTALL_DIR, "checkout", "--force", ref], check=False
    ).returncode == 0
    if not checked_out:
        subprocess.run(["git", "-C", INSTALL_DIR, "checkout", "--force", "FETCH_HEAD"], check=False)
    # Dependencies: core app + hardware extras (+ Pi legacy extras on a Pi). If
    # an optional extra fails to build on this board, fall back to the core
    # install so the update is never left half-applied.
    if subprocess.run(_pip_install_cmd(), check=False).returncode != 0:
        logger.warning("OTA: pip install with extras failed — retrying core install only")
        cmd = _pip_install_cmd()
        cmd[-2] = INSTALL_DIR  # "-e <dir>" without extras
        subprocess.run(cmd, check=False)


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

    # Persist the version we ended up on, so the next check compares correctly
    # (otherwise a successful update would be re-applied every cycle).
    ended = result.get("version") if result.get("updated") else (current if result.get("rolled_back") else None)
    if ended:
        try:
            with open(f"{INSTALL_DIR}/.git-ref", "w") as fh:
                fh.write(str(ended))
        except OSError as exc:
            logger.warning("OTA: could not write .git-ref: %s", exc)

    # Exit non-zero only if we ended up unhealthy after a rollback.
    return 1 if result.get("rolled_back") and not result.get("recovered") else 0


if __name__ == "__main__":
    sys.exit(main())
