#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="incubator-v3"
API_PORT="${API_PORT:-8000}"

_run_as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "[WARN] sudo not found; cannot run: $*"
    return 1
  fi
}

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
    echo "[INFO] Stopping ${SERVICE_NAME}.service..."
    _run_as_root systemctl stop "${SERVICE_NAME}.service" || true
  else
    echo "[INFO] ${SERVICE_NAME}.service is not active."
  fi
else
  echo "[WARN] systemctl not available; skipping service stop."
fi

if command -v ps >/dev/null 2>&1; then
  mapfile -t _uvicorn_pids < <(ps -eo pid=,args= | awk '/[u]vicorn .*app.main:app/ {print $1}')
  if [[ ${#_uvicorn_pids[@]} -gt 0 ]]; then
    echo "[INFO] Stopping uvicorn process(es): ${_uvicorn_pids[*]}"
    _run_as_root kill "${_uvicorn_pids[@]}" 2>/dev/null || true
  fi
fi

if command -v fuser >/dev/null 2>&1; then
  _run_as_root fuser -k "${API_PORT}/tcp" 2>/dev/null || true
fi

echo "[OK] Stop complete."
