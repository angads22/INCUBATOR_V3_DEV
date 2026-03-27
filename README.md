# Incubator v3 (UNO Q + ESP32)

This repository contains an initial migration scaffold for porting the Pi-based incubator app to an Arduino UNO Q (Debian Linux) + ESP32 architecture.

## What this scaffold includes

- FastAPI backend intended to run on UNO Q Linux.
- SQLite data model for local-first auth, onboarding claim-state, and incubator telemetry.
- Board-agnostic hardware abstraction layer.
- UART bridge service for UNO Q <-> ESP32 communication.
- Camera service abstraction so ESP32-CAM and direct UNO Q camera paths can coexist later.
- First-boot claim flow where the first local admin user is created by onboarding.

## Run

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Next integration step

Integrate AG-robotics reference code for real UART command protocol framing and camera transfer implementation once that repository is available in this environment.
