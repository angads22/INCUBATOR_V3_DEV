#!/usr/bin/env bash
# Pull latest code, reinstall deps, and restart the systemd service (or print
# instructions for manual restart if systemd is not available).
#
# Works on a normal clone AND on an installed appliance checkout (which has
# an `origin` remote but no upstream branch: install.sh creates it with
# `git init` and OTA leaves it detached on a release tag).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
SERVICE_NAME="incubator"
API_PORT="${API_PORT:-8000}"
cd "$PROJECT_ROOT"

_run_as_root() { if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then "$@"; else sudo "$@"; fi; }

# ── Fetch the newest code ──────────────────────────────────────────────────
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  git pull --ff-only
else
  echo "[INFO] No upstream branch configured — fetching origin directly."
  git fetch --tags origin
  if git rev-parse --verify -q origin/main >/dev/null; then
    git checkout -q --force origin/main
  else
    # Fall back to the newest release tag (img-<version>-<date>).
    latest_tag="$(git tag -l 'img-*' --sort=-v:refname | head -1)"
    [[ -n "$latest_tag" ]] || { echo "[ERROR] origin/main not found and no img-* tags."; exit 1; }
    echo "[INFO] Checking out latest release tag ${latest_tag}"
    git checkout -q --force "$latest_tag"
  fi
fi

if [[ ! -d .venv ]]; then
  echo "[ERROR] .venv not found. Run: sudo bash install.sh"
  exit 1
fi

# ── Reinstall (same extras logic as install.sh / OTA) ──────────────────────
source .venv/bin/activate
PIP_INDEX=()
if [[ -r /etc/os-release ]] && grep -qiE '^(ID|ID_LIKE)=.*(debian|raspbian|ubuntu)' /etc/os-release \
   && [[ "$(uname -m)" =~ ^(armv6l|armv7l|aarch64)$ ]]; then
  PIP_INDEX=(--extra-index-url https://www.piwheels.org/simple)
fi
python -m pip install ${PIP_INDEX[@]+"${PIP_INDEX[@]}"} -e . -q
python -m pip install ${PIP_INDEX[@]+"${PIP_INDEX[@]}"} -e ".[hardware]" -q 2>/dev/null \
  || echo "[WARN] hardware extras not installed (continuing)"
if [[ -r /proc/device-tree/model ]] && tr -d '\0' < /proc/device-tree/model | grep -q "Raspberry Pi"; then
  python -m pip install ${PIP_INDEX[@]+"${PIP_INDEX[@]}"} -e ".[pi]" -q 2>/dev/null \
    || echo "[WARN] Pi extras not installed (continuing)"
fi
python -c "import app.main; print('[OK] Import check passed')"

# Record the version we are on so the OTA agent compares against it correctly.
grep -oP 'VERSION\s*=\s*"\K[^"]+' app/version.py > .git-ref 2>/dev/null || true

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
