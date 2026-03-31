#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run ./init_unoq.sh first."
  exit 1
fi

source .venv/bin/activate
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
