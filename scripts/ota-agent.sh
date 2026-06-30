#!/usr/bin/env bash
# Incubator OTA update agent (GitHub Releases, direct).
#
# Invoked by incubator-ota.service (systemd timer, every 15 min by default).
# Checks the latest GitHub Release of this repo and, if it is a newer version,
# applies it atomically and verifies health — rolling back automatically if the
# unit doesn't come up healthy. Only the web service (incubator.service) is
# restarted; incubator-control.service keeps incubation running throughout.
#
# The decision/apply/verify/rollback logic lives in `python -m app.ota`
# (app/ota/updater.py), which is unit-tested including the forced-failure
# rollback path. This wrapper just provides the run-lock and environment.
set -euo pipefail

INSTALL_DIR="${INCUBATOR_INSTALL_DIR:-/opt/incubator}"
LOCK_FILE="/run/incubator-ota.lock"
ENV_FILE="/etc/incubator.env"
LOG_TAG="incubator-ota"

_log() { logger -t "${LOG_TAG}" "$*" 2>/dev/null || true; echo "[OTA] $*"; }

# ── Guard: prevent concurrent runs ─────────────────────────────────────────
exec 200>"${LOCK_FILE}"
flock -n 200 || { _log "Another OTA run is already in progress — exiting."; exit 0; }

# ── Load env ───────────────────────────────────────────────────────────────
[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}" || true
OTA_ENABLED="${OTA_ENABLED:-true}"
if [[ "${OTA_ENABLED}" != "true" ]]; then
    _log "OTA disabled (OTA_ENABLED != true) — skipping."
    exit 0
fi

PYTHON="${INSTALL_DIR}/.venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON="python3"

_log "Checking GitHub Releases for a newer version..."
INCUBATOR_INSTALL_DIR="${INSTALL_DIR}" exec "${PYTHON}" -m app.ota
