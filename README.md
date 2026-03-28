# Incubator v3 (UNO Q + ESP32)

Local-first incubator control app running on Arduino UNO Q with FastAPI.

## Current focus

- Main control web app at `/` (dashboard-first)
- Simulation mode by default (no physical hardware required)
- Login optional by default for bring-up (`INCUBATOR_REQUIRE_LOGIN=false`)
- Stable JSON API for future iOS/Android clients
- Systemd-friendly deployment on-device

## Audit summary

Active runtime paths:

- `app/main.py` (UI + API entrypoint)
- `app/providers/*` (hardware abstraction interface + simulated/hardware providers)
- `app/services/*` (ESP32 bridge + inference placeholder)
- `app/templates/*`, `app/static/app.css` (operator UI)
- `app/models.py`, `app/database.py`, `app/settings_store.py` (persistence + settings)

Pi reference repo was not available inside this container session; layout and terminology were aligned to your spec and can be tightened further when repo access is provided.

## Device modes

Set with environment vars:

- `INCUBATOR_DEVICE_MODE`
- `INCUBATOR_REQUIRE_LOGIN`

Device mode values:

- `simulated` (default): realistic mock sensor/control behavior
- `hardware`: uses ESP32 provider over UART bridge

Check current mode:

- `GET /api/device-mode`

## Routes

Frontend:

- `/` dashboard
- `/settings`
- `/status`
- `/login`

API (core):

- `GET /api/health`
- `GET /api/status`
- `GET /api/environment`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/control/heater`
- `POST /api/control/fan`
- `POST /api/control/turn`
- `POST /api/control/reset-alarm`
- `GET /api/device-mode`

Future AI slot:

- `POST /api/viability/predict` (placeholder; inference module not enabled yet)

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## systemd

- Service: `deploy/incubator-v3.service`
- Env file: `deploy/incubator-v3.env.example`
- Quick init: `sudo ./init_unoq.sh`

## Private hosting guidance (later)

Do not expose this publicly without access controls.
Use one:

1. Reverse proxy with HTTPS + auth (OIDC/Basic)
2. VPN-only access (e.g., Tailscale)
3. Authenticated private tunnel

## Mobile-app readiness

- Core logic is API-first, not template-bound.
- Stable typed responses for status/environment/settings.
- UI uses the same API the mobile app can use later.

## TODO

- Add exact Pi-UI parity once Pi repo is available.
- Add CSRF hardening + role-based auth.
- Implement real inference service behind `/api/viability/predict`.
- Expand onboarding/provisioning after core operations are stable.
