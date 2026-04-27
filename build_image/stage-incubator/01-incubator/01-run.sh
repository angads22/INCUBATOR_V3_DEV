#!/bin/bash -e
# ============================================================
#  pi-gen stage: install Incubator v3 onto the Pi OS image
#  Runs in an ARM chroot via QEMU during the image build.
# ============================================================

INSTALL_DIR="/opt/incubator"
VENV_DIR="${INSTALL_DIR}/.venv"
DATA_DIR="/var/incubator"
SERVICE_NAME="incubator"

# ── Copy app source into /opt/incubator ─────────────────────
install -d "${ROOTFS_DIR}${INSTALL_DIR}"
cp -r files/app_src/. "${ROOTFS_DIR}${INSTALL_DIR}/"

# ── Copy service and helper files ───────────────────────────
install -m 755 files/firstboot.sh        "${ROOTFS_DIR}/usr/local/bin/incubator-firstboot"
install -m 644 files/incubator-firstboot.service \
    "${ROOTFS_DIR}/etc/systemd/system/incubator-firstboot.service"

# Render the main service file (substitute __INSTALL_DIR__)
sed "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    "${ROOTFS_DIR}${INSTALL_DIR}/deploy/incubator.service" \
    > "${ROOTFS_DIR}/etc/systemd/system/${SERVICE_NAME}.service"

# ── Data directories ────────────────────────────────────────
install -d "${ROOTFS_DIR}${DATA_DIR}/captures"
install -d "${ROOTFS_DIR}${DATA_DIR}/models/vision"
install -d "${ROOTFS_DIR}${DATA_DIR}/models/llm"
install -d "${ROOTFS_DIR}${INSTALL_DIR}/database"

# ── Python venv + pip install (runs in ARM chroot via QEMU) ─
on_chroot << CHROOT
set -e

echo "[incubator] Creating Python virtual environment..."
python3 -m venv --system-site-packages "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip --prefer-binary --quiet \
    --extra-index-url https://www.piwheels.org/simple

echo "[incubator] Installing Python dependencies..."
"${VENV_DIR}/bin/pip" install \
    --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    "${INSTALL_DIR}[pi]" \
    --quiet

# TFLite is optional — don't fail the build if unavailable
echo "[incubator] Attempting TFLite install (optional)..."
"${VENV_DIR}/bin/pip" install \
    --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    tflite-runtime numpy \
    --quiet 2>/dev/null \
    && echo "[incubator] TFLite installed." \
    || echo "[incubator] TFLite not available — vision will use mock/api backend."

echo "[incubator] Enabling camera interface..."
raspi-config nonint do_camera 0 2>/dev/null || true

echo "[incubator] Configuring NetworkManager..."
systemctl enable NetworkManager

echo "[incubator] Enabling incubator services..."
systemctl enable incubator-firstboot.service
systemctl enable ${SERVICE_NAME}.service

echo "[incubator] Setting GPIO permissions for root..."
usermod -aG gpio,dialout,video root 2>/dev/null || true

echo "[incubator] Stage complete."
CHROOT
