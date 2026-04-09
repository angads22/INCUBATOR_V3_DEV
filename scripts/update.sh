#!/usr/bin/env bash
# Pull latest code, reinstall deps, and restart the systemd service (or print
# instructions for manual restart if systemd is not available).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SERVICE_NAME="incubator-v3"
API_PORT="${API_PORT:-8000}"
cd "$PROJECT_ROOT"

_run_as_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] && "$@" || sudo "$@"; }

git pull

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run ./init_unoq.sh first."
  exit 1
fi

source .venv/bin/activate
python -m pip install -e . -q
python -c "import app.main; print('[OK] Import check passed')"

# Keep only one app instance after update.
if command -v ps >/dev/null 2>&1; then
  mapfile -t _uvicorn_pids < <(ps -eo pid=,args= | awk '/[u]vicorn .*app.main:app/ {print $1}')
  if [[ ${#_uvicorn_pids[@]} -gt 0 ]]; then
    echo "[INFO] Stopping stale uvicorn process(es): ${_uvicorn_pids[*]}"
    _run_as_root kill "${_uvicorn_pids[@]}" 2>/dev/null || true
  fi
fi

if command -v fuser >/dev/null 2>&1; then
  _run_as_root fuser -k "${API_PORT}/tcp" 2>/dev/null || true
fi

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --type=service | grep -qE "^${SERVICE_NAME}\\.service\\s+"; then
  echo "[INFO] Restarting $SERVICE_NAME service..."
  _run_as_root systemctl daemon-reload
  _run_as_root systemctl enable "${SERVICE_NAME}.service"
  _run_as_root systemctl restart "${SERVICE_NAME}.service"
  sleep 2
  _run_as_root systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
  echo "[OK] Service restarted."
else
  echo "[OK] Update complete. Service not installed; restart manually with: ./scripts/start.sh"
fi
