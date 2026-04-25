# ESP32-CAM Firmware — Incubator V3

C++ firmware for the AI-Thinker ESP32-CAM module.  Built with PlatformIO.

---

## Features

| Feature | Detail |
|---------|--------|
| WiFi onboarding | Boots as `IncubatorSetup` hotspot; captive-portal web page lets you pick your home WiFi and name the device. Credentials saved to NVS and used on every subsequent boot. |
| JSON UART protocol | Listens on Serial (115 200 baud) for `{"action":"…","value":…}` commands from the Python backend and responds `{"ok":true,"value":…}`. |
| DHT22 | Temperature (`read_temp`) and humidity (`read_humidity`). |
| Relays | Heater (`set_candle`/`set_heater`), fan (`set_fan`), lock (`open_lock`/`close_lock`), door (`open_door`/`close_door`). |
| Egg turner | `move_motor` / `"turn_cycle"` drives a stepper motor. |
| Camera | `capture_image` — grabs an OV2640 JPEG and sends it as a binary frame over Serial. |
| WiFi reset | Send `{"action":"reset_wifi"}` over UART to wipe NVS and reboot into setup mode. |

---

## Hardware

**Board:** AI-Thinker ESP32-CAM (ESP32-S module + OV2640)

### GPIO assignments

| Signal | GPIO | Notes |
|--------|------|-------|
| DHT22 data | 13 | |
| Heater relay | 14 | Active-LOW |
| Fan relay | 15 | Active-LOW |
| Motor STEP | 12 | Boot must be LOW — verify your module's eFuse |
| Motor DIR | -1 | Disabled by default; set `PIN_MOTOR_DIR` in `config.h` |
| Lock relay | 2 | Also drives the on-board LED |
| Candle flash | 4 | On-board white LED |

All pin assignments are in `src/config.h`.  Adjust to match your wiring.

> **GPIO12 warning** — on modules where the MTDI eFuse selects 3.3 V flash
> voltage, GPIO12 **must** be LOW at boot.  A pull-down resistor (10 kΩ) on
> the STEP line is recommended.

---

## Build & Flash

### Prerequisites

```bash
pip install platformio
# or install the PlatformIO IDE extension for VS Code
```

### Build

```bash
cd firmware/esp32_cam
pio run -e esp32cam
```

### Flash

Put the ESP32-CAM into **bootloader mode** before flashing:

1. Connect `IO0` to `GND`
2. Power-cycle (press RST or disconnect/reconnect USB)
3. Flash:

```bash
pio run -e esp32cam --target upload
```

4. Disconnect `IO0` from `GND` and press RST to boot normally.

### Monitor

```bash
pio device monitor --baud 115200
```

---

## First-boot onboarding

1. Power the board — it starts a hotspot: **SSID `IncubatorSetup`  Password `incubator`**
2. Connect your phone or laptop to that hotspot.
3. A "Sign in to network" notification should appear (captive portal).
   If not, open a browser and navigate to **http://192.168.4.1**
4. The setup page scans nearby WiFi networks.  Select yours, enter the
   password, give the device a name, and tap **Save & Connect**.
5. The ESP32-CAM saves the credentials and reboots into station mode.
6. The Python backend connects to the device over the USB serial cable as
   usual — WiFi is used for future OTA updates.

### Reset WiFi credentials

Either:
- Send `{"action":"reset_wifi"}` over the serial connection, **or**
- Hold GPIO0 LOW for 5 seconds at boot (if you wire a button there)

The device will clear NVS and reboot into hotspot mode.

---

## UART Protocol reference

All messages are UTF-8 newline-terminated JSON.

### Commands

| action | value | Response |
|--------|-------|----------|
| `read_temp` | — | `{"ok":true,"value":37.50}` |
| `read_humidity` | — | `{"ok":true,"value":55.20}` |
| `set_heater` / `set_candle` | `"on"` / `"off"` | `{"ok":true}` |
| `set_fan` | `"on"` / `"off"` | `{"ok":true}` |
| `open_lock` | — | `{"ok":true}` |
| `close_lock` | — | `{"ok":true}` |
| `open_door` | — | `{"ok":true}` |
| `close_door` | — | `{"ok":true}` |
| `move_motor` | `"turn_cycle"` or step count | `{"ok":true}` |
| `capture_image` | — | JSON line + 4-byte LE length + JPEG bytes |
| `ping` | — | `{"ok":true,"value":"pong"}` |
| `reset_wifi` | — | reboots into AP mode |

### Error response

```json
{"ok": false, "error": "description"}
```

### Camera binary frame

After the JSON line for `capture_image` the firmware immediately writes:

```
[uint32 LE JPEG length][raw JPEG bytes]
```

The Python `CameraService` reads and saves these bytes automatically.
