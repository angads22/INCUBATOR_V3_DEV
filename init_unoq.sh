#!/usr/bin/env bash
set -euo pipefail

# Idempotent one-command setup for UNO Q.
# Recommended usage (from repo root):
#   sudo ./init_unoq.sh

SERVICE_NAME=${SERVICE_NAME:-incubator-v3.service}
SERVICE_USER=${SERVICE_USER:-root}
INSTALL_DIR=${INSTALL_DIR:-$(pwd)}
API_HOST=${API_HOST:-0.0.0.0}
API_PORT=${API_PORT:-8000}
ENV_FILE=${ENV_FILE:-/etc/incubator-v3.env}

SERIAL_PORT=${SERIAL_PORT:-/dev/ttyUSB0}
SERIAL_BAUD=${SERIAL_BAUD:-115200}
SERIAL_TIMEOUT=${SERIAL_TIMEOUT:-1.0}
DEVICE_MODE=${DEVICE_MODE:-simulated}
REQUIRE_LOGIN=${REQUIRE_LOGIN:-false}
SESSION_SECURE=${SESSION_SECURE:-false}

if [[ "$EUID" -ne 0 ]]; then
  echo "[ERROR] Run as root: sudo ./init_unoq.sh"
  exit 1
fi

if [[ ! -f "pyproject.toml" || ! -f "app/main.py" ]]; then
  echo "[ERROR] Run this from the incubator repo root."
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 not found"; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "[ERROR] systemctl not found"; exit 1; }

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [[ ! -f app/main.py || ! -f app/templates/index.html || ! -f app/static/app.css ]]; then
  echo "[ERROR] Required frontend files missing in $INSTALL_DIR"
  exit 1
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -e .

python - <<'PYCHK'
from app.main import app
import app.main as main_mod
paths = {getattr(r, "path", "") for r in app.routes}
assert "/" in paths, "Root route '/' missing from app route table"
assert "/api/health" in paths, "Health route missing"
print(f"[INFO] app.main loaded from: {main_mod.__file__}")
print("[INFO] Route check passed: root and health routes are registered")
PYCHK

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<ENVVARS
INCUBATOR_DB_URL=sqlite:///$INSTALL_DIR/incubator.db
INCUBATOR_SERIAL_PORT=$SERIAL_PORT
INCUBATOR_SERIAL_BAUD=$SERIAL_BAUD
INCUBATOR_SERIAL_TIMEOUT=$SERIAL_TIMEOUT
INCUBATOR_DEVICE_MODE=$DEVICE_MODE
INCUBATOR_REQUIRE_LOGIN=$REQUIRE_LOGIN
INCUBATOR_SESSION_SECURE=$SESSION_SECURE
ENVVARS
  echo "[INFO] Created $ENV_FILE"
else
  echo "[INFO] Reusing existing $ENV_FILE"
fi

cat >/etc/systemd/system/$SERVICE_NAME <<SERVICE
[Unit]
Description=Incubator v3 API (UNO Q)
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app.main:app --host $API_HOST --port $API_PORT
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
curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null
HEALTH_OK=$?
curl -fsS "http://127.0.0.1:$API_PORT/docs" >/dev/null
DOCS_OK=$?
ROOT_HEADERS=$(curl -si "http://127.0.0.1:$API_PORT/" | head -n 10)
ROOT_CODE=$(printf "%s" "$ROOT_HEADERS" | awk 'NR==1 {print $2}')
printf "%s" "$ROOT_HEADERS" | grep -Ei "content-type: text/html" >/dev/null
ROOT_HTML=$?
set -e

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo
if [[ $HEALTH_OK -eq 0 && $DOCS_OK -eq 0 && "$ROOT_CODE" == "200" && "$ROOT_HTML" == "0" ]]; then
  echo "[OK] Service is running and dashboard is reachable."
else
  echo "[WARN] One or more checks failed (health=$HEALTH_OK docs=$DOCS_OK root=$ROOT_CODE html=$ROOT_HTML)."
  echo "[WARN] Inspect logs: journalctl -u $SERVICE_NAME -f"
fi

echo "Local dashboard: http://127.0.0.1:$API_PORT/"
if [[ -n "${LAN_IP:-}" ]]; then
  echo "LAN dashboard:   http://$LAN_IP:$API_PORT/"
fi
echo "API docs:        http://127.0.0.1:$API_PORT/docs"
