#!/usr/bin/env bash
# Pull latest code, reinstall deps, and restart the systemd service (or print
# instructions for manual restart if systemd is not available).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SERVICE_NAME="incubator-v3"
cd "$PROJECT_ROOT"

git pull

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run ./init_unoq.sh first."
  exit 1
fi

source .venv/bin/activate
python -m pip install -e . -q
python -c "import app.main; print('[OK] Import check passed')"

if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
  _run_as_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] && "$@" || sudo "$@"; }
  echo "[INFO] Restarting $SERVICE_NAME service..."
  _run_as_root systemctl restart "${SERVICE_NAME}.service"
  sleep 2
  _run_as_root systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  echo "[OK] Service restarted."
else
  echo "[OK] Update complete. Restart manually with: ./scripts/start.sh"
fi
