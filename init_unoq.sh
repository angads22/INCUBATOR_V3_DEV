#!/usr/bin/env bash
# UNO Q — full install / setup / systemd service install.
# Safe to re-run: stops any old service, overwrites venv and service file.
# Usage:
#   ./init_unoq.sh                          # installs from current directory
#   INSTALL_DIR=/opt/incubator-v3 ./init_unoq.sh   # copies repo then installs
#
# After running:
#   ./scripts/start.sh   — foreground dev run
#   ./scripts/stop.sh    — stop service/stale foreground process
#   ./scripts/update.sh  — git pull, reinstall deps, restart service

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INSTALL_DIR=${INSTALL_DIR:-"$SCRIPT_DIR"}
API_PORT=${API_PORT:-8000}
SERVICE_NAME="incubator-v3"
ENV_FILE="/etc/incubator-v3.env"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
OLD_SERVICE_DESTS=(
  "/etc/systemd/system/incubator-v3-dev.service"
  "/etc/systemd/system/incubator_v3.service"
)

# ── 1. System packages ────────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
  echo "[INFO] Installing required system packages..."
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv git curl ca-certificates
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv git curl ca-certificates
  else
    echo "[WARN] Not root and sudo is unavailable; skipping apt package install."
  fi
else
  echo "[WARN] apt-get not available; ensure python3, python3-pip, python3-venv, git, and curl are installed."
fi

# ── 2. Copy repo to INSTALL_DIR if different ─────────────────────────────────
cd "$SCRIPT_DIR"
if [[ "$INSTALL_DIR" != "$SCRIPT_DIR" ]]; then
  echo "[INFO] Copying repo to $INSTALL_DIR..."
  mkdir -p "$INSTALL_DIR"
  cp -a . "$INSTALL_DIR/"
fi
cd "$INSTALL_DIR"

# ── 3. Stop any running old service / stale process on port ──────────────────
_run_as_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] && "$@" || sudo "$@"; }

if command -v systemctl >/dev/null 2>&1; then
  # Stop every known service name variant
  for svc in "$SERVICE_NAME" incubator-v3-dev incubator_v3; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
      echo "[INFO] Stopping old service: $svc"
      _run_as_root systemctl stop "$svc" || true
    fi
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
      _run_as_root systemctl disable "$svc" || true
    fi
  done
fi

# Kill any lingering uvicorn app process to keep only one repo instance active
if command -v ps >/dev/null 2>&1; then
  mapfile -t _uvicorn_pids < <(ps -eo pid=,args= | awk '/[u]vicorn .*app.main:app/ {print $1}')
  if [[ ${#_uvicorn_pids[@]} -gt 0 ]]; then
    echo "[INFO] Stopping stale uvicorn process(es): ${_uvicorn_pids[*]}"
    _run_as_root kill "${_uvicorn_pids[@]}" 2>/dev/null || true
  fi
fi

# Kill anything lingering on port $API_PORT
if command -v fuser >/dev/null 2>&1; then
  _run_as_root fuser -k "${API_PORT}/tcp" 2>/dev/null || true
fi

# ── 4. Create / refresh virtualenv ───────────────────────────────────────────
if [[ -d .venv ]]; then
  echo "[INFO] Removing old .venv for clean install..."
  rm -rf .venv
fi
echo "[INFO] Creating .venv..."
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip -q
python -m pip install -e . -q

# ── 5. Verify import ─────────────────────────────────────────────────────────
python -c "import app.main; print('[OK] Import check passed')"

# ── 6. Make scripts executable ───────────────────────────────────────────────
chmod +x init_unoq.sh scripts/start.sh scripts/stop.sh scripts/update.sh scripts/deploy_local_unoq.sh 2>/dev/null || true

# ── 7. Install systemd service (if systemd is available) ─────────────────────
if command -v systemctl >/dev/null 2>&1; then
  # Write env file if missing (mode 600 so only root can read it)
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "[INFO] Writing default env file to $ENV_FILE..."
    _run_as_root bash -c "
      umask 177
      cat > '$ENV_FILE' <<'ENVEOF'
INCUBATOR_DB_URL=sqlite:///${INSTALL_DIR}/incubator.db
INCUBATOR_SERIAL_PORT=/dev/ttyUSB0
INCUBATOR_SERIAL_BAUD=115200
INCUBATOR_SERIAL_TIMEOUT=1.0
ENVEOF
"
  fi

  echo "[INFO] Installing systemd service to $SERVICE_DEST..."
  for old_unit in "${OLD_SERVICE_DESTS[@]}"; do
    if [[ -f "$old_unit" ]]; then
      echo "[INFO] Removing old unit file: $old_unit"
      _run_as_root rm -f "$old_unit"
    fi
  done

  # Use mktemp to avoid /tmp symlink attacks
  _TMP_SERVICE=$(mktemp)
  sed "s|__INSTALL_DIR__|${INSTALL_DIR}|g" deploy/incubator-v3.service > "$_TMP_SERVICE"
  _run_as_root cp "$_TMP_SERVICE" "$SERVICE_DEST"
  rm -f "$_TMP_SERVICE"

  _run_as_root systemctl daemon-reload
  _run_as_root systemctl enable "${SERVICE_NAME}.service"
  _run_as_root systemctl restart "${SERVICE_NAME}.service"
  sleep 2
  _run_as_root systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  echo "[OK] Service installed and started."
else
  echo "[INFO] systemd not available — skipping service install."
  echo "       Run the app manually with: ./scripts/start.sh"
fi

echo
echo "[OK] Setup complete."
echo "Commands:"
echo "  Start (dev foreground): ./scripts/start.sh"
echo "  Stop app/service:       ./scripts/stop.sh"
echo "  Update from git:        ./scripts/update.sh"
echo "  Open UI:                http://127.0.0.1:${API_PORT}/"
echo "  API docs:               http://127.0.0.1:${API_PORT}/docs"
echo
