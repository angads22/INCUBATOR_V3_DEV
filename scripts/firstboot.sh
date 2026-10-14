#!/usr/bin/env bash
# Incubator first-boot provisioning script (any Linux SBC).
#
# Runs once on first power-on via incubator-firstboot.service
# (guarded by /etc/incubator-firstboot.done).  Safe to run manually to
# re-provision a device.
#
#   * stable device ID  — Pi serial → PI-<SERIAL>; else /etc/machine-id →
#                         INC-<16 hex>; else a random UUID → INC-<16 hex>
#   * unique hostname   — <base>-<last 4 of the id, lowercase>
#   * root-fs expand    — raspi-config when present, else growpart + resize2fs
#   * seeds /etc/incubator.env (open setup AP, device id)
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
_hex16() { tr -d '-' | tr -cd '0-9a-fA-F' | cut -c1-16 | tr '[:lower:]' '[:upper:]'; }

if [[ ! -f "${DEVICE_ID_FILE}" ]]; then
    DEVICE_ID=""
    # Raspberry Pi (and a few other SoCs) expose a board serial in /proc/cpuinfo.
    SERIAL="$(grep -m1 '^Serial' /proc/cpuinfo 2>/dev/null | awk '{print $NF}' || true)"
    if [[ -z "${SERIAL}" && -r /proc/device-tree/serial-number ]]; then
        SERIAL="$(tr -d '\0' < /proc/device-tree/serial-number 2>/dev/null || true)"
    fi
    IS_PI=0
    if [[ -r /proc/device-tree/model ]] && tr -d '\0' < /proc/device-tree/model | grep -q "Raspberry Pi"; then
        IS_PI=1
    fi
    if [[ -n "${SERIAL}" && ! "${SERIAL}" =~ ^0+$ ]]; then
        if [[ "${IS_PI}" == "1" ]]; then
            DEVICE_ID="PI-${SERIAL^^}"
        else
            DEVICE_ID="INC-$(printf '%s' "${SERIAL}" | _hex16)"
        fi
    fi
    if [[ -z "${DEVICE_ID}" || "${DEVICE_ID}" == "INC-" ]]; then
        MID="$(cat /etc/machine-id 2>/dev/null | _hex16 || true)"
        if [[ -n "${MID}" && ${#MID} -ge 8 && ! "${MID}" =~ ^0+$ ]]; then
            DEVICE_ID="INC-${MID}"
        fi
    fi
    if [[ -z "${DEVICE_ID}" ]]; then
        DEVICE_ID="INC-$(cat /proc/sys/kernel/random/uuid 2>/dev/null | _hex16)"
    fi
    echo "${DEVICE_ID}" > "${DEVICE_ID_FILE}"
    chmod 600 "${DEVICE_ID_FILE}"
    _log "Device ID: ${DEVICE_ID}"
fi

DEVICE_ID="$(cat "${DEVICE_ID_FILE}")"
SHORT_ID="${DEVICE_ID: -4}"

# ── Set unique hostname ────────────────────────────────────────────────────
BASE_HOSTNAME="$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]' | sed 's/-[0-9a-f]\{4\}$//')"
[[ -n "${BASE_HOSTNAME}" && "${BASE_HOSTNAME}" != "localhost" ]] || BASE_HOSTNAME="incubator"
NEW_HOSTNAME="${BASE_HOSTNAME}-${SHORT_ID,,}"
echo "${NEW_HOSTNAME}" > /etc/hostname
hostnamectl set-hostname "${NEW_HOSTNAME}" 2>/dev/null || hostname "${NEW_HOSTNAME}" 2>/dev/null || true
if grep -qE "^127\.0\.1\.1" /etc/hosts 2>/dev/null; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${NEW_HOSTNAME}/" /etc/hosts
else
    echo "127.0.1.1	${NEW_HOSTNAME}" >> /etc/hosts
fi
_log "Hostname: ${NEW_HOSTNAME}"
# avahi announces <hostname>.local — the address the setup wizard hands out —
# from the name it read at start-up, so make it pick up the new one now.
if command -v systemctl >/dev/null 2>&1; then
    systemctl try-restart avahi-daemon.service 2>/dev/null || true
fi

# ── Seed environment file ─────────────────────────────────────────────────
INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -f "${ENV_FILE}" ]]; then
    _log "Creating ${ENV_FILE}..."
    cp "${INSTALL_DIR}/deploy/incubator.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
fi

# Open setup network — no Wi-Fi password. The operator joins "Incubator-XXXX"
# and creates their account in the captive-portal wizard; security lives in the
# account login, not a printed AP key.
sed -i "s|INCUBATOR_AP_PASSWORD=.*|INCUBATOR_AP_PASSWORD=|" "${ENV_FILE}"

# Inject device ID into env file
if ! grep -q "^INCUBATOR_DEVICE_ID=" "${ENV_FILE}"; then
    printf '\n# Auto-generated on first boot\nINCUBATOR_DEVICE_ID=%s\n' "${DEVICE_ID}" >> "${ENV_FILE}"
else
    sed -i "s|INCUBATOR_DEVICE_ID=.*|INCUBATOR_DEVICE_ID=${DEVICE_ID}|" "${ENV_FILE}"
fi

_log "Environment file updated at ${ENV_FILE}"

# ── Expand root filesystem if needed ──────────────────────────────────────
# A flashed image is smaller than the card. Raspberry Pi OS has raspi-config;
# everywhere else try cloud-utils' growpart + resize2fs on the live root
# (ext4 grows online). Best effort — a failure here never blocks boot.
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_expand_rootfs 2>/dev/null || true
elif command -v growpart &>/dev/null && command -v resize2fs &>/dev/null; then
    ROOT_SRC="$(findmnt -no SOURCE / 2>/dev/null || true)"
    ROOT_FS="$(findmnt -no FSTYPE / 2>/dev/null || true)"
    if [[ "${ROOT_SRC}" == /dev/* && "${ROOT_FS}" == "ext4" ]]; then
        ROOT_SRC="$(readlink -f "${ROOT_SRC}")"
        PART_NUM="$(lsblk -no PARTN "${ROOT_SRC}" 2>/dev/null | tr -d '[:space:]' || true)"
        [[ -n "${PART_NUM}" ]] || PART_NUM="$(printf '%s' "${ROOT_SRC}" | grep -oE '[0-9]+$' || true)"
        DISK_NAME="$(lsblk -no PKNAME "${ROOT_SRC}" 2>/dev/null | tr -d '[:space:]' || true)"
        if [[ -n "${DISK_NAME}" ]]; then
            DISK="/dev/${DISK_NAME}"
        else
            # /dev/mmcblk0p2 → /dev/mmcblk0 ; /dev/sda2 → /dev/sda ; /dev/nvme0n1p2 → /dev/nvme0n1
            DISK="$(printf '%s' "${ROOT_SRC}" | sed -E 's/p?[0-9]+$//')"
        fi
        if [[ -n "${PART_NUM}" && -b "${DISK}" ]]; then
            _log "Expanding root partition ${DISK} #${PART_NUM} (${ROOT_SRC})..."
            # growpart exits 1 for NOCHANGE (already full size) — not an error.
            growpart "${DISK}" "${PART_NUM}" >/dev/null 2>&1 || true
            resize2fs "${ROOT_SRC}" >/dev/null 2>&1 || _log "resize2fs skipped/failed (continuing)."
        fi
    fi
fi

# ── Mark as done ──────────────────────────────────────────────────────────
touch "${DONE_MARKER}"

_log "First-boot provisioning complete."
echo ""
echo "  Device ID:   ${DEVICE_ID}"
echo "  Hostname:    ${NEW_HOSTNAME}"
echo "  AP SSID:     Incubator-${SHORT_ID^^}  (open — no password)"
echo "  Dashboard:   http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '10.42.0.1'):8000"
echo ""
echo "  Logs:  journalctl -u incubator -f"
