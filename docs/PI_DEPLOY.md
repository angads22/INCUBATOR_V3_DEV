# Deployment guide (Raspberry Pi and other boards)

The Raspberry Pi Zero 2 W is the reference build and the one the published
SD-card image targets. Everything in this guide also applies to any other
Linux single-board computer through `install.sh`; the board-specific parts
are called out, and the [Other boards](#other-boards) section lists what
changes. For a brand-new board start with [PORTING.md](PORTING.md).

## Hardware Requirements (reference build)

| Component | Spec |
|-----------|------|
| Pi Zero 2W | RP3A0 quad-core 1 GHz, 512 MB RAM |
| Temp/humidity sensor | DHT22 on GPIO4 **or** an I²C sensor (SHT3x / AHT20 / BME280 / HTU21D) on SDA/SCL |
| Heater relay | Active-LOW relay on GPIO17 |
| Fan relay | Active-LOW relay on GPIO27 |
| Turner motor | Stepper on GPIO22 (step) + GPIO23 (dir) |
| Candle LED | GPIO24 |
| Alarm / buzzer | GPIO25 |
| Lock relay | GPIO12 (fail-safe open) |
| Door relay | GPIO13 |
| Setup button | Momentary push-button on GPIO18 (pull-up) |
| Pi Camera | CSI ribbon — Camera Module v2 or v3 (or a USB webcam) |
| Optional | DS3231 RTC and INA219 current sensor on I²C; independent thermal cutoff in the heater leg (see HARDWARE_SAFETY_ADDENDUM.md) |

> **GPIO numbering**: All pin numbers use BCM (Broadcom) numbering, not physical pin numbers.

## GPIO Wiring Reference

```
Pi BCM  Physical  Function
──────  ────────  ─────────────────────────────
  4       7       DHT22 DATA (DHT backend only)
  2/3     3/5     I²C SDA/SCL (I²C sensors, RTC, INA219)
 12      32       Lock relay (IN1)
 13      33       Door relay (IN1)
 17      11       Heater relay (IN1)
 22      15       Turner step
 23      16       Turner direction
 24      18       Candle LED
 25      22       Alarm / buzzer
 27      13       Fan relay (IN1)
 18      12       Setup button (GND when pressed)
  -       1       3.3V → sensor VCC
  -       2/4     5V → Relay module VCC
  -       6/9/14  GND
```

Relay modules with active-LOW inputs (most 5V SRD-05VDC-SL-C modules):
- **Relay ON** = Pi drives pin LOW → closes relay → power to load
- **Relay OFF** = Pi drives pin HIGH (or hi-Z) → relay open → no power

The installer writes `gpio=12,13,17,27=op,dh` and `gpio=22,23,24,25=op,dl`
to `config.txt` so the firmware holds every relay OFF from power-on, before
the kernel even loads.

## First-Time Setup

Choose **one** of two installation paths. Both end at the same first-boot
onboarding flow.

### Option A — Flash the ready-to-run image (recommended)

Download `incubator-v3-<version>-<date>.img.xz` from the GitHub **Releases**
page (built by the "Build SD image" workflow), or build it yourself on a Linux
host:

```bash
git clone https://github.com/angads22/INCUBATOR_V3_DEV
cd INCUBATOR_V3_DEV
sudo ./build_image.sh
```

Flash it with Raspberry Pi Imager (*Use custom*), balenaEtcher, or `dd`, then
insert the card and power on. No internet is needed at first boot. See
`./build_image.sh --help` for options (custom base image, hostname, Wi-Fi
country, SSH user).

### Option B — Install on an existing Linux

1. Flash **Raspberry Pi OS Lite (64-bit, Bookworm)** with Raspberry Pi Imager;
   enable SSH in the advanced settings. (Any other Debian/Ubuntu/Armbian/
   Fedora/Arch image works the same way.)
2. SSH in: `ssh pi@raspberrypi.local`
3. Run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/angads22/INCUBATOR_V3_DEV/main/install.sh | sudo bash
```

or from a checkout: `sudo bash install.sh` (`init_pi.sh` still works and
calls the same script). `install.sh`:

- Installs system packages (NetworkManager, dnsmasq, avahi, libgpiod, i2c-tools,
  Python, build tools; `python3-picamera2` on a Pi)
- Makes NetworkManager own Wi-Fi (hotspot-capable), sets the regulatory country
- Creates a Python venv and installs the app with the hardware extras
- Creates `/etc/incubator.env` with an **open** setup AP (no Wi-Fi password)
- Installs and enables `incubator`, `incubator-control`, `incubator-firstboot`
  and the `incubator-ota` timer; configures the hardware watchdog and mDNS
- On a Pi: I²C + DS3231 overlay, boot-safe GPIO levels, zram, Bluetooth off
- Re-running it is safe; your `/etc/incubator.env` is kept.

### First boot onboarding (both options)

On a fresh install the device has no Wi-Fi config, so it **automatically
broadcasts an open Wi-Fi hotspot**:

```
SSID:     Incubator-XXXX   (XXXX = last 4 chars of device ID; no password)
URL:      http://10.42.0.1:8000  (opens by itself via the captive portal)
```

1. Join `Incubator-XXXX` on your phone; tap **Open setup in my browser** on the
   sheet that pops up (or open the URL above).
2. **Step 1:** pick your home Wi-Fi and enter its password.
3. **Step 2:** name the incubator; optionally create a login (recommended).
4. The device joins your Wi-Fi (rising chime, LED heartbeat) and the setup
   page **finds it on your network by itself** and opens it. If the join
   fails (long buzz, LED SOS) the hotspot comes back and the wizard asks only
   for the Wi-Fi again.

Once an account exists, the dashboard requires login. Sign in at `/login`
with your username (or email) and password.

### Re-entering setup mode / factory reset

Hold the **setup button** (GPIO18) for **4 seconds** (one beep) → the
hotspot returns and the wizard runs again (use this to move to a new Wi-Fi;
also available from the profile menu → *Setup / change Wi-Fi*). Hold for
**15 seconds** (three rapid beeps, release to confirm) → **factory reset**:
accounts, Wi-Fi and settings are wiped and the device returns to setup mode.
Also available from Settings → Device.

### Normal operation

After onboarding the service is accessible on the local network:

```
http://<name>.local:8000      e.g. http://incubator-a3f2.local:8000
http://<pi-ip>:8000           (Android phones often cannot resolve .local)
```

---

## Configuration

All settings are in `/etc/incubator.env`.  Edit and restart:

```bash
sudo nano /etc/incubator.env
sudo systemctl restart incubator incubator-control
```

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `GPIO_BACKEND` | `auto` | `gpiod` / `rpi` / `sysfs` / `mock` |
| `GPIO_CHIP` | *(blank)* | GPIO chip for bare pin numbers (`gpiochip0`…) |
| `GPIO_*_PIN` | BCM defaults | per-function pins; number, line name or `chip:offset` |
| `GPIO_MOCK` | `false` | Set `true` to run without hardware |
| `SENSOR_BACKEND` | `auto` | `iio` / `sht3x` / `aht20` / `bme280` / `htu21d` / `dht` / `mock` / `none` |
| `CAMERA_BACKEND` | `auto` | `picamera2` / `opencv` / `mock` |
| `STATUS_LED` | `auto` | board LED name, `gpio:<pin>` or `none` |
| `WIFI_IFACE` | *(blank)* | Wi-Fi interface (blank = auto) |
| `INCUBATOR_WIFI_COUNTRY` | `US` | regulatory country; unblocks the radio |
| `CONTROL_DAEMON_ENABLED` | `true` | control loop as its own service (owns heater/fan/turner/sensor) |
| `VISION_BACKEND` | `auto` | `tflite` / `api` / `mock` |
| `VISION_API_URL` | *(empty)* | Remote vision model endpoint |
| `INCUBATOR_REQUIRE_LOGIN` | `false` | Force login even before an account exists (login is auto-enforced once one does) |
| `SENSOR_POLL_INTERVAL` | `30` | Sensor poll interval (seconds) |
| `OTA_ENABLED` | `true` | automatic updates from GitHub Releases |

Temperature/humidity targets and the turn interval are set in the web app
(**Settings**). Manual hardware actions live in the **Controls** tab.

---

## Connecting a Vision Model

### Option A — TFLite (on-device, no internet)

1. Train a classification model (MobileNetV2 / EfficientLite) on egg images.
2. Convert to TFLite:
   ```bash
   tflite_convert --saved_model_dir=saved_model/ --output_file=model.tflite
   ```
3. Copy model and labels to the device:
   ```bash
   scp model.tflite pi@<pi-ip>:/var/incubator/models/vision/
   scp labels.txt   pi@<pi-ip>:/var/incubator/models/vision/
   ```
4. Update `/etc/incubator.env` (or just drop the file in — `VISION_BACKEND=auto`
   picks it up):
   ```
   VISION_BACKEND=tflite
   VISION_TFLITE_MODEL=/var/incubator/models/vision/model.tflite
   ```

### Option B — Remote API (cloud vision, GPT-4V, Claude Vision, Roboflow, etc.)

1. Deploy or use an existing vision API that accepts:
   ```json
   {"image_b64": "<base64-jpeg>", "mode": "egg"}
   ```
   and returns:
   ```json
   {"label": "fertile", "confidence": 0.94, "details": {}}
   ```
2. Update `/etc/incubator.env`:
   ```
   VISION_BACKEND=api
   VISION_API_URL=https://your-server/analyze
   VISION_API_KEY=your-key
   ```

### Candling workflow

Call `POST /api/vision/candle` (with optional `egg_id`), or tap **Candle** in
the Controls tab.  The server:
1. Turns on the candle LED
2. Captures an image
3. Runs vision inference
4. Turns off the candle LED
5. Returns the result (and persists it to `model_results` table if `egg_id` is supplied)

---

## Logs and Diagnostics

```bash
# Live service logs (web app + control loop)
journalctl -u incubator -u incubator-control -f

# Check service status
systemctl status incubator incubator-control incubator-ota.timer

# Health endpoint (includes board / gpio_backend / sensor_backend)
curl http://localhost:8000/health

# Live sensor reading
curl http://localhost:8000/api/sensors/latest

# Hardware detection report
sudo /opt/incubator/.venv/bin/python -m app.hal
```

---

## Updating

Updates are **automatic**: `incubator-ota.timer` checks GitHub Releases 10 min
after boot and every 15 min, applies a newer release, restarts only the web
service, verifies `/health` and the control-loop heartbeat, and rolls back on
failure. The control loop is never restarted by an update. Set
`OTA_ENABLED=false` in `/etc/incubator.env` to pin a version.

Manual update:

```bash
sudo /opt/incubator/scripts/update.sh      # fetch newest code (works without an upstream branch), reinstall, restart
# or, from a checkout:
sudo bash install.sh                        # re-runs the installer; /etc/incubator.env is preserved
```

---

## Other boards

Everything above applies, with these differences:

| Topic | Raspberry Pi | Other SBC |
|---|---|---|
| Install | image or `install.sh` | `install.sh` (`curl … | sudo bash`), or `build_image.sh --base <vendor image>` |
| Pin numbers | BCM numbers resolve by line name on every Pi model | line offsets on `gpiochip0`, or `GPIO_CHIP=…`, or names like `PA12` / `gpiochip1:5` |
| Boot-time relay state | `config.txt` `gpio=` lines (installer) | pull-up on the relay input or a device-tree `gpio-hog` |
| Sensor | DHT22 (`dht` or kernel `dht11` overlay) or I²C | I²C recommended; DHT via a `dht11` device-tree overlay (IIO) |
| Camera | picamera2 | USB webcam via OpenCV (`/dev/video0`) |
| Status LED | `ACT` | board LED from `/sys/class/leds` (`STATUS_LED=…`) or `gpio:<pin>` |
| Wi-Fi | on-board, AP capable | check `iw list` for `* AP`; USB adapter if not |
| Root-fs expansion | `raspi-config` | `growpart` + `resize2fs` (ext4), automatic if installed |
| Tuning | zram, Bluetooth off, `config.txt` cosmetics | journald cap, `noatime`, no wait-online; Armbian I²C overlay |

Step-by-step bring-up: [PORTING.md](PORTING.md).

---

## Developer / Non-Pi Mode

Run on any Linux/macOS machine without hardware:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
GPIO_MOCK=true CAMERA_BACKEND=mock VISION_BACKEND=mock ./scripts/start.sh
```

Open http://localhost:8000 — all hardware calls return simulated data.

---

## Security Notes

- The setup hotspot is open by design and only exists while the device is
  unconfigured (or when you re-enter setup mode); security is the account login,
  enforced automatically once an account exists.
- **Do not expose port 8000 directly to the internet** without a reverse proxy + auth.
- Force login before an account exists: `INCUBATOR_REQUIRE_LOGIN=true` in `/etc/incubator.env`.
- Use Tailscale, WireGuard, or Cloudflare Tunnel for remote access.
- Set `INCUBATOR_SESSION_SECURE=true` when behind HTTPS.
