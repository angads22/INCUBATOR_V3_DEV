#!/usr/bin/env bash
# Incubator OTA update agent
#
# Invoked by incubator-ota.service (systemd timer, every 15 min by default).
# Checks the central server for a newer application version and applies it
# atomically with automatic rollback on health-check failure.
#
# This script lives in git so that an OTA update can also update the agent.
# The image build hook (rpi-image/layer/incubator/hooks/30-ota-setup) installs
# this same script into the image at build time.
#
# Required env (read from /etc/incubator.env):
#   ENABLE_CLOUD_SYNC=true
#   DOMAIN_API_BASE=https://your-server.example.com
#   DEVICE_SHARED_SECRET=<secret>
#
# Server API contract:
#   GET  {DOMAIN_API_BASE}/api/v1/ota/check
#        ?device_id=PI-xxxx&version=1.30&sha=abc123
#   Headers: Authorization: Bearer <secret>
#            X-Device-Id: PI-xxxx
#   Response:
#     { "update_available": false,
#       "version": "1.31",
#       "git_ref": "v1.31.0",
#       "force_update": false }
set -euo pipefail

INSTALL_DIR="/opt/incubator"
LOCK_FILE="/run/incubator-ota.lock"
ENV_FILE="/etc/incubator.env"
LOG_TAG="incubator-ota"
HEALTH_URL="http://127.0.0.1:8000/health"
HEALTH_RETRIES=10
HEALTH_SLEEP=3

_log()  { logger -t "${LOG_TAG}" "$*"; echo "[OTA] $*"; }
_die()  { _log "ERROR: $*"; exit 1; }

# ── Guard: prevent concurrent runs ─────────────────────────────────────────
exec 200>"${LOCK_FILE}"
flock -n 200 || { _log "Another OTA run is already in progress — exiting."; exit 0; }
trap 'rm -f ${LOCK_FILE}' EXIT

# ── Load env ───────────────────────────────────────────────────────────────
[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}" || true

ENABLE_CLOUD_SYNC="${ENABLE_CLOUD_SYNC:-false}"
DOMAIN_API_BASE="${DOMAIN_API_BASE:-}"
DEVICE_SHARED_SECRET="${DEVICE_SHARED_SECRET:-}"
DEVICE_ID_FILE="/etc/incubator-device-id"

if [[ "${ENABLE_CLOUD_SYNC}" != "true" || -z "${DOMAIN_API_BASE}" || -z "${DEVICE_SHARED_SECRET}" ]]; then
    _log "Cloud sync disabled or not configured — skipping OTA check."
    exit 0
fi

DEVICE_ID="$(cat "${DEVICE_ID_FILE}" 2>/dev/null || echo "")"
[[ -z "${DEVICE_ID}" ]] && _die "Device ID not found at ${DEVICE_ID_FILE}. Run scripts/firstboot.sh first."

CURRENT_VERSION="$(cat "${INSTALL_DIR}/.git-ref" 2>/dev/null || echo "unknown")"
CURRENT_SHA="$(git -C "${INSTALL_DIR}" rev-parse HEAD 2>/dev/null || echo "unknown")"

# ── Check for update ───────────────────────────────────────────────────────
_log "Checking for updates (device=${DEVICE_ID} version=${CURRENT_VERSION} sha=${CURRENT_SHA:0:8})"

OTA_RESPONSE="$(curl -sf \
    --max-time 15 \
    -H "Authorization: Bearer ${DEVICE_SHARED_SECRET}" \
    -H "X-Device-Id: ${DEVICE_ID}" \
    "${DOMAIN_API_BASE}/api/v1/ota/check?device_id=${DEVICE_ID}&version=${CURRENT_VERSION}&sha=${CURRENT_SHA}" \
)" || { _log "Could not reach OTA server — will retry next cycle."; exit 0; }

UPDATE_AVAILABLE="$(echo "${OTA_RESPONSE}" | jq -r '.update_available // false')"
TARGET_REF="$(echo "${OTA_RESPONSE}"       | jq -r '.git_ref         // empty')"
TARGET_VERSION="$(echo "${OTA_RESPONSE}"   | jq -r '.version         // empty')"

if [[ "${UPDATE_AVAILABLE}" != "true" ]]; then
    _log "No update available. Running version ${CURRENT_VERSION} is current."
    exit 0
fi

[[ -z "${TARGET_REF}" ]] && _die "Server returned update_available=true but no git_ref"
_log "Update available: ${CURRENT_VERSION} → ${TARGET_VERSION} (ref=${TARGET_REF})"

# ── Apply update ───────────────────────────────────────────────────────────
PREV_SHA="${CURRENT_SHA}"

_rollback() {
    local prev_sha="$1"
    _log "ROLLBACK: reverting to ${prev_sha:0:8}"
    git -C "${INSTALL_DIR}" checkout "${prev_sha}"
    echo "${CURRENT_VERSION}" > "${INSTALL_DIR}/.git-ref"
    echo "${prev_sha}"        > "${INSTALL_DIR}/.git-sha"
    "${INSTALL_DIR}/.venv/bin/pip" install \
        --extra-index-url https://www.piwheels.org/simple \
        -e "${INSTALL_DIR}[pi]" --quiet 2>/dev/null || true
    systemctl restart incubator.service || true
    _die "Update rolled back to ${prev_sha:0:8}. Check: journalctl -u incubator -n 50"
}

_log "Fetching ref ${TARGET_REF}..."
git -C "${INSTALL_DIR}" fetch --depth 1 origin "${TARGET_REF}"
git -C "${INSTALL_DIR}" checkout FETCH_HEAD

NEW_SHA="$(git -C "${INSTALL_DIR}" rev-parse HEAD)"
echo "${TARGET_VERSION}" > "${INSTALL_DIR}/.git-ref"
echo "${NEW_SHA}"        > "${INSTALL_DIR}/.git-sha"

_log "Installing Python dependencies..."
"${INSTALL_DIR}/.venv/bin/pip" install \
    --extra-index-url https://www.piwheels.org/simple \
    -e "${INSTALL_DIR}[pi]" --quiet \
    || { _log "pip install failed — rolling back"; _rollback "${PREV_SHA}"; }

# ── Restart and verify ─────────────────────────────────────────────────────
_log "Restarting incubator service..."
systemctl daemon-reload
systemctl restart incubator.service

_log "Waiting for service health check (up to $((HEALTH_RETRIES * HEALTH_SLEEP))s)..."
for i in $(seq 1 ${HEALTH_RETRIES}); do
    if curl -sf --max-time 5 "${HEALTH_URL}" > /dev/null 2>&1; then
        _log "Health check passed — update complete."
        _log "Now running: ${TARGET_VERSION} (sha=${NEW_SHA:0:8})"
        exit 0
    fi
    _log "Health check ${i}/${HEALTH_RETRIES} pending..."
    sleep "${HEALTH_SLEEP}"
done

_log "Service failed health check after update — rolling back to ${PREV_SHA:0:8}"
_rollback "${PREV_SHA}"
