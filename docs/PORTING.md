# Porting Incubator v3 to a new board

The app has no hard dependency on Raspberry Pi hardware: GPIO goes through
libgpiod (or sysfs), sensors through Linux IIO / I²C / a legacy DHT driver,
the status LED through `/sys/class/leds`, Wi-Fi through NetworkManager, and
the board is detected from the device tree. Bringing up a new board is a
checklist, not a code change. Work through it top to bottom.

## 0. Pick a distro

Anything with **systemd + NetworkManager** available. Known-good: Raspberry Pi
OS, Debian, Ubuntu Server, Armbian, DietPi. Fedora, Arch, openSUSE and Alpine
are covered by `install.sh`'s package maps on a best-effort basis (Alpine
needs OpenRC-less systemd images to use the units; otherwise run the app
manually). Python ≥ 3.10.

Flash the vendor image, boot it once, get a shell (SSH or serial), then:

```bash
curl -fsSL https://raw.githubusercontent.com/angads22/INCUBATOR_V3_DEV/main/install.sh | sudo bash
```

Everything below is about making the detected hardware match the real
hardware. Start with the detection report:

```bash
sudo /opt/incubator/.venv/bin/python -m app.hal
```

It prints the board model, distro, GPIO chips, I²C buses, the GPIO backend it
would pick, the sensor it found, the LED it will drive and the Wi-Fi
interface + AP capability.

## 1. Wi-Fi: NetworkManager must own the radio, and it must do AP mode

```bash
nmcli device status            # the wlan device must be "disconnected"/"connected", not "unmanaged"
iw list | sed -n '/Supported interface modes/,/^$/p'   # must list "* AP"
rfkill list                    # nothing "Soft blocked: yes"
```

* **unmanaged** → another daemon owns Wi-Fi. The installer already wrote
  `/etc/NetworkManager/conf.d/10-incubator.conf` (`unmanaged-devices=none`)
  and, on Ubuntu, `/etc/netplan/90-incubator-nm.yaml` (`renderer:
  NetworkManager`); run `sudo netplan apply` once, or disable
  `wpa_supplicant@wlan0`, `iwd`, `connman` or `dhcpcd` if your image runs one
  of those standalone.
* **no `* AP`** → the driver cannot host the setup hotspot. Options: a cheap
  USB Wi-Fi adapter that does (most RTL8188/8192, MT7601U, AR9271); or skip
  the hotspot and configure Wi-Fi with `nmcli dev wifi connect <ssid>
  password <pw>` once, then open the web app — everything else still works.
* **Soft blocked** → set `INCUBATOR_WIFI_COUNTRY=<CC>` in `/etc/incubator.env`
  (the `incubator-wifi-country` unit runs `rfkill unblock wlan` and
  `iw reg set CC` at boot).
* The interface name is auto-detected (first `/sys/class/net/*/wireless`).
  Set `WIFI_IFACE=wlan1` to force one.
* `dnsmasq` must be installed (`dnsmasq-base` on Debian): NetworkManager's
  shared mode uses it for DHCP/DNS on the hotspot. `install.sh` installs it;
  `which dnsmasq` to confirm.

## 2. GPIO: find your chip and your lines

```bash
ls /dev/gpiochip*
gpioinfo                       # libgpiod tools: every chip, line offset, name, consumer
```

* **One chip** (most boards) → nothing to set; bare numbers in
  `GPIO_*_PIN` are line offsets on `gpiochip0`.
* **Several chips** → either set `GPIO_CHIP=gpiochip1` for all pins, or
  address pins explicitly as `gpiochip1:5`.
* **Named lines** (Allwinner `PA12`, Rockchip `GPIO3_B2` where the kernel
  names them, Pi `GPIO17`) can be used directly: `GPIO_HEATER_PIN=PA12`.
  The app searches every chip for the name.
* Pi BCM numbers keep working on every Pi model (including Pi 5's
  `gpiochip4`) because the app resolves `GPIO<n>` by line name.
* Backend: `GPIO_BACKEND=auto` picks `gpiod` when `/dev/gpiochip*` exists.
  Force `sysfs` on an old kernel that still exposes `/sys/class/gpio` (line
  numbers are then `gpiochip base + offset`), or `rpi` to use RPi.GPIO.
* The service runs as root, so no `gpio` group is needed. To run as another
  user, add a udev rule giving that user `/dev/gpiochip*` and `/dev/i2c-*`.

Map the pins in `/etc/incubator.env`:

```ini
GPIO_HEATER_PIN=PC7
GPIO_FAN_PIN=PC8
GPIO_ALARM_PIN=PA6
GPIO_SETUP_BUTTON_PIN=PA1
GPIO_CANDLE_PIN=PA12
GPIO_RELAY_ACTIVE_LOW=true
```

**Boot-time relay state.** On a Pi, `config.txt` drives the relay pins to
their OFF level before the kernel loads. On other boards the pins float
during boot and an active-LOW relay module may click ON for ~30 s. Fix it in
hardware (a 10 kΩ pull-up from the relay input to VCC) or in the device tree
(a `gpio-hog` node with `output-high` for the relay lines). The control
daemon asserts OFF the moment it starts and re-asserts every cycle, and the
thermal cutoff in [HARDWARE_SAFETY_ADDENDUM.md](HARDWARE_SAFETY_ADDENDUM.md)
covers the rest.

## 3. Sensor

Preferred on a new board: an **I²C** sensor (SHT3x, AHT20, BME280, HTU21D).

