#!/usr/bin/env bash
# ============================================================
#  Incubator v3 — Raspberry Pi Zero 2W initialization script
#  Run once on a fresh Raspberry Pi OS (Bookworm) install.
#
#  Usage:
#    sudo bash init_pi.sh [install_dir]
#    Default install_dir: /opt/incubator
# ============================================================
set -euo pipefail

INSTALL_DIR="${1:-/opt/incubator}"
SERVICE_NAME="incubator"
ENV_FILE="/etc/${SERVICE_NAME}.env"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${INSTALL_DIR}/.venv"
DATA_DIR="/var/incubator"

# IMAGE_BUILD=1 is set by build_image.sh when running inside a chroot while
# baking an SD-card image. In that mode there is no running init system, so we
# only *enable* units (create symlinks) and skip daemon-reload/start/health
# checks — the service starts on the first real boot instead.
IMAGE_BUILD="${INCUBATOR_IMAGE_BUILD:-0}"

# Wi-Fi regulatory country (ISO 3166-1 alpha-2). On RPi OS Bookworm the WLAN
# radio is rfkill soft-blocked until a country is set, so the onboarding hotspot
# never appears without it. Passed in by build_image.sh; defaults to US.
WIFI_COUNTRY="${INCUBATOR_WIFI_COUNTRY:-US}"
[[ "$WIFI_COUNTRY" =~ ^[A-Za-z]{2}$ ]] || WIFI_COUNTRY="US"
WIFI_COUNTRY="${WIFI_COUNTRY^^}"

# ── Colour helpers ────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Preflight checks ─────────────────────────────────────────
[[ "$EUID" -ne 0 ]] && error "Run as root: sudo bash $0"

info "Raspberry Pi Zero 2W — Incubator v3 setup"
info "Install dir: ${INSTALL_DIR}"
info "Repo dir:    ${REPO_DIR}"
echo ""

# ── System packages ──────────────────────────────────────────
info "Installing system packages..."
# apt-get can transiently fail under cross-arch emulation (image builds); retry.
apt-get update -qq \
    || { warn "apt-get update failed; retrying in 3s..."; sleep 3; apt-get update -qq; } \
    || error "apt-get update failed."

# Essential: the app, onboarding/account creation + auth, and the Wi-Fi hotspot
# all need these. A failure here is fatal — there's no usable device without them.
# Raspberry Pi OS package names can vary by Debian base (e.g. t64 transition), so
# detect the available runtime package before installing essentials.
GPIO_RUNTIME_PKG=""
for pkg in libgpiod2 libgpiod2t64 libgpiod3; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
        GPIO_RUNTIME_PKG="$pkg"
        break
    fi
done
[[ -n "$GPIO_RUNTIME_PKG" ]] || error "No compatible libgpiod runtime package found."

apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    git curl ca-certificates \
    network-manager \
    rfkill iw \
    "$GPIO_RUNTIME_PKG" \
    libjpeg-dev zlib1g-dev \
    || error "Failed to install essential system packages."

# Regulatory database — lets `iw reg set <country>` actually take effect.
# Best-effort: a Bookworm Lite image normally already ships it.
apt-get install -y --no-install-recommends wireless-regdb \
    || warn "wireless-regdb not installed — regulatory domain may not apply."

# Camera + BLAS are heavier and only used for vision/candling, not for the
# first-boot setup/auth flow. Their maintainer scripts (libcamera, etc.) can
# choke under QEMU emulation, so don't let that abort an image build — they can
# be added on the device later: sudo apt install python3-picamera2 libatlas-base-dev
apt-get install -y --no-install-recommends \
    python3-picamera2 libatlas-base-dev \
    || warn "Camera/BLAS packages not installed (install on-device later if needed)."

apt-get clean

# Enable camera interface if raspi-config is available
if command -v raspi-config &>/dev/null; then
    info "Enabling camera interface..."
    raspi-config nonint do_camera 0 || true
fi

# Ensure NetworkManager is enabled (needed for hotspot AP mode)
if [[ "$IMAGE_BUILD" == "1" ]]; then
    systemctl enable NetworkManager || true
else
    systemctl enable --now NetworkManager || true
fi

# ── Wi-Fi regulatory country (unblocks the radio) ────────────
# Without a country set, Bookworm keeps the WLAN radio rfkill soft-blocked and
# the onboarding hotspot never comes up. Persist the country and make sure the
# radio is unblocked + the regulatory domain applied on every boot.
info "Configuring Wi-Fi regulatory country: ${WIFI_COUNTRY}"
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_wifi_country "${WIFI_COUNTRY}" || warn "raspi-config do_wifi_country failed (continuing)."
fi
# crda regdomain (best-effort; harmless if crda is absent on Bookworm).
echo "REGDOMAIN=${WIFI_COUNTRY}" > /etc/default/crda 2>/dev/null || true

