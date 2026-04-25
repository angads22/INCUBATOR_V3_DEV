#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SERVICE_NAME="incubator"
API_PORT="${API_PORT:-8000}"
cd "$PROJECT_ROOT"

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run: sudo bash init_pi.sh"
  exit 1
fi

# In mock mode no GPIO hardware is required — safe for dev machines
export GPIO_MOCK="${GPIO_MOCK:-true}"
export CAMERA_BACKEND="${CAMERA_BACKEND:-mock}"
export VISION_BACKEND="${VISION_BACKEND:-mock}"

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "[INFO] ${SERVICE_NAME}.service is already running on port ${API_PORT}."
  echo "[INFO] Stop it first for foreground dev mode: ./scripts/stop.sh"
  exit 0
fi

source .venv/bin/activate
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT"
