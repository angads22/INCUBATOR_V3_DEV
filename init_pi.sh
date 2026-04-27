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
apt-get update -qq

# Install packages that exist on all supported Pi OS versions (Bullseye/Bookworm, 32/64-bit)
apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev python3-setuptools python3-wheel \
    git curl ca-certificates rsync \
    network-manager \
    libjpeg-dev zlib1g-dev \
    > /dev/null

# libgpiod: name differs by OS version
apt-get install -y --no-install-recommends libgpiod2 > /dev/null 2>&1 \
    || apt-get install -y --no-install-recommends libgpiod-dev > /dev/null 2>&1 \
    || warn "libgpiod not found — GPIO will use mock mode"

# BLAS (for numpy/tflite): atlas on Bullseye, openblas on Bookworm
apt-get install -y --no-install-recommends libatlas-base-dev > /dev/null 2>&1 \
    || apt-get install -y --no-install-recommends libopenblas-dev > /dev/null 2>&1 \
    || warn "BLAS library not found — numpy may be slow"

# picamera2: available as system package on Bullseye/Bookworm
apt-get install -y --no-install-recommends python3-picamera2 > /dev/null 2>&1 \
    || warn "python3-picamera2 not found — set CAMERA_BACKEND=mock or CAMERA_BACKEND=opencv"

# Enable camera interface if raspi-config is available
if command -v raspi-config &>/dev/null; then
    info "Enabling camera interface..."
    raspi-config nonint do_camera 0 || true
fi

# Ensure NetworkManager is running (needed for hotspot AP mode)
systemctl enable --now NetworkManager || true

# ── Create install directory and copy files ──────────────────
info "Deploying application to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='captures' \
    "${REPO_DIR}/" "${INSTALL_DIR}/"

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

info "Installing Python dependencies (using piwheels pre-built wheels)..."
"${VENV_DIR}/bin/pip" install \
    --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    "${INSTALL_DIR}[pi]" \
    --quiet

# Try TFLite runtime (optional — skip if unavailable for this Pi version)
"${VENV_DIR}/bin/pip" install \
    --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    tflite-runtime numpy \
    --quiet 2>/dev/null || warn "TFLite runtime not available — vision will use mock or API backend"

# ── Environment file ─────────────────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    info "Creating ${ENV_FILE}..."
    cp "${INSTALL_DIR}/deploy/incubator.env.example" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
    # Seed a random AP password — use dd+xxd to stay pipefail-safe
    # (tr + head triggers SIGPIPE under set -euo pipefail on some shells)
    AP_PASS="$(dd if=/dev/urandom bs=9 count=1 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | cut -c1-12)"
    sed -i "s|INCUBATOR_AP_PASSWORD=.*|INCUBATOR_AP_PASSWORD=${AP_PASS}|" "${ENV_FILE}"
    info "AP password set to: ${AP_PASS}  (saved in ${ENV_FILE})"
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

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# ── GPIO permissions ─────────────────────────────────────────
# Add the service user (root) to gpio group — already root so skip.
# If you change User= in the service file, add that user to gpio group.
usermod -aG gpio,dialout,video root 2>/dev/null || true

# ── Health check ─────────────────────────────────────────────
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
    echo "  AP SSID:  Incubator-XXXX"
    echo "  Password: $(grep INCUBATOR_AP_PASSWORD ${ENV_FILE} | cut -d= -f2)"
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
