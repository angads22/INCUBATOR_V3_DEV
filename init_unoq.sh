#!/usr/bin/env bash
set -euo pipefail

# One-command UNO Q bootstrap.
# Usage:
#   sudo ./init_unoq.sh
# Optional env overrides:
#   INSTALL_DIR=/opt/incubator-v3 SERVICE_USER=root DB_PATH=/opt/incubator-v3/incubator.db SERIAL_PORT=/dev/ttyUSB0 SERIAL_BAUD=115200 ./init_unoq.sh

INSTALL_DIR=${INSTALL_DIR:-/opt/incubator-v3}
SERVICE_NAME=${SERVICE_NAME:-incubator-v3.service}
SERVICE_USER=${SERVICE_USER:-root}
DB_PATH=${DB_PATH:-$INSTALL_DIR/incubator.db}
SERIAL_PORT=${SERIAL_PORT:-/dev/ttyUSB0}
SERIAL_BAUD=${SERIAL_BAUD:-115200}
SERIAL_TIMEOUT=${SERIAL_TIMEOUT:-1.0}
API_PORT=${API_PORT:-8000}

if [[ "$EUID" -ne 0 ]]; then
  echo "[ERROR] Run as root: sudo ./init_unoq.sh"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 is required"
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

mkdir -p "$INSTALL_DIR"
cp -a . "$INSTALL_DIR/"
cd "$INSTALL_DIR"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

cat >/etc/incubator-v3.env <<ENVVARS
INCUBATOR_DB_URL=sqlite:///$DB_PATH
INCUBATOR_SERIAL_PORT=$SERIAL_PORT
INCUBATOR_SERIAL_BAUD=$SERIAL_BAUD
INCUBATOR_SERIAL_TIMEOUT=$SERIAL_TIMEOUT
ENVVARS

cat >/etc/systemd/system/$SERVICE_NAME <<SERVICE
[Unit]
Description=Incubator v3 API (UNO Q)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=/etc/incubator-v3.env
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $API_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 1
set +e
curl -fsS "http://127.0.0.1:$API_PORT/health"
HEALTH_EXIT=$?
set -e

if [[ $HEALTH_EXIT -eq 0 ]]; then
  echo
  echo "[OK] Incubator API is running."
  echo "[OK] Health: http://127.0.0.1:$API_PORT/health"
else
  echo
  echo "[WARN] Service started but health check failed."
  echo "Run: journalctl -u $SERVICE_NAME -f"
fi
