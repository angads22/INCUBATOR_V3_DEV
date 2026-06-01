#!/usr/bin/env bash
# Incubator first-boot provisioning script.
#
# Runs once on first power-on via incubator-firstboot.service
# (guarded by /etc/incubator-firstboot.done).  Safe to run manually to
# re-provision a device.
#
# Usage: sudo bash scripts/firstboot.sh
set -euo pipefail

ENV_FILE="/etc/incubator.env"
DONE_MARKER="/etc/incubator-firstboot.done"
DEVICE_ID_FILE="/etc/incubator-device-id"
LOG_TAG="incubator-firstboot"

_log() { logger -t "${LOG_TAG}" "$*" 2>/dev/null || true; echo "[FIRSTBOOT] $*"; }

[[ "${EUID:-$(id -u)}" -ne 0 ]] && { echo "Run as root: sudo bash $0"; exit 1; }

_log "Starting first-boot provisioning..."

# ── Generate device ID ─────────────────────────────────────────────────────
if [[ ! -f "${DEVICE_ID_FILE}" ]]; then
    PI_SERIAL="$(grep -m1 Serial /proc/cpuinfo 2>/dev/null | awk '{print $NF}' || true)"
    if [[ -n "${PI_SERIAL}" && "${PI_SERIAL}" != "0000000000000000" ]]; then
        DEVICE_ID="PI-${PI_SERIAL^^}"
    else
        DEVICE_ID="PI-$(cat /proc/sys/kernel/random/uuid 2>/dev/null | tr -d '-' | cut -c1-16 | tr '[:lower:]' '[:upper:]')"
    fi
    echo "${DEVICE_ID}" > "${DEVICE_ID_FILE}"
    chmod 600 "${DEVICE_ID_FILE}"
    _log "Device ID: ${DEVICE_ID}"
fi

DEVICE_ID="$(cat "${DEVICE_ID_FILE}")"
SHORT_ID="${DEVICE_ID: -4}"

# ── Set unique hostname ────────────────────────────────────────────────────
BASE_HOSTNAME="$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]' | sed 's/-[0-9a-f]\{4\}$//')"
NEW_HOSTNAME="${BASE_HOSTNAME}-${SHORT_ID,,}"
echo "${NEW_HOSTNAME}" > /etc/hostname
hostnamectl set-hostname "${NEW_HOSTNAME}" 2>/dev/null || true
if grep -qE "^127\.0\.1\.1" /etc/hosts 2>/dev/null; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${NEW_HOSTNAME}/" /etc/hosts
else
    echo "127.0.1.1	${NEW_HOSTNAME}" >> /etc/hosts
fi
_log "Hostname: ${NEW_HOSTNAME}"

# ── Seed environment file ─────────────────────────────────────────────────
INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "${ENV_FILE}" ]]; then
    _log "Creating ${ENV_FILE}..."
    cp "${INSTALL_DIR}/deploy/incubator.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
fi

# Randomise AP password
AP_PASS="$(dd if=/dev/urandom bs=9 count=1 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | cut -c1-12)"
sed -i "s|INCUBATOR_AP_PASSWORD=.*|INCUBATOR_AP_PASSWORD=${AP_PASS}|" "${ENV_FILE}"

# Inject device ID into env file
if ! grep -q "^INCUBATOR_DEVICE_ID=" "${ENV_FILE}"; then
    printf '\n# Auto-generated on first boot\nINCUBATOR_DEVICE_ID=%s\n' "${DEVICE_ID}" >> "${ENV_FILE}"
else
    sed -i "s|INCUBATOR_DEVICE_ID=.*|INCUBATOR_DEVICE_ID=${DEVICE_ID}|" "${ENV_FILE}"
fi

_log "Environment file updated at ${ENV_FILE}"

# ── Expand root filesystem if needed ──────────────────────────────────────
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_expand_rootfs 2>/dev/null || true
fi

# ── Mark as done ──────────────────────────────────────────────────────────
touch "${DONE_MARKER}"

_log "First-boot provisioning complete."
echo ""
echo "  Device ID:   ${DEVICE_ID}"
echo "  Hostname:    ${NEW_HOSTNAME}"
echo "  AP SSID:     Incubator-${SHORT_ID^^}"
echo "  AP Password: ${AP_PASS}  (also in ${ENV_FILE})"
echo "  Dashboard:   http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '10.42.0.1'):8000"
echo ""
echo "  Logs:  journalctl -u incubator -f"
