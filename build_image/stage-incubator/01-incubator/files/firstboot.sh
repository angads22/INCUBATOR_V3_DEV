#!/usr/bin/env bash
# ============================================================
#  Incubator v3 — first-boot initialisation
#  Runs once via incubator-firstboot.service on the very first
#  boot after flashing.  Creates /etc/incubator.env with a
#  unique random AP password so every Pi gets its own hotspot.
# ============================================================
set -euo pipefail

ENV_SRC="/opt/incubator/deploy/incubator.env.example"
ENV_DEST="/etc/incubator.env"

echo "[firstboot] Incubator v3 first-boot setup..."

# ── Environment file ────────────────────────────────────────
if [ ! -f "${ENV_DEST}" ]; then
    cp "${ENV_SRC}" "${ENV_DEST}"
    chmod 600 "${ENV_DEST}"

    # Unique random AP password for this Pi
    AP_PASS="$(dd if=/dev/urandom bs=9 count=1 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | cut -c1-12)"
    sed -i "s|INCUBATOR_AP_PASSWORD=.*|INCUBATOR_AP_PASSWORD=${AP_PASS}|" "${ENV_DEST}"

    echo "[firstboot] AP password set: ${AP_PASS}"
    echo "[firstboot] Config: ${ENV_DEST}"
fi

# ── WiFi country (needed for the radio to activate) ─────────
COUNTRY="${WPA_COUNTRY:-US}"
raspi-config nonint do_wifi_country "${COUNTRY}" 2>/dev/null \
    || echo "[firstboot] Could not set WiFi country via raspi-config — set manually if needed"

echo "[firstboot] Done."