# Oneshot unit: unblock WLAN + apply the regulatory domain before the radio is
# used. Ordered before NetworkManager and the app so the hotspot can start.
cat > /etc/systemd/system/incubator-wifi-country.service <<EOF
[Unit]
Description=Incubator: unblock WLAN radio and set Wi-Fi regulatory domain
Before=NetworkManager.service ${SERVICE_NAME}.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'rfkill unblock wlan || true; iw reg set ${WIFI_COUNTRY} || true'

[Install]
WantedBy=multi-user.target
EOF
systemctl enable incubator-wifi-country.service 2>/dev/null \
    || ln -sf /etc/systemd/system/incubator-wifi-country.service \
        /etc/systemd/system/multi-user.target.wants/incubator-wifi-country.service

# ── Captive-portal DNS hijack (setup hotspot) ────────────────
# NetworkManager's shared (AP) mode runs its own dnsmasq. Resolving every
# domain to the AP IP while the hotspot is up makes phones/laptops fire their
# "sign in to network" captive-portal flow, which the app's :80 responder
# turns into the onboarding page. This only affects the shared/AP connection;
# once the Pi joins a real network as a client it is not in shared mode.
info "Installing captive-portal DNS config for the setup hotspot..."
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/090-incubator-captive.conf <<EOF
# Resolve all names to the incubator AP so the onboarding portal auto-opens.
address=/#/${INCUBATOR_AP_IP:-10.42.0.1}
EOF

# ── Create install directory and copy files ──────────────────
mkdir -p "${INSTALL_DIR}"
if [[ "$IMAGE_BUILD" == "1" && "$REPO_DIR" == "$INSTALL_DIR" ]]; then
    info "Image build — application already staged at ${INSTALL_DIR}."
else
    info "Deploying application to ${INSTALL_DIR}..."
    rsync -a --delete \
        --exclude='.git' \
        --exclude='.venv' \
        --exclude='*.pyc' \
        --exclude='__pycache__' \
        --exclude='captures' \
        --exclude='dist' \
        "${REPO_DIR}/" "${INSTALL_DIR}/"
fi

# ── Data directories ─────────────────────────────────────────
info "Creating data directories..."
mkdir -p "${DATA_DIR}/captures"
mkdir -p "${DATA_DIR}/models/vision"
mkdir -p "${DATA_DIR}/models/llm"
mkdir -p "${INSTALL_DIR}/database"

# Update image capture dir to use persistent storage
if [ -f "${INSTALL_DIR}/deploy/incubator.env.example" ]; then
    sed -i "s|CAMERA_IMAGE_DIR=.*|CAMERA_IMAGE_DIR=${DATA_DIR}/captures|" \
        "${INSTALL_DIR}/deploy/incubator.env.example" 2>/dev/null || true
fi

# ── Python virtual environment ───────────────────────────────
info "Creating Python virtual environment..."
python3 -m venv --system-site-packages "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet

info "Installing Python dependencies..."
"${VENV_DIR}/bin/pip" install \
    --extra-index-url https://www.piwheels.org/simple \
    -e "${INSTALL_DIR}[pi]" \
    --quiet

# Try TFLite runtime (optional — skip if unavailable for this Pi version)
"${VENV_DIR}/bin/pip" install \
    --extra-index-url https://www.piwheels.org/simple \
    tflite-runtime numpy \
    --quiet 2>/dev/null || warn "TFLite runtime not available — vision will use mock or API backend"

# Trim the pip download cache so the baked image stays as small as possible.
# A smaller image flashes onto more SD cards; Raspberry Pi OS auto-expands the
# root filesystem to fill the whole card on first boot anyway.
"${VENV_DIR}/bin/pip" cache purge >/dev/null 2>&1 || true
rm -rf /root/.cache/pip "${HOME:-/root}/.cache/pip" 2>/dev/null || true

# ── Environment file ─────────────────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    info "Creating ${ENV_FILE}..."
    cp "${INSTALL_DIR}/deploy/incubator.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    # Open setup network — no Wi-Fi password. The operator joins "Incubator-XXXX"
    # and creates their account in the captive-portal wizard.
    sed -i "s|INCUBATOR_AP_PASSWORD=.*|INCUBATOR_AP_PASSWORD=|" "${ENV_FILE}"
    info "Setup AP is open (no Wi-Fi password)."
    # Bake the Wi-Fi country so the running app applies the same regulatory
    # domain before starting the hotspot.
    if grep -q '^INCUBATOR_WIFI_COUNTRY=' "${ENV_FILE}"; then
        sed -i "s|^INCUBATOR_WIFI_COUNTRY=.*|INCUBATOR_WIFI_COUNTRY=${WIFI_COUNTRY}|" "${ENV_FILE}"
    else
        echo "INCUBATOR_WIFI_COUNTRY=${WIFI_COUNTRY}" >> "${ENV_FILE}"
    fi
else
    warn "${ENV_FILE} already exists — skipping (edit manually if needed)"
    chmod 600 "${ENV_FILE}" 2>/dev/null || true
fi