```bash
ls /dev/i2c-*                  # enable the bus in the vendor tool if missing (armbian-config → System → Hardware)
i2cdetect -y 1                 # 0x44 SHT3x · 0x38 AHT20 · 0x76/0x77 BME280 · 0x40 HTU21D · 0x68 DS3231 RTC
```

Set `SENSOR_I2C_BUS=<n>` if the bus is not `1`; `SENSOR_BACKEND=auto` probes
the addresses above in order, or pin one (`SENSOR_BACKEND=sht3x`). On Armbian
the installer adds an `i2c0`-style entry to `overlays=` in
`/boot/armbianEnv.txt` if none is present — check the overlay name for your
SoC (`ls /boot/dtb/*/overlay/ | grep i2c`) and adjust.

**DHT22 without a Pi:** use the kernel's `dht11` driver through a
device-tree overlay (compatible `"dht11"`, `gpios = <&pio ... >`); it then
appears as `iio:deviceN` with `in_temp_input` and `in_humidityrelative_input`
and `SENSOR_BACKEND=auto` picks it up (`iio`). The adafruit bit-banging
driver (`dht`) is Pi-only.

No sensor connected yet? `SENSOR_BACKEND=mock` keeps the UI alive; the
heater stays off because the loop refuses to heat on a mock reading.

## 4. Camera

`CAMERA_BACKEND=auto`: picamera2 (Pi camera, apt package) → OpenCV on
`/dev/video0` (USB webcams; `pip install opencv-python-headless` into
`/opt/incubator/.venv`) → mock. For a CSI camera on a non-Pi board, expose it
as a V4L2 device via the vendor's pipeline and use `opencv`.

## 5. Status LED and buzzer

```bash
ls /sys/class/leds             # e.g. orangepi:red:status, led0, ACT, user-led
```

`STATUS_LED=auto` picks the first known status/user/activity LED;
`STATUS_LED=orangepi:red:status` pins one; `STATUS_LED=gpio:PA10` drives an
external LED; `STATUS_LED=none` disables it. The buzzer is on
`GPIO_ALARM_PIN`. The LED/beep legend is in the README.

## 6. Networking around the appliance

* **netplan (Ubuntu):** the installer writes `/etc/netplan/90-incubator-nm.yaml`
  so NetworkManager is the renderer. `sudo netplan apply` after the first
  install if `nmcli device` still shows the Wi-Fi as unmanaged.
* **Firewall:** `install.sh` opens 8000/tcp, 80/tcp (captive portal),
  mDNS 5353/udp, DHCP 67/udp and DNS 53 on firewalld (Fedora/openSUSE) and
  ufw (Ubuntu) if they are active. Other firewalls: open the same.
* **mDNS:** avahi advertises `_http._tcp` on 8000 and answers
  `<hostname>.local`. The hostname is `<base>-<last 4 of the device id>`
  (`firstboot.sh`). Android phones usually cannot resolve `.local`; the setup
  wizard therefore also shows the IP.
* **Runtime dir:** `/etc/tmpfiles.d/incubator.conf` creates `/run/incubator`
  (tmpfs) for the control-state file, command queue, setup-state and camera
  preview frames, so nothing transient hits the SD card.
* **Hardware watchdog:** configured when `/dev/watchdog` exists
  (`RuntimeWatchdogSec=14`). If your SoC watchdog needs a longer or shorter
  timeout, edit `/etc/systemd/system.conf.d/10-incubator-watchdog.conf`.

## 7. Root-fs expansion, device id, hostname

`incubator-firstboot.service` runs once: device id from the SoC serial
(`PI-…` on a Pi) or `/etc/machine-id` (`INC-…`), hostname suffix, and root
filesystem expansion via `raspi-config` or `growpart` + `resize2fs` (ext4).
Boards whose vendor image already auto-expands are unaffected.

## 8. Test it

```bash
sudo /opt/incubator/.venv/bin/python -m app.hal          # detection report
sudo systemctl status incubator incubator-control        # both active
curl -s localhost:8000/health | python3 -m json.tool      # board, gpio_backend, sensor_backend
journalctl -u incubator-control -n 30                     # heater/fan decisions every 10 s
```

Then do the farmer test: hold the setup button 4 s (one beep, hotspot up),
join `Incubator-XXXX` on a phone, run the two-step wizard, hear the chime,
and let the wizard find the device on your network.

## 9. Building an image for the board

`build_image.sh --base <vendor-image.img.xz>` loop-mounts a single-partition
(Armbian-style) or boot+root image, runs `install.sh` inside it in
image-build mode, and verifies the generic checklist. Pi-only steps
(PARTUUID rewrite, `config.txt`) run only when the boot partition carries a
`cmdline.txt`. Set the pin map and `INCUBATOR_WIFI_COUNTRY` in
`deploy/incubator.env.example` (or a copy) before building if your board's
defaults differ.

## Ownership summary

| Concern | Where it is decided |
|---|---|
| Board detection | `app/hal/board.py` (`detect_board()`) |
| GPIO | `app/hal/gpio/` — `GPIO_BACKEND`, `GPIO_CHIP`, `GPIO_*_PIN` |
| Sensor | `app/hal/sensors/` — `SENSOR_BACKEND`, `SENSOR_I2C_*`, `SENSOR_IIO_DEVICE` |
| LED | `app/hal/led.py` — `STATUS_LED` |
| Wi-Fi interface | `app/hal/wifi.py` — `WIFI_IFACE` |
| Packages, units, tuning | `install.sh` |
| First boot identity | `scripts/firstboot.sh` |
