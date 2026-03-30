# Incubator v3 (UNO Q + ESP32)

UNO Q-hosted FastAPI incubator control app with ESP32 UART hardware bridge.

## Audit summary (current repo)

## Version history

- **1.30 (feature, current)**: Registered the web router in `app/main.py`, mounted static files, and restored the `/` HTML dashboard route while keeping `/docs` available.
- **1.20 (previous)**: API-focused baseline release for UNO Q + ESP32 backend scaffolding and deployment flow, before the web router was wired into the FastAPI app.

Active, authoritative files currently used by runtime:

- `app/main.py` (FastAPI routes, templates, API, auth/session wiring)
- `app/models.py`, `app/database.py` (SQLAlchemy persistence)
- `app/services/*` (hardware and camera abstraction)
- `app/templates/*` + `app/static/app.css` (operator UI)
- `deploy/incubator-v3.service`, `deploy/incubator-v3.env.example`, `init_unoq.sh` (UNO Q deployment)

No extra dead-end modules are used by the current runtime path.

## Pi-app reference note

The original Pi repository was not available in this container session, so parity was implemented from your provided requirements (dashboard hierarchy, appliance-style control flow, terminology, status-first layout). When the Pi repo path/URL is available, UI text/layout can be tightened further for exact parity.

## User-facing routes

Frontend:

- `/` dashboard
- `/settings`
- `/status`
- `/login`
- `/onboarding`

API:

- `GET /api/health`
- `GET /api/status`
- `GET /api/environment`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/control/heater`
- `POST /api/control/fan`
- `POST /api/control/turn`
- `POST /api/login`
- `POST /api/logout`

## Local run (UNO Q)
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

## systemd

- Service template: `deploy/incubator-v3.service`
- Env template: `deploy/incubator-v3.env.example`
- Full deploy guide: `docs/UNOQ_DEPLOY.md`

## Private remote hosting guidance (recommended)

Do **not** expose this app publicly without an access layer.

Preferred options:

1. Reverse proxy (Nginx/Caddy) with HTTPS + auth gate (OIDC/BasicAuth)
2. VPN-only (e.g., Tailscale subnet route/device ACL)
3. Authenticated private tunnel (Cloudflare Tunnel Access policies)

Security defaults:

- Session cookie is HTTPOnly.
- Set `INCUBATOR_SESSION_SECURE=true` when behind HTTPS.
- Keep host firewall locked to trusted ingress path only.

## Known TODOs

- Integrate Pi repo exact visual/wording parity once repo is available.
- Replace placeholder ESP32 command names with AG-robotics protocol mappings.
- Add role-based authorization and stronger CSRF protection for control endpoints.
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
