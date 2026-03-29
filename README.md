# Incubator v3 (UNO Q + ESP32)

Local-first incubator control app running on Arduino UNO Q with FastAPI.

## Current focus

- Main control web app at `/` (dashboard-first)
- Simulation mode by default (no physical hardware required)
- Login optional by default for bring-up (`INCUBATOR_REQUIRE_LOGIN=false`)
- Stable JSON API for future iOS/Android clients
- Systemd-friendly deployment on-device

## Version History

### v1.30 - 2026-03-29
Type: Feature
Summary: Split web/frontend route wiring into a dedicated router and explicitly included it in app startup.
Previous version summary: v1.21 focused on deploy-time root-route verification and stricter health checks.
Changes:
- added `app/routes/web.py` with dashboard/template routes
- registered web router via `app.include_router(web_router)` in `app/main.py`
- kept static mounting in main app bootstrap
- kept dashboard showing mock/simulated incubator values and version

### v1.21 - 2026-03-29
Type: Bugfix
Summary: Hardened deployed route verification to catch and fail on root-dashboard 404 issues.
Previous version summary: v1.20 added dashboard version visibility and `/api/version` support.
Changes:
- added startup route table logging (`Root route registered`)
- strengthened `init_unoq.sh` checks for required frontend files and route assertions
- validated root endpoint as `200` + `Content-Type: text/html` in setup checks

### v1.20 - 2026-03-29
Type: Feature
Summary: Added root index dashboard wiring, version endpoint, and dashboard version display.
Previous version summary: v1.10 established provider-based simulated/hardware app structure.
Changes:
- mounted `index.html` at `/`
- added `GET /api/version`
- added centralized version source and bump script

## Critical quick fix

If `/` shows `404`, run:

```bash
sudo ./init_unoq.sh
```

This command reinstalls the app, rewrites/refreshes systemd service config, restarts the service, and verifies:

- `GET /api/health`
- `GET /docs`
- `GET /` returns HTTP 200

## Device modes

Set with environment vars:

- `INCUBATOR_DEVICE_MODE` (`simulated` or `hardware`)
- `INCUBATOR_REQUIRE_LOGIN` (`true` / `false`)

Default for no hardware attached: `INCUBATOR_DEVICE_MODE=simulated`.

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
- `GET /api/version`

Future AI slot:

- `POST /api/viability/predict` (placeholder; inference module not enabled yet)

## Versioning rules

This project uses **M.mm** versioning:

- `1.00` initial release
- `1.01` bug fix
- `1.10` feature release
- `2.00` major release

Current version is served by `GET /api/version` and shown on the dashboard.

To bump automatically:

```bash
./scripts/bump_version.py bugfix
./scripts/bump_version.py feature
./scripts/bump_version.py major
```

## Setup / deploy on UNO Q

From repo root:

```bash
git pull
sudo ./init_unoq.sh
```

The script is designed to be idempotent and safe to rerun after updates.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Private remote hosting guidance (later)

Do not expose publicly without access controls. Preferred options:

1. Reverse proxy with HTTPS + auth (OIDC/Basic)
2. VPN-only access (e.g., Tailscale)
3. Authenticated private tunnel

## Mobile-app readiness

- Core logic is API-first, not template-bound.
- Typed status/environment/settings response models.
- Web UI and future mobile clients can share the same API.

## TODO

- Align exact UI copy/layout to Pi repo once full Pi source is available.
- Add CSRF hardening + role-based auth.
- Implement real inference service behind `/api/viability/predict`.
- Expand onboarding/provisioning after core operations are stable.
