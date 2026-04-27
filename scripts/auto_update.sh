#!/usr/bin/env bash
# ============================================================
#  Incubator v3 — auto-update script
#
#  Checks GitHub for a newer commit on the configured branch.
#  If one is found, clones it and hot-deploys to INSTALL_DIR,
#  reinstalls pip deps, then restarts the service.
#
#  Invoked by:
#    - incubator-update.timer  (startup + weekly)
#    - POST /api/system/update (manual trigger from dashboard)
#    - sudo bash scripts/auto_update.sh  (manual CLI)
# ============================================================
set -euo pipefail

INSTALL_DIR="${INCUBATOR_INSTALL_DIR:-/opt/incubator}"
VENV_DIR="${INSTALL_DIR}/.venv"
VERSION_FILE="${INSTALL_DIR}/.version"
SERVICE_NAME="incubator"

# Read from env file if present (systemd passes these via EnvironmentFile)
ENV_FILE="/etc/incubator.env"
if [ -f "${ENV_FILE}" ]; then
    # shellcheck disable=SC1090
    set -a; source "${ENV_FILE}"; set +a
fi

REPO_URL="${INCUBATOR_REPO_URL:-https://github.com/angads22/INCUBATOR_V3_DEV.git}"
BRANCH="${INCUBATOR_UPDATE_BRANCH:-main}"
AUTO_UPDATE="${INCUBATOR_AUTO_UPDATE:-true}"

# Colour helpers (only when running interactively)
if [ -t 1 ]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; NC=''
fi
info()  { echo -e "${GREEN}[update]${NC} $*"; }
warn()  { echo -e "${YELLOW}[update]${NC} $*"; }
skip()  { echo "[update] $*"; exit 0; }

# ── Guard ────────────────────────────────────────────────────
[ "${AUTO_UPDATE}" = "true" ] || skip "INCUBATOR_AUTO_UPDATE=false — skipping"

# ── Internet check ───────────────────────────────────────────
if ! curl -sf --max-time 5 --head https://github.com > /dev/null 2>&1; then
    skip "No internet connection — skipping update check"
fi

# ── Compare local vs remote commit ───────────────────────────
LOCAL_SHA="$(cat "${VERSION_FILE}" 2>/dev/null || echo "none")"

REMOTE_SHA="$(git ls-remote "${REPO_URL}" "refs/heads/${BRANCH}" 2>/dev/null \
    | awk '{print $1}' | cut -c1-8)"

if [ -z "${REMOTE_SHA}" ]; then
    warn "Could not reach repo — skipping"
    exit 0
fi

if [ "${LOCAL_SHA}" = "${REMOTE_SHA}" ]; then
    info "Already up to date (${LOCAL_SHA})"
    exit 0
fi

info "Update available: ${LOCAL_SHA} → ${REMOTE_SHA}"
info "Repo: ${REPO_URL}  branch: ${BRANCH}"

# ── Download new version ─────────────────────────────────────
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

info "Cloning ${BRANCH}..."
git clone --depth=1 --branch "${BRANCH}" "${REPO_URL}" "${TMP}/src" --quiet

# Write version stamp into the clone
git -C "${TMP}/src" rev-parse --short HEAD > "${TMP}/src/.version"

# ── Deploy ───────────────────────────────────────────────────
info "Deploying to ${INSTALL_DIR}..."
rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='captures' \
    --exclude='.version' \
    "${TMP}/src/" "${INSTALL_DIR}/"

# Copy version file separately (rsync excluded it above)
cp "${TMP}/src/.version" "${VERSION_FILE}"

# ── Update pip dependencies ───────────────────────────────────
info "Updating Python dependencies..."
"${VENV_DIR}/bin/pip" install \
    --prefer-binary \
    --extra-index-url https://www.piwheels.org/simple \
    "${INSTALL_DIR}[pi]" \
    --quiet 2>/dev/null || warn "pip install failed — running with existing deps"

# ── Restart service ──────────────────────────────────────────
info "Restarting ${SERVICE_NAME} service..."
systemctl restart "${SERVICE_NAME}" || warn "systemctl restart failed"

info "Update complete — now at ${REMOTE_SHA}"
