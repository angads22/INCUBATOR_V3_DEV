# Incubator v3 — a portable Linux egg-incubator appliance

Incubator v3 turns a small Linux board into a self-contained egg incubator
controller: it holds the temperature, turns the eggs on a schedule, watches
humidity, sounds an alarm when something is wrong, candles eggs with a camera,
and serves a phone-friendly web app on your home network.

It is built to behave like a **smart vacuum, not a computer**: power it on,
join the Wi-Fi it broadcasts, answer two questions on your phone, done. It
updates itself. It needs no keyboard, monitor, terminal or internet to set up.

The **Raspberry Pi Zero 2 W** is the reference board, and a ready-to-flash
SD-card image is published for it. The same software installs with one
command on **any Linux single-board computer** (Pi 3/4/5, Orange Pi, Rock Pi,
Banana Pi, Radxa, an old laptop…) that has Wi-Fi and a few GPIO pins.

---

## Contents

1. [What you get](#what-you-get)
2. [Supported hardware](#supported-hardware)
3. [60-second quickstart](#60-second-quickstart)
4. [First boot — what the farmer sees](#first-boot--what-the-farmer-sees)
5. [Wiring](#wiring)
6. [Sensors](#sensors)
7. [Cameras](#cameras)
8. [Configuration](#configuration)
9. [The web app](#the-web-app)
10. [Updates](#updates)
11. [Troubleshooting](#troubleshooting)
12. [Developer mode](#developer-mode)
13. [Building your own image](#building-your-own-image)
14. [Security notes](#security-notes)
15. [More documentation](#more-documentation)

---

## What you get

| | |
|---|---|
| **Thermostat** | Holds the setpoint (37.5 °C default) with hysteresis; fails **safe** — heater OFF — on any sensor doubt, latched or implausible readings, or heater runaway. |
| **Egg turner** | Stepper/relay turner on a schedule (every 3 h default); lockdown stops turning before hatch. Remembers the last turn across reboots. |
| **Humidity** | Monitors always; optional fan venting. Raises humidity target at lockdown. |
| **Alarms** | Buzzer + dashboard banner for over/under temperature, sensor loss, door open, heater-circuit faults (optional current sensor). |
| **Candling + vision** | Camera + candling LED; on-device TFLite or remote API model estimates fertility/stage; growth tracking per egg. |
| **Web app (PWA)** | Mobile-first dashboard, Controls, Status, Help; installable to the phone home screen; login once an account exists. |
| **Setup like an appliance** | Open setup Wi-Fi, captive-portal page, 2-step wizard, status LED + beeps, setup button, factory reset. |
| **Self-updating** | Checks GitHub Releases every 15 min, applies with a health check and automatic rollback. The control loop keeps running through updates. |
| **Fleet-ready** | Optional MQTT telemetry/commands and a hub (`hub/`). |

---

## Supported hardware

### Reference: Raspberry Pi Zero 2 W (flash the image)

512 MB RAM, 2.4 GHz Wi-Fi, 40-pin header. The published image is tuned for it
(zram swap, Bluetooth off, small journal, boot-safe GPIO levels). Any Pi with
Wi-Fi works the same way (Zero 2 W, 3A+/3B+, 4, 5, 400).

### Any Linux SBC (run the installer)

Requirements:

* A Linux distribution with **systemd** and **NetworkManager** available
  (Raspberry Pi OS, Debian, Ubuntu, Armbian, DietPi, Fedora, Arch, openSUSE…
  the installer installs NetworkManager itself).
* A Wi-Fi adapter whose driver supports **AP mode** (`iw list` → "Supported
  interface modes" → `* AP`). Almost all on-board SBC Wi-Fi and most USB
  adapters do.
* A **GPIO character device** (`/dev/gpiochip*`) — every mainline kernel has
  one; the app uses libgpiod. Legacy sysfs GPIO and RPi.GPIO also work.
* Python 3.10 or newer.

Tested install paths: Raspberry Pi OS (Bookworm, 32/64-bit), Ubuntu Server,
Armbian (Debian/Ubuntu based), Fedora/Arch/openSUSE/Alpine on a best-effort
package map. See [docs/PORTING.md](docs/PORTING.md) for the board checklist.

---

## 60-second quickstart

### Raspberry Pi — flash the image

1. Download the latest `incubator-v3-*.img.xz` from the project's
   **Releases** page (it is built by the "Build SD image" workflow).
2. Flash it with Raspberry Pi Imager (*Use custom*) or balenaEtcher. Do not
   unzip it; the tools read `.img.xz` directly. Skip Imager's OS-customisation
   prompt — the image configures itself.
3. Insert the card, connect the hardware (see [Wiring](#wiring)), power on.
4. Continue at [First boot](#first-boot--what-the-farmer-sees).

### Any board — one command

On a freshly installed Linux board, over SSH or a console:

```bash
curl -fsSL https://raw.githubusercontent.com/angads22/INCUBATOR_V3_DEV/main/install.sh | sudo bash
```

Or from a checkout: `sudo bash install.sh`. The installer:

* installs the packages for your distro (Python, NetworkManager, dnsmasq,
  avahi, libgpiod, i2c-tools, build tools) and makes NetworkManager own Wi-Fi;
* creates the Python environment and `/etc/incubator.env`;
* installs and enables the systemd services (web app, control loop,
  first-boot provisioning, OTA timer), the hardware watchdog, mDNS and the
  captive-portal DNS for the setup hotspot;
* on a Pi, adds the I²C/RTC overlay and the **boot-safe GPIO levels** to
  `config.txt` so the heater relay is OFF from the moment power is applied;
  on every other board an early-boot unit (`incubator-gpio-safe`) drives the
  relay lines OFF before anything else starts;
* applies SD-card-friendly tuning (`--no-tuning` to skip);
* prints the LED/beep legend and starts the app. Re-running it is safe.

Options: `--install-dir`, `--data-dir`, `--wifi-country CC`, `--ref <tag>`,
`--no-tuning`, `--skip-packages`. `sudo bash install.sh --help` lists them.

---

## First boot — what the farmer sees

1. **Power on.** The status LED blinks fast while the app starts; one short
   beep means it is ready (about a minute; two on the very first boot while
   the filesystem expands).
2. **The incubator broadcasts its own open Wi-Fi: `Incubator-XXXX`** (two short
   beeps, LED double-blink). On your phone, join it. A "sign in to network"
   sheet pops up — tap **Open setup in my browser**. (If it does not pop up,
   open `http://10.42.0.1:8000`.)
3. **Step 1 — your Wi-Fi.** Pick your home network from the list (or type it),
   enter its password.
4. **Step 2 — finish up.** Give the incubator a name. Optionally protect it
   with a login (recommended). Tap **Connect**.
5. The incubator joins your Wi-Fi (LED fast blink) and plays a rising
   short-short-long chime when it is on (LED slow heartbeat). **The setup page
   finds the incubator on your home network by itself** and opens it; tap
   **Open my incubator** and bookmark it or add it to your home screen.

**Wrong Wi-Fi password?** One long buzz and an S-O-S blink; the
`Incubator-XXXX` network comes back by itself and the wizard asks only for the
Wi-Fi again. Nothing else is lost.

**The setup button** (GPIO 18 by default):

| Hold for | What happens |
|---|---|
| 4 s | one medium beep → setup mode again (Wi-Fi hotspot comes back; use it to move the incubator to a new Wi-Fi) |
| 15 s | three rapid beeps = factory reset armed; **release** to confirm (one long beep). Wipes accounts, Wi-Fi and settings and returns to setup mode. Let go before 15 s to abort. |

### What the light and the beeper mean

| Event | Buzzer | Status LED |
|---|---|---|
| Power on, app ready | 1 short | fast blink → then the pattern below |
| Setup mode (own Wi-Fi up) | 2 short | double-blink (pairing) |
| Joining your Wi-Fi | – | fast blink |
| Connected | short-short-long (rising) | slow heartbeat |
| Couldn't join / offline | 1 long | SOS |
| Setup button registered (4 s) | 1 medium | – |
| Factory-reset armed (15 s) | 3 rapid, then 1 long on release | fast blink |

The status LED is the board's own activity LED (`ACT` on a Pi) by default;
`STATUS_LED=gpio:<pin>` uses an external LED instead. Alarms keep sounding
regardless of these cues. `INCUBATOR_SOUND_FEEDBACK=false` silences the cues.

Finding the incubator later: `http://<name>.local:8000` (the name you gave it,
e.g. `http://incubator-a3f2.local:8000`), the same hostname in your router's
device list, or its IP address — all three are shown on the Status page and in
Settings → Device.

---

## Wiring

Default pins (BCM numbering on a Pi — the numbers printed on most breakout
boards, **not** the physical header positions). On another board, pick any
free GPIO lines and set the `GPIO_*_PIN` keys in `/etc/incubator.env` — a pin
may be a number, a line name (`PA12`, `GPIO17`) or `gpiochip1:5`.

| Function | Default (BCM) | Pi header pin | Notes |
|---|---|---|---|
| DHT22 data | GPIO 4 | 7 | only when using the DHT backend; I²C sensors use SDA/SCL instead |
| Heater relay IN | GPIO 17 | 11 | **active-LOW** relay module |
| Fan relay IN | GPIO 27 | 13 | optional |
| Turner step | GPIO 22 | 15 | optional stepper/relay turner |
| Turner direction | GPIO 23 | 16 | |
| Candling LED | GPIO 24 | 18 | via ~220 Ω |
| Buzzer / alarm | GPIO 25 | 22 | active buzzer, 3–5 V |
| Lock relay | GPIO 12 | 32 | optional, fail-safe open |
| Door relay | GPIO 13 | 33 | optional |
| Setup button | GPIO 18 | 12 | momentary, to GND (internal pull-up) |
| I²C SDA / SCL | GPIO 2 / 3 | 3 / 5 | I²C sensors, DS3231 RTC, INA219 current sensor |
| 3V3 / 5V / GND | – | 1 / 2 / 6 | sensor 3V3; relay board 5 V |

Relay modules with active-LOW inputs (most 5 V SRD-05VDC-SL-C modules):
pin LOW = relay ON = power to the load; pin HIGH or floating = OFF. The Pi
image drives the relay pins to their OFF level **from power-on, before the
kernel loads** (`gpio=12,13,17,27=op,dh` in `config.txt`), the control loop
re-asserts them every cycle, and fails safe on any doubt.

> **Not negotiable for a real hatch:** put an independent thermal cutoff
> (bimetal limit switch + thermal fuse) in series with the heater. Software
> cannot open a welded relay. See
> [docs/HARDWARE_SAFETY_ADDENDUM.md](docs/HARDWARE_SAFETY_ADDENDUM.md).

Parts list and a step-by-step build: [docs/BUILD_QUICKSTART.md](docs/BUILD_QUICKSTART.md).

---

## Sensors

`SENSOR_BACKEND=auto` (the default) tries, in order:

| Backend | Sensor | How |
|---|---|---|
| `iio` | DHT11/DHT22 via the kernel driver, or any Linux IIO temp+humidity device | On a Pi add `dtoverlay=dht11,gpiopin=4` to `config.txt`; the app reads `/sys/bus/iio/devices/iio:device*`. Works on every board with a device-tree overlay for the sensor. |
| `sht3x` | Sensirion SHT30/31/35 (0x44/0x45) | I²C, user-space driver |
| `aht20` | AHT20 / AHT10 (0x38) | I²C |
| `bme280` | Bosch BME280 (temp + humidity) / BMP280 (temp only) (0x76/0x77) | I²C |
| `htu21d` | HTU21D / SI7021 (0x40) | I²C |
| `dht` | DHT22 on `GPIO_DHT_PIN` via the legacy adafruit driver | Pi only (`pip install -e ".[pi]"`) |
| `mock` / `none` | fixed test values / no sensor | development / sensor-less boards |

I²C sensors are the recommended choice on non-Pi boards: they need no
bit-banging, no kernel overlay, and the same two wires also carry the DS3231
RTC and the optional INA219 heater-current sensor. `SENSOR_I2C_BUS` and
`SENSOR_I2C_ADDR` pin a bus/address when auto-detection is ambiguous.

---

## Cameras

`CAMERA_BACKEND=auto`: the Raspberry Pi camera via **picamera2** when it is
installed (`sudo apt install python3-picamera2`), otherwise any **USB webcam**
via OpenCV (`/dev/video0`; `pip install opencv-python-headless`), otherwise a
mock. The dashboard shows a low-rate live preview; candling captures a still,
lights the candling LED and runs the vision model. Captures are stored under
`/var/incubator/captures` and pruned automatically before the card fills up.

---

## Configuration

Everything lives in **`/etc/incubator.env`** (created from
[`deploy/incubator.env.example`](deploy/incubator.env.example) on first
install, never overwritten). Edit it and run
`sudo systemctl restart incubator incubator-control`.

Highlights:

| Key | Default | Meaning |
|---|---|---|
| `GPIO_BACKEND` | `auto` | `gpiod` (libgpiod, any board) → `rpi` (RPi.GPIO) → `sysfs` → `mock` |
| `GPIO_CHIP` | *(blank)* | chip for bare pin numbers, e.g. `gpiochip1`; blank = resolve by Pi line names, else `gpiochip0` |
| `GPIO_*_PIN` | BCM defaults | pin per function; number, line name or `chip:offset` |
| `GPIO_RELAY_ACTIVE_LOW` | `true` | relay module polarity |
| `SENSOR_BACKEND` | `auto` | see [Sensors](#sensors); plus `SENSOR_I2C_BUS`, `SENSOR_I2C_ADDR`, `SENSOR_IIO_DEVICE` |
| `CAMERA_BACKEND` | `auto` | `picamera2` / `opencv` / `mock` |
| `STATUS_LED` | `auto` | board LED name under `/sys/class/leds`, `gpio:<pin>`, or `none` |
| `INCUBATOR_SOUND_FEEDBACK` | `true` | beeps for boot/setup/connect/fail |
| `WIFI_IFACE` | *(blank)* | Wi-Fi interface; blank = first wireless interface found |
| `INCUBATOR_WIFI_COUNTRY` | `US` | regulatory country (unblocks the radio) |
| `INCUBATOR_SETUP_BUTTON_HOLD_SECONDS` / `INCUBATOR_FACTORY_RESET_HOLD_SECONDS` | `4` / `15` | button hold times |
| `CONTROL_DAEMON_ENABLED` | `true` | the control loop runs as its own service and owns heater/fan/turner/sensor |
| `CONTROL_HYSTERESIS_C`, `TURN_INTERVAL_HOURS`, `HUMIDITY_CONTROL_MODE` | `0.4`, `3`, `off` | control tuning |
| `CAMERA_IMAGE_DIR`, `CAPTURE_*` | `/var/incubator/captures`… | photo storage + auto-prune |
| `VISION_BACKEND`, `VISION_TFLITE_MODEL`, `VISION_API_URL` | `auto`… | vision model source |
| `OTA_ENABLED` | `true` | automatic updates from GitHub Releases |
| `INCUBATOR_REQUIRE_LOGIN` | `false` | login is enforced automatically once an account exists |
| `GPIO_MOCK`, `INCUBATOR_WIFI_MOCK`, `INCUBATOR_NMCLI_BIN` | `false`, `false`, *(blank)* | dev-laptop knobs: mock GPIO, simulate Wi-Fi without NetworkManager, nmcli path override |
| `MQTT_*` | disabled | fleet telemetry bus |

Temperature/humidity **targets** are set in the web app (Settings), not in the
file. Run `/opt/incubator/.venv/bin/python -m app.hal` on the device to print
what board, GPIO chip, sensor, LED and Wi-Fi interface were detected.

---

## The web app

Open `http://<name>.local:8000` on a phone or laptop on the same network. It
is a progressive web app: "Add to Home Screen" gives you an app icon.

| Tab | What it is for |
|---|---|
| **Home** (`/`) | The big ring: current temperature, humidity, what the incubator is doing right now and what you should do next. Live camera, quick actions, recent events. |
| **Controls** (`/hardware`) | Manual actions: turn eggs now, candle (LED + photo), test the alarm, lock/door relays, heater/fan state (read-only while the control loop owns them). |
| **Status** (`/status`) | Health chips: sensor, control loop, Wi-Fi, board/GPIO/sensor backend, version, addresses; alarm history. |
| **Help** (`/help`) | What the lights and beeps mean, the setup button, finding the address, troubleshooting, candling. |
| Profile menu → **Settings** | Targets (temperature, humidity, turn interval), device name, Wi-Fi/network, account + linked sign-ins, **Device** card (board info, "Restart setup mode", "Factory reset"). |
| Profile menu → **Vision lab** (`/testing`) | Try the candling/vision model on saved or uploaded photos, record labelled results. |
| Profile menu → **Setup / change Wi-Fi** (`/onboarding`) | Move the incubator to a different Wi-Fi: the device switches to its own `Incubator-XXXX` network and the wizard runs again. |

Everything also exists as JSON under `/api/...` (`/docs` has the OpenAPI UI):
`/health`, `/api/sensors/latest`, `/api/network/status`, `/api/device/info`,
`/api/vision/*`, `/api/captures/*`, `/hardware/send`.

---

## Updates

The incubator **updates itself**: a timer checks GitHub Releases ten minutes
after boot and every 15 minutes, applies a newer release, restarts only the
web service, checks `/health` and the control-loop heartbeat, and rolls back
automatically if the new version is unhealthy. **The control loop is never
restarted by an update**, so incubation continues throughout. Set
`OTA_ENABLED=false` to pin a version.

Manual update from a shell: `sudo /opt/incubator/scripts/update.sh`
(works on an appliance checkout with no upstream branch). Re-running
`sudo bash install.sh` is also safe at any time.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| No `Incubator-XXXX` Wi-Fi after power-on | Wait 2 minutes on a first boot. LED SOS + long buzz means it tried to join a saved Wi-Fi and failed — the hotspot returns on its own. Still nothing: hold the setup button 4 s. On a non-Pi board run `iw list` and confirm `* AP` under supported interface modes, and `rfkill list` (country not set → `INCUBATOR_WIFI_COUNTRY`). |
| The "sign in to network" page does not pop up | Open `http://10.42.0.1:8000` manually. Some phones need mobile data switched off while on the setup Wi-Fi. |
| Wizard says "Couldn't join" | Almost always the Wi-Fi password. 5 GHz-only networks are invisible to a Pi Zero 2 W (2.4 GHz only). Hidden SSIDs: type the name. |
| Can't find the incubator after setup | Try `http://<name>.local:8000`, then the IP from your router's device list (Android often cannot resolve `.local`). The wizard shows both; so does Settings → Device. |
| Dashboard shows "no sensor" / heater stays off | Fail-safe is working. Check wiring and `SENSOR_BACKEND`; run `sudo /opt/incubator/.venv/bin/python -m app.hal` and `i2cdetect -y 1`. The heater only runs with a plausible, changing reading. |
| Alarm keeps sounding | The Status page says why. Silence from Home; the cause must be fixed for it to clear. |
| Relay clicks ON at power-up on a non-Pi board | Add the board's equivalent of the Pi's boot-time GPIO levels (device-tree `gpio-hog`, or a pull-up resistor on the relay input). See docs/PORTING.md. |
| Logs | `journalctl -u incubator -u incubator-control -f`; health: `curl http://localhost:8000/health` |
| Start over | Hold the setup button 15 s (factory reset), or Settings → Device → Factory reset. |

---

## Developer mode

Runs on any Linux/macOS machine with no hardware — GPIO, sensor, camera and
Wi-Fi are mocked.

```bash
git clone https://github.com/angads22/INCUBATOR_V3_DEV
cd INCUBATOR_V3_DEV
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
GPIO_MOCK=true CAMERA_BACKEND=mock VISION_BACKEND=mock ./scripts/start.sh   # http://localhost:8000
pytest -q
```

`INCUBATOR_BUTTON_MOCK_FILE=/tmp/btn` lets you simulate the setup button
(`echo 1 > /tmp/btn` to press). Extras: `[hardware]` (libgpiod 2 + smbus2),
`[pi]` (RPi.GPIO + adafruit DHT), `[tflite]`, `[dev]`.

Layout: `app/` (FastAPI app, `app/hal/` hardware abstraction, `app/control/`
the control loop, `app/services/`, `app/ota/`), `deploy/` (systemd units, env
example, tmpfiles, avahi), `scripts/` (firstboot, update, OTA agent),
`install.sh`, `build_image.sh`, `rpi-image/` (rpi-image-gen layer), `hub/`
(fleet hub), `tests/`.

---

## Building your own image

```bash
sudo ./build_image.sh                       # Raspberry Pi OS Lite (pinned) + the app → dist/*.img.xz
sudo ./build_image.sh --wifi-country GB --hostname coop --user pi --password secret
sudo ./build_image.sh --base Armbian_xx_Orangepizero2_bookworm.img.xz   # single-partition base
```

The builder loop-mounts the base image, runs `install.sh` inside it (via
QEMU on x86 hosts, natively on ARM), enables SSH, then **verifies the final
bytes** against a zero-tolerance checklist (services enabled, open setup AP,
control daemon on, no pre-burned identity, and on a Pi the PARTUUID boot
config and boot-safe GPIO lines). Pi-specific steps run only when the base
image is a Pi image. GitHub Actions builds the same image on a native arm64
runner and publishes it as a release asset
([docs/BUILD_WINDOWS.md](docs/BUILD_WINDOWS.md) if you are on Windows).

---

## Security notes

* The setup network is open on purpose and exists only while the device is
  unconfigured (or when you ask for it); security lives in the **account
  login**, which is enforced automatically as soon as an account exists.
* Passwords are stored as PBKDF2-HMAC-SHA256 hashes; sessions are random
  tokens stored as SHA-256 hashes; Wi-Fi passwords never reach the browser's
  storage.
* Do **not** expose port 8000 to the internet directly; use Tailscale,
  WireGuard or a reverse proxy with HTTPS (`INCUBATOR_SESSION_SECURE=true`).
* Updates are fetched from GitHub Releases over HTTPS and health-checked;
  set `OTA_ENABLED=false` on an air-gapped unit.
* The hardware cannot be made safe by software alone: fit the thermal cutoff.

---

## More documentation

* [docs/BUILD_QUICKSTART.md](docs/BUILD_QUICKSTART.md) — parts, wiring and the first-boot walk-through
* [docs/PI_DEPLOY.md](docs/PI_DEPLOY.md) — deployment guide (Raspberry Pi and other boards), vision models, logs
* [docs/PORTING.md](docs/PORTING.md) — bringing up a new board step by step
* [docs/HARDWARE_SAFETY_ADDENDUM.md](docs/HARDWARE_SAFETY_ADDENDUM.md) — thermal cutoff, RTC, heater-current sensing
* [docs/BUILD_WINDOWS.md](docs/BUILD_WINDOWS.md) — building/flashing the image from Windows
