# Incubator v3 (UNO Q + ESP32)

This repository contains a migration scaffold for porting the Pi-based incubator app to an Arduino UNO Q (Debian Linux) + ESP32 architecture.

## Does this work similarly to the Pi app?

Yes—at the backend level, this is designed to behave similarly:

- UNO Q runs the Linux backend (FastAPI + SQLite), like the Pi used to run app logic.
- Route pattern is preserved for core actions (`/health`, setup flow, hardware send).
- Main difference: hardware access is abstracted and routed through ESP32 over UART instead of Pi-specific GPIO/camera drivers.

## What this scaffold includes

- FastAPI backend intended to run on UNO Q Linux.
- SQLite data model for local-first auth, onboarding claim-state, and incubator telemetry.
- Board-agnostic hardware abstraction layer.
- UART bridge service for UNO Q <-> ESP32 communication.
- Camera service abstraction so ESP32-CAM and direct UNO Q camera paths can coexist later.
- First-boot claim flow where the first local admin user is created by onboarding.

## Quick start (dev)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## One-command setup on UNO Q (after pull)

From repo root on the UNO Q:

```bash
sudo ./init_unoq.sh
```

This bootstraps venv + dependencies, writes `/etc/incubator-v3.env`, installs/updates the systemd service, restarts it, and runs a local health check.

## How to deploy/push this onto Arduino UNO Q

Recommended flow:

1. Build and commit locally.
2. Copy or clone this repo onto UNO Q.
3. Install dependencies in a Python venv on UNO Q.
4. Run as a `systemd` service for auto-start.

See: [`docs/UNOQ_DEPLOY.md`](docs/UNOQ_DEPLOY.md)

## Next integration step

Integrate AG-robotics reference code for real UART command protocol framing and camera transfer implementation once that repository is available in this environment.
