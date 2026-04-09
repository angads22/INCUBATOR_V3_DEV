#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SERVICE_NAME="incubator-v3"
API_PORT="${API_PORT:-8000}"
cd "$PROJECT_ROOT"

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run ./init_unoq.sh first."
  exit 1
fi

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  echo "[INFO] ${SERVICE_NAME}.service is already running and owns port ${API_PORT}."
  echo "[INFO] For local dev foreground mode, stop service first: ./scripts/stop.sh"
  exit 0
fi

source .venv/bin/activate
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$API_PORT"
