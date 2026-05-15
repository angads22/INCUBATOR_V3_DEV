# Incubator v3 (Raspberry Pi Zero 2W)

Local-first FastAPI incubator controller for the Raspberry Pi Zero 2W.
Runs on the Pi with direct GPIO access (DHT22, relays, motor, candle LED,
buzzer, lock, door, setup button) and an optional Pi Camera for egg candling.

## What this is

- FastAPI app served by uvicorn on port 8000.
- SQLite persistence (`./database/incubator.db`).
- Jinja2 templated operator UI under `/`, `/status`, `/settings`,
  `/hardware`, `/onboarding`, `/help`, `/login`.
- WiFi AP onboarding (`/onboarding`) for first-boot setup, with a physical
  setup button on GPIO18 to re-enter setup mode.
- Pluggable vision backend: `mock`, on-device `tflite`, or remote `api`.

## User-facing routes

Frontend:

- `/` dashboard
- `/status`
- `/settings`
- `/hardware`
- `/login`
- `/onboarding`
- `/help`

API:

- `GET /health`
- `GET /setup/status`
- `POST /setup/complete`
- `GET /api/sensors/latest`
- `POST /hardware/send`
- `POST /onboarding/start`
- `GET /onboarding/wifi-scan`
- `POST /onboarding/complete`
- `POST /api/settings`
- `POST /api/login`
- `POST /api/logout`
- `POST /api/vision/analyze`
- `POST /api/vision/candle`
- `POST /api/ai/chat`
- `POST /api/ai/explain-status`
- `GET /docs`, `GET /openapi.json`

## Quick start — dev machine (no Pi hardware)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
GPIO_MOCK=true CAMERA_BACKEND=mock VISION_BACKEND=mock \
  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or use the helper script (sets the same env vars):

```bash
./scripts/start.sh
```

Open <http://localhost:8000>. All hardware calls return simulated data.

## Quick start — Raspberry Pi Zero 2W

```bash
git clone https://github.com/angads22/incubator_v3_dev /home/pi/incubator
sudo bash /home/pi/incubator/init_pi.sh /opt/incubator
```

`init_pi.sh` installs apt deps (`python3-picamera2`, `libgpiod2`,
NetworkManager, etc.), creates `/opt/incubator/.venv`, writes
`/etc/incubator.env` with a randomised AP password, installs the
`incubator` systemd unit, and starts it.

On first boot (no WiFi configured) the Pi broadcasts an
`Incubator-XXXX` hotspot at `http://10.42.0.1:8000` for onboarding.
Hold the setup button on **GPIO18** for **~4 seconds** to re-enter
setup mode at any time.

Full hardware/wiring and deploy guide: [`docs/PI_DEPLOY.md`](docs/PI_DEPLOY.md).

## Updating

```bash
./scripts/update.sh           # pulls, reinstalls deps, restarts service
```

To stop:

```bash
./scripts/stop.sh
```

## Configuration

All runtime settings live in `/etc/incubator.env` (template:
[`deploy/incubator.env.example`](deploy/incubator.env.example)). Key knobs:

| Variable | Default | Description |
|---|---|---|
| `GPIO_MOCK` | `false` | Set `true` to run without Pi hardware |
| `CAMERA_BACKEND` | `picamera2` | `picamera2` / `opencv` / `mock` |
| `VISION_BACKEND` | `mock` | `tflite` / `api` / `mock` |
| `VISION_API_URL` | *(empty)* | Remote vision model endpoint |
| `INCUBATOR_REQUIRE_LOGIN` | `false` | Enforce password login on UI |
| `INCUBATOR_SESSION_SECURE` | `false` | Set `true` when behind HTTPS |
| `SENSOR_POLL_INTERVAL` | `30` | DHT22 poll interval (seconds) |
| `INCUBATOR_AP_SSID_PREFIX` | `Incubator` | Hotspot SSID prefix |
| `INCUBATOR_AP_IP` | `10.42.0.1` | Hotspot gateway IP |

Reload after edits:

```bash
sudo systemctl restart incubator
```

## Deployment files

- Systemd unit template: [`deploy/incubator.service`](deploy/incubator.service)
- Env template: [`deploy/incubator.env.example`](deploy/incubator.env.example)
- Install script: [`init_pi.sh`](init_pi.sh)
- Pi deploy guide: [`docs/PI_DEPLOY.md`](docs/PI_DEPLOY.md)

## Remote access (optional)

Do not expose port 8000 directly to the internet. Use one of:

1. Reverse proxy (Nginx/Caddy) with HTTPS + auth gate.
2. VPN-only (Tailscale subnet route or device ACL).
3. Authenticated tunnel (Cloudflare Tunnel with Access policies).

When fronted by HTTPS, set `INCUBATOR_SESSION_SECURE=true` so the
session cookie carries the `Secure` flag.

## Vision model integration

The candling workflow lives at `POST /api/vision/candle`:

1. Turns on the candle LED (GPIO24).
2. Captures an image via `CameraService`.
3. Runs inference via `VisionService.analyze_egg_image()`.
4. Always turns the candle LED off.
5. Optionally persists the result to `model_results`.

Swap backends without code changes via `VISION_BACKEND` and (for `api`)
`VISION_API_URL` + `VISION_API_KEY`. See `docs/PI_DEPLOY.md` for the
expected request/response payload.

## Layout

```
app/            FastAPI app, routes, services, templates, static assets
database/       SQLite schema + migrations
deploy/         systemd unit and env templates
docs/           Pi deployment guide
init_pi.sh      one-shot Pi installer
models/         Local TFLite/LLM model storage (.gitkeep only)
scripts/        start.sh / stop.sh / update.sh / bump_version.py
training/       Optional model training scaffolding (.gitkeep only)
```
