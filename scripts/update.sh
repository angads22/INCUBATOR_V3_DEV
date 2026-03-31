#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$PROJECT_ROOT"

git pull

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run ./init_unoq.sh first."
  exit 1
fi

source .venv/bin/activate
python -m pip install -e .

echo "[OK] Update complete. Restart with ./scripts/start.sh"