# ── systemd service ──────────────────────────────────────────
info "Installing systemd service..."
sed \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    "${INSTALL_DIR}/deploy/incubator.service" \
    > "${SERVICE_FILE}"

# Enable the unit. `systemctl enable` works offline (in a chroot) too, but fall
# back to a manual wants-symlink if systemd is unavailable.
systemctl enable "${SERVICE_NAME}" 2>/dev/null \
    || ln -sf "${SERVICE_FILE}" "/etc/systemd/system/multi-user.target.wants/${SERVICE_NAME}.service"

# ── Control daemon unit (Phase 3) ─────────────────────────────
# Always-on control loop that survives app updates. Safe to enable on every
# image: the daemon idles (no GPIO) until CONTROL_DAEMON_ENABLED=true, so it
# never contends with the web app. Flip the flag in /etc/incubator.env (and
# restart both units) to activate closed-loop control — validate on a unit with
# no eggs incubating first.
if [ -f "${INSTALL_DIR}/deploy/incubator-control.service" ]; then
    info "Installing control-daemon service (idle until CONTROL_DAEMON_ENABLED)..."
    sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
        "${INSTALL_DIR}/deploy/incubator-control.service" \
        > /etc/systemd/system/incubator-control.service
    systemctl enable incubator-control.service 2>/dev/null \
        || ln -sf /etc/systemd/system/incubator-control.service \
            "/etc/systemd/system/multi-user.target.wants/incubator-control.service"
fi

# ── OTA update timer ──────────────────────────────────────────
# Periodically check GitHub Releases and apply newer versions (app/ota) with
# health-checked rollback, restarting only the web service. Without this timer
# a flashed image never auto-updates.
if [ -f "${INSTALL_DIR}/deploy/incubator-ota.service" ]; then
    info "Installing OTA update timer (checks GitHub Releases every 15 min)..."
    sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
        "${INSTALL_DIR}/deploy/incubator-ota.service" > /etc/systemd/system/incubator-ota.service
    cp "${INSTALL_DIR}/deploy/incubator-ota.timer" /etc/systemd/system/incubator-ota.timer
    chmod +x "${INSTALL_DIR}/scripts/ota-agent.sh" 2>/dev/null || true
    systemctl enable incubator-ota.timer 2>/dev/null \
        || ln -sf /etc/systemd/system/incubator-ota.timer \
            /etc/systemd/system/timers.target.wants/incubator-ota.timer
fi

# ── Make the app a git checkout so OTA can update from GitHub ──
# OTA applies updates by fetching/checking out release tags. The staged app has
# no .git, so initialize one pointed at the GitHub remote and record the baked
# version. .gitignore keeps .venv/data untracked, and OTA's force-checkout
# leaves untracked files in place.
APP_VERSION="$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${INSTALL_DIR}/app/version.py" 2>/dev/null || echo dev)"
if [ ! -d "${INSTALL_DIR}/.git" ]; then
    info "Initializing ${INSTALL_DIR} as a git checkout for OTA (version ${APP_VERSION})..."
    git -C "${INSTALL_DIR}" init -q
    git -C "${INSTALL_DIR}" add -A
    git -C "${INSTALL_DIR}" -c user.email=build@incubator -c user.name=incubator-build \
        commit -qm "Image base v${APP_VERSION}" || true
    git -C "${INSTALL_DIR}" remote add origin https://github.com/angads22/INCUBATOR_V3_DEV.git 2>/dev/null \
        || git -C "${INSTALL_DIR}" remote set-url origin https://github.com/angads22/INCUBATOR_V3_DEV.git
fi
echo "${APP_VERSION}" > "${INSTALL_DIR}/.git-ref"

# ── GPIO permissions ─────────────────────────────────────────
# Add the service user (root) to gpio group — already root so skip.
# If you change User= in the service file, add that user to gpio group.
usermod -aG gpio,dialout,video root 2>/dev/null || true

# ── Start + health check (skipped during image build) ────────
if [[ "$IMAGE_BUILD" == "1" ]]; then
    info "Image build complete — service enabled, will start on first boot."
    info "First boot broadcasts open AP 'Incubator-XXXX' (no password) at http://10.42.0.1:8000"
    exit 0
fi

systemctl daemon-reload
systemctl restart "${SERVICE_NAME}"

info "Waiting for service to start..."
sleep 5
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Service is running."
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Incubator v3 — Pi Zero 2W setup complete!        ${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Dashboard:    http://${IP}:8000"
    echo ""
    echo "  On FIRST BOOT (no WiFi configured) the Pi broadcasts:"
    echo "  AP SSID:  Incubator-XXXX  (open — no password)"
    echo "  URL:      http://10.42.0.1:8000"
    echo ""
    echo "  Hold the setup button (GPIO18) for 4 s to re-enter setup mode."
    echo ""
    echo "  Logs:   journalctl -u ${SERVICE_NAME} -f"
    echo "  Config: ${ENV_FILE}"
    echo ""
else
    error "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 50"
fi
