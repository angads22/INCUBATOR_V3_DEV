# Incubator v3 — Raspberry Pi Zero 2W Deployment Guide

## Hardware Requirements

| Component | Spec |
|-----------|------|
| Pi Zero 2W | RP3A0 quad-core 1 GHz, 512 MB RAM |
| DHT22 sensor | Temperature + humidity on GPIO4 |
| Heater relay | Active-LOW relay on GPIO17 |
| Fan relay | Active-LOW relay on GPIO27 |
| Turner motor | Stepper on GPIO22 (step) + GPIO23 (dir) |
| Candle LED | GPIO24 |
| Alarm / buzzer | GPIO25 |
| Lock relay | GPIO12 (fail-safe open) |
| Door relay | GPIO13 |
| Setup button | Momentary push-button on GPIO18 (pull-up) |
| Pi Camera | CSI ribbon — Camera Module v2 or v3 |

> **GPIO numbering**: All pin numbers use BCM (Broadcom) numbering, not physical pin numbers.

## GPIO Wiring Reference

```
Pi BCM  Physical  Function
──────  ────────  ─────────────────────────────
  4       7       DHT22 DATA
 12      32       Lock relay (IN1)
 13      33       Door relay (IN1)
 17      11       Heater relay (IN1)
 22      15       Turner step
 23      16       Turner direction
 24      18       Candle LED
 25      22       Alarm / buzzer
 27      13       Fan relay (IN1)
 18      12       Setup button (GND when pressed)
  -       1       3.3V → DHT22 VCC
  -       2/4     5V → Relay module VCC
  -       6/9/14  GND
```

Relay modules with active-LOW inputs (most 5V SRD-05VDC-SL-C modules):
- **Relay ON** = Pi drives pin LOW → closes relay → power to load
- **Relay OFF** = Pi drives pin HIGH (or hi-Z) → relay open → no power

## First-Time Setup

### 1. Flash OS

Flash **Raspberry Pi OS Lite (64-bit, Bookworm)** to a microSD card using Raspberry Pi Imager.  
Enable SSH and set hostname in the imager advanced settings.

### 2. Boot and SSH in

```bash
ssh pi@raspberrypi.local
```

### 3. Clone and run init script

```bash
git clone https://github.com/angads22/incubator_v3_dev /home/pi/incubator
sudo bash /home/pi/incubator/init_pi.sh /opt/incubator
```

This script:
- Installs system packages (`python3-picamera2`, `libgpiod2`, NetworkManager, etc.)
- Creates a Python venv with all dependencies
- Creates `/etc/incubator.env` with randomised AP password
- Installs and enables the `incubator` systemd service
- Enables the camera interface

### 4. First boot onboarding

On a fresh install the device has no WiFi config, so it **automatically broadcasts a WiFi hotspot**:

```
SSID:     Incubator-XXXX   (XXXX = last 4 chars of device ID)
Password: <shown at end of init_pi.sh output / in /etc/incubator.env>
IP:       http://10.42.0.1:8000
```

1. Connect your phone/laptop to the `Incubator-XXXX` network
2. Open **http://10.42.0.1:8000** in a browser
3. Complete the onboarding wizard:
   - Name your incubator
   - Select and connect to your home WiFi
   - (Optional) create an owner account
4. The Pi switches from AP to client mode automatically

### Re-entering setup mode

Hold the **setup button** (GPIO18) for **4 seconds**.  The AP restarts.

### 5. Normal operation

After onboarding the service is accessible on the local network:

```
http://<pi-ip>:8000
```

---

## Configuration

All settings are in `/etc/incubator.env`.  Edit and restart:

```bash
sudo nano /etc/incubator.env
sudo systemctl restart incubator
```

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `GPIO_MOCK` | `false` | Set `true` to run without hardware |
| `CAMERA_BACKEND` | `picamera2` | `picamera2` / `opencv` / `mock` |
| `VISION_BACKEND` | `mock` | `tflite` / `api` / `mock` |
| `VISION_API_URL` | *(empty)* | Remote vision model endpoint |
| `INCUBATOR_REQUIRE_LOGIN` | `false` | Enforce password login |
| `SENSOR_POLL_INTERVAL` | `30` | DHT22 poll interval (seconds) |

---

## Connecting a Vision Model

### Option A — TFLite (on-device, no internet)

1. Train a classification model (MobileNetV2 / EfficientLite) on egg images.
2. Convert to TFLite:
   ```bash
   tflite_convert --saved_model_dir=saved_model/ --output_file=model.tflite
   ```
3. Copy model and labels to the Pi:
   ```bash
   scp model.tflite pi@<pi-ip>:/var/incubator/models/vision/
   scp labels.txt   pi@<pi-ip>:/var/incubator/models/vision/
   ```
4. Update `/etc/incubator.env`:
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

Call `POST /api/vision/candle` (with optional `egg_id`).  The server:
1. Turns on the candle LED
2. Captures an image
3. Runs vision inference
4. Turns off the candle LED
5. Returns the result (and persists it to `model_results` table if `egg_id` is supplied)

---

## Logs and Diagnostics

```bash
# Live service logs
journalctl -u incubator -f

# Check service status
systemctl status incubator

# Health endpoint
curl http://localhost:8000/health

# Live sensor reading
curl http://localhost:8000/api/sensors/latest
```

---

## Updating

```bash
cd /home/pi/incubator
git pull
sudo bash init_pi.sh   # re-runs install; existing /etc/incubator.env is preserved
```

Or use the update script:

```bash
./scripts/update.sh
```

---

## Developer / Non-Pi Mode

Run on any Linux/macOS machine without Pi hardware:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
GPIO_MOCK=true CAMERA_BACKEND=mock VISION_BACKEND=mock ./scripts/start.sh
```

Open http://localhost:8000 — all hardware calls return simulated data.

---

## Security Notes

- **Do not expose port 8000 directly to the internet** without a reverse proxy + auth.
- Enable login: `INCUBATOR_REQUIRE_LOGIN=true` in `/etc/incubator.env`.
- Use Tailscale, WireGuard, or Cloudflare Tunnel for remote access.
- Set `INCUBATOR_SESSION_SECURE=true` when behind HTTPS.
