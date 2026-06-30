# Incubator v3 — Raspberry Pi Zero 2W

A local-first egg-incubator controller for the **Raspberry Pi Zero 2W**. It runs
a FastAPI web app that drives the incubator hardware over GPIO (heater, fan, egg
turner, candling LED, door/lock relays, DHT22 sensor, Pi Camera) and serves an
operator dashboard on the local network.

The device is an appliance: flash the SD card, power on, and finish setup from
your phone over a Wi-Fi hotspot — **no keyboard, monitor, or internet required**.

---

## Fastest path: build a ready-to-flash image

`build_image.sh` is a single script that bakes the app, its dependencies, and
the systemd service into the official Raspberry Pi OS Lite image, producing one
`.img.xz` you flash to a microSD card.

```bash
# On a Linux build host (x86 or arm64), with root:
sudo ./build_image.sh
```

This downloads Raspberry Pi OS Lite (arm64), installs everything into the image
(using `qemu` emulation on non-ARM hosts), and writes
`dist/incubator-v3-<version>-<date>.img.xz`.

Flash it with **Raspberry Pi Imager** (*Use custom* → pick the file),
**balenaEtcher**, or `dd`:

```bash
xz -dc dist/incubator-v3-*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
```

Useful options (`./build_image.sh --help` for all):

| Option | Purpose |
|--------|---------|
| `--base <path\|url>` | Use a local/alternate base image instead of downloading |
| `--hostname <name>` | Set the Pi hostname (default `incubator`) |
| `--user / --password` | Create an OS login for SSH access (optional) |
| `--grow <MB>` | Root-fs headroom for the pre-baked deps (default 2560) |
| `--no-compress` | Emit a raw `.img` instead of `.img.xz` |

> **On Windows?** The script needs Linux. Easiest path: run the **Build SD
> image** GitHub Action and download the artifact — or use WSL2. See
> [docs/BUILD_WINDOWS.md](docs/BUILD_WINDOWS.md).

### First boot (account creation + user auth)

1. Insert the card, power on the Pi.
2. It broadcasts Wi-Fi **`Incubator-XXXX`** (password printed by the build and
   stored in `/etc/incubator.env`).
3. Join that network and open **http://10.42.0.1:8000**.
4. The setup wizard walks you through:
   - selecting your home Wi-Fi network,
   - naming the device,
   - **creating an operator account** (username + email + password).
5. After setup the Pi joins your Wi-Fi. From then on the dashboard **requires
   login** — sign in at `/login` with the account you created.

Re-enter setup later by holding the **setup button (GPIO18) for 4 seconds**.

---

## Authentication model

- A fresh device is open so onboarding can run.
- As soon as an operator account exists, every page and control API
  (`/api/settings`, `/hardware/send`) requires a valid session cookie.
- Passwords are stored as PBKDF2-HMAC-SHA256 hashes; sessions are random tokens
  stored only as SHA-256 hashes server-side.
- Sign in with username **or** email at `/login`; sign out from the profile menu.
- Knobs (in `/etc/incubator.env`): `INCUBATOR_REQUIRE_LOGIN`,
  `INCUBATOR_SESSION_SECURE` (set behind HTTPS), `INCUBATOR_SESSION_TTL`.

---

## Alternative: install onto an existing Raspberry Pi OS

If you already have Raspberry Pi OS Lite (64-bit, Bookworm) running:

```bash
git clone https://github.com/angads22/incubator_v3_dev /home/pi/incubator
sudo bash /home/pi/incubator/init_pi.sh /opt/incubator
```

`init_pi.sh` installs system packages, builds a Python venv, writes
`/etc/incubator.env` with an open setup AP (no Wi-Fi password), and enables the
`incubator` systemd service. (`build_image.sh` reuses this same installer inside
the image.)

---

## Local development (no Pi hardware)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
GPIO_MOCK=true CAMERA_BACKEND=mock VISION_BACKEND=mock ./scripts/start.sh
```

Open http://localhost:8000 — all hardware/camera/vision calls return simulated
data, and the full onboarding + auth flow works.

Run the test suite (auth lifecycle, in mock mode):

```bash
pip install -e ".[dev]"
pytest
```

CI (`.github/workflows/ci.yml`) runs these tests plus `bash -n` on every push
and PR.

---

## Routes

**Web UI:** `/` (dashboard), `/status`, `/settings`, `/hardware`, `/help`,
`/onboarding`, `/login`

**API:** `GET /health`, `GET /setup/status`, `POST /setup/complete`,
`POST /api/login`, `POST /api/logout`, `POST /api/settings`,
`POST /hardware/send`, `GET /api/sensors/latest`, `GET /docs`

---

## Project layout

| Path | Purpose |
|------|---------|
| `build_image.sh` | **Build a flashable SD-card image** |
| `init_pi.sh` | On-device installer (also runs inside the image build) |
| `app/main.py` | FastAPI app, lifecycle, core API |
| `app/routes/` | Web pages, onboarding, auth, settings APIs |
| `app/auth.py` | Password hashing + session management |
| `app/services/` | GPIO, camera, Wi-Fi/hotspot, vision, onboarding |
| `app/models.py`, `app/database.py` | SQLAlchemy persistence (SQLite) |
| `app/templates/`, `app/static/` | Operator UI |
| `deploy/` | systemd unit + env template |
| `docs/PI_DEPLOY.md` | Full deployment + GPIO wiring guide |
| `docs/BUILD_WINDOWS.md` | Building the image from Windows |
| `tests/` | Auth-lifecycle tests (pytest, mock mode) |
| `.github/workflows/` | CI (lint + tests) and on-demand image build |

---

## Security notes

- Don't expose port 8000 directly to the internet. Use Tailscale, WireGuard, or
  a reverse proxy with HTTPS, and set `INCUBATOR_SESSION_SECURE=true`.
- The setup AP is open by default (no Wi-Fi password); access control lives in
  the operator account created during onboarding. The `/etc/incubator.env` file
  holds secrets (API keys, device secret) and is mode `600`.
