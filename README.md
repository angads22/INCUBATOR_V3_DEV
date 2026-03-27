# Incubator v3 (UNO Q + ESP32)

Linux-first incubator backend for **Arduino UNO Q** with **ESP32** as hardware/provisioning bridge.

## Pi parity (what stays the same)

- Backend still runs on Linux with FastAPI + SQLite.
- Core API behavior remains similar (`/health`, setup flow, hardware dispatch).
- Business logic remains on the Linux board.

## What changed from Pi

- No direct Pi GPIO/camera assumptions.
- Hardware/camera actions route through ESP32 over UART via service abstractions.

## Quick start (local dev)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## One-command UNO Q initialize (after pull)

```bash
sudo ./init_unoq.sh
```

This command:

1. Creates/updates `.venv`
2. Installs package dependencies
3. Writes `/etc/incubator-v3.env`
4. Installs/updates systemd service
5. Restarts service and runs `/health` check

## Deployment docs

- Full deploy guide: [`docs/UNOQ_DEPLOY.md`](docs/UNOQ_DEPLOY.md)
- Env template: [`deploy/incubator-v3.env.example`](deploy/incubator-v3.env.example)
- Service template: [`deploy/incubator-v3.service`](deploy/incubator-v3.service)

## Next step

Integrate AG-robotics UART protocol + camera transfer implementation into `app/services/esp32_link.py` and `app/services/camera_service.py`.
