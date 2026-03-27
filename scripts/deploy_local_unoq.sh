#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=${1:-/opt/incubator-v3}

cd "$REPO_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

if [[ -f /etc/incubator-v3.env ]]; then
  cp deploy/incubator-v3.service /etc/systemd/system/incubator-v3.service
  systemctl daemon-reload
  systemctl enable incubator-v3.service
  systemctl restart incubator-v3.service
  systemctl --no-pager --full status incubator-v3.service || true
else
  echo "[WARN] /etc/incubator-v3.env not found. Copy deploy/incubator-v3.env.example first."
fi
