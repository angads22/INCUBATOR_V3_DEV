# Incubator v3 — Raspberry Pi Zero 2W

A local-first egg-incubator controller for the **Raspberry Pi Zero 2W**. It runs
a FastAPI web app that drives the incubator hardware over GPIO (heater, fan, egg
turner, candling LED, door/lock relays, DHT22 sensor, Pi Camera) and serves an
operator dashboard on the local network.

The device is an appliance: flash the SD card, power on, and finish setup from
your phone over a Wi-Fi hotspot — **no keyboard, monitor, or internet required**.

---

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

### First boot (account creation + user auth)

1. Join that network and open **http://10.42.0.1:8000**.
2. The setup wizard walks you through:
   - selecting your home Wi-Fi network,
   - naming the device,
   - **creating an operator account** (username + email + password).
3. After setup the Pi joins your Wi-Fi. From then on the dashboard **requires
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

## Routes

**Web UI:** `/` (dashboard), `/status`, `/settings`, `/hardware`, `/help`,
`/onboarding`, `/login`

**API:** `GET /health`, `GET /setup/status`, `POST /setup/complete`,
`POST /api/login`, `POST /api/logout`, `POST /api/settings`,
`POST /hardware/send`, `GET /api/sensors/latest`, `GET /docs`

---
