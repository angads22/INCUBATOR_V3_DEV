#!/usr/bin/env bash
# ============================================================
#  Incubator v3 — custom Pi OS image builder
#
#  Produces a ready-to-flash .img.xz for the Pi Zero 2W.
#  Requires: Docker (Linux / Mac / Windows WSL2)
#
#  Usage:
#    bash build_image/build.sh
#
#  Output:
#    build_image/deploy/incubator-v3-*.img.xz
#
#  Flash with:
#    Raspberry Pi Imager → "Use custom" → select the .img.xz
#    or: xz -d *.img.xz && sudo dd if=*.img of=/dev/sdX bs=4M status=progress
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PIGEN_DIR="${SCRIPT_DIR}/.pi-gen"
DEPLOY_DIR="${SCRIPT_DIR}/deploy"
STAGE_DIR="${SCRIPT_DIR}/stage-incubator"
STAGE_FILES="${STAGE_DIR}/01-incubator/files"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Preflight ────────────────────────────────────────────────
command -v docker &>/dev/null || error "Docker not found. Install Docker Desktop and try again."
docker info &>/dev/null        || error "Docker daemon not running. Start Docker Desktop and try again."

info "Incubator v3 image builder"
info "Repo:   ${REPO_DIR}"
info "Output: ${DEPLOY_DIR}"
echo ""

# ── Clone pi-gen ─────────────────────────────────────────────
if [ ! -d "${PIGEN_DIR}/.git" ]; then
    info "Cloning pi-gen (official Raspberry Pi OS builder)..."
    git clone --depth=1 https://github.com/RPi-Distro/pi-gen.git "${PIGEN_DIR}"
else
    info "pi-gen already present — pulling latest..."
    git -C "${PIGEN_DIR}" pull --ff-only || warn "Could not update pi-gen (offline?), using cached version"
fi

# ── Stage app source into files/ ────────────────────────────
info "Staging app source into build files..."
mkdir -p "${STAGE_FILES}/app_src"
rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='build_image' \
    --exclude='captures' \
    --exclude='*.img' \
    --exclude='*.img.xz' \
    "${REPO_DIR}/" "${STAGE_FILES}/app_src/"

info "App source staged ($(du -sh "${STAGE_FILES}/app_src" | cut -f1))"

# ── Patch pi-gen for Bookworm compatibility ──────────────────
# rpi-resize.service was removed in Bookworm
find "${PIGEN_DIR}" -name "*.sh" | xargs grep -rl "rpi-resize" 2>/dev/null | while read -r f; do
    sed -i '/rpi-resize/d' "${f}"
    warn "Patched: ${f}"
done

# rpi-cloud-init-mods package doesn't exist in Bookworm — skip the stage
touch "${PIGEN_DIR}/stage2/04-cloud-init/SKIP" 2>/dev/null || true
info "Patching pi-gen for Bookworm compatibility done"

# ── Wire our stage into pi-gen ───────────────────────────────
info "Linking custom stage into pi-gen..."
rm -rf "${PIGEN_DIR}/stage-incubator"
cp -r "${STAGE_DIR}" "${PIGEN_DIR}/stage-incubator"

# Mark earlier stages to not export intermediate images
for s in stage0 stage1 stage2; do
    touch "${PIGEN_DIR}/${s}/SKIP_IMAGES" 2>/dev/null || true
done

# Copy config into pi-gen root (where build-docker.sh expects it)
cp "${SCRIPT_DIR}/config" "${PIGEN_DIR}/config"

# ── Build ────────────────────────────────────────────────────
info "Starting pi-gen Docker build..."
info "This takes 30-60 min on first run (downloads + ARM emulation)."
info "Subsequent builds are faster thanks to Docker layer caching."
echo ""

mkdir -p "${DEPLOY_DIR}"
export DEPLOY_DIR

cd "${PIGEN_DIR}"
DEPLOY_DIR="${DEPLOY_DIR}" bash build-docker.sh 2>&1 | tee "${DEPLOY_DIR}/build.log"

# ── Report ───────────────────────────────────────────────────
echo ""
IMG=$(ls -t "${DEPLOY_DIR}"/*.img.xz 2>/dev/null | head -1)
if [ -n "${IMG}" ]; then
    SIZE=$(du -sh "${IMG}" | cut -f1)
    info "Build complete!"
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Image: ${IMG}${NC}"
    echo -e "${GREEN}  Size:  ${SIZE}${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Flash: Raspberry Pi Imager → Use Custom → select the .img.xz"
    echo "  Or:    xz -d \"${IMG}\" && sudo dd if=*.img of=/dev/sdX bs=4M status=progress"
    echo ""
    echo "  On first boot the Pi broadcasts:"
    echo "  WiFi:  Incubator-XXXX  (password generated on first boot)"
    echo "  URL:   http://10.42.0.1:8000"
    echo ""
else
    error "Build finished but no .img.xz found in ${DEPLOY_DIR}. Check ${DEPLOY_DIR}/build.log"
fi
