#!/usr/bin/env bash
set -euo pipefail

# UNO Q bootstrap for local-run workflow (no forced systemd setup).
# Usage:
#   ./init_unoq.sh
# Optional env overrides:
#   INSTALL_DIR=/opt/incubator-v3 ./init_unoq.sh

INSTALL_DIR=${INSTALL_DIR:-$(pwd)}
API_PORT=${API_PORT:-8000}

if ! command -v python3 >/dev/null 2>&1; then
  echo "[INFO] python3 not found yet. Attempting package install..."
fi

if command -v apt-get >/dev/null 2>&1; then
  echo "[INFO] Installing required system packages (python3, pip, venv, git, curl)..."
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv git curl ca-certificates
  elif command -v sudo >/dev/null 2>&1; then
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv git curl ca-certificates
  else
    echo "[WARN] Not root and sudo is unavailable; skipping apt package install."
  fi
else
  echo "[WARN] apt-get not available; ensure python3, python3-pip, python3-venv, git, and curl are installed."
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

if [[ "$INSTALL_DIR" != "$SCRIPT_DIR" ]]; then
  mkdir -p "$INSTALL_DIR"
  cp -a . "$INSTALL_DIR/"
  cd "$INSTALL_DIR"
fi

if [[ ! -d .venv ]]; then
  echo "[INFO] Creating .venv"
  python3 -m venv .venv
else
  echo "[INFO] Reusing existing .venv"
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

chmod +x init_unoq.sh scripts/start.sh scripts/update.sh scripts/deploy_local_unoq.sh || true

echo
echo "[OK] Setup complete."
echo "Next steps:"
echo "  1) Start app:   ./scripts/start.sh"
echo "  2) Open UI:     http://127.0.0.1:${API_PORT}/"
echo "  3) API docs:    http://127.0.0.1:${API_PORT}/docs"
echo "  4) Update app:  ./scripts/update.sh"
echo
echo "Optional later: deploy/incubator-v3.service can be used for systemd once ready."
