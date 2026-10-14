# Notes for the integrator — Section A (hardware abstraction layer)

Worktree branch: `worktree-wf_97729aa6-4fe-1` (based on `fa42c4f`).

## What landed

| Path | Purpose |
|------|---------|
| `app/hal/__init__.py` | re-exports `detect_board, BoardInfo, get_gpio_backend, parse_pin, get_sensor, StatusLed, wifi_interface, wifi_interfaces, supports_ap_mode` (+ `PinSpec, GpioBackend, GpioBackendUnavailable, Sensor, MockSensor, OutputLine, InputLine`) |
| `app/hal/_util.py` | `optional_import()` (the only way hardware libs are imported — never at module import time), sysfs read/write helpers |
| `app/hal/board.py` | `detect_board(root="/") -> BoardInfo` (+ `as_dict()`, `short_model`) |
| `app/hal/gpio/{__init__,base,chips,_gpiod_common,gpiod_v2,gpiod_v1,rpi,sysfs,mock}.py` | `parse_pin`, `get_gpio_backend`, `available_backends`, `list_gpio_chips`; backends gpiod v2 / gpiod v1 / RPi.GPIO / sysfs / mock |
| `app/hal/sensors/{__init__,base,iio,i2c,dht,mock}.py` | `get_sensor`; IIO, SHT3x, AHT20, BME280/BMP280, HTU21D/SI7021 (smbus2), adafruit DHT, mock |
| `app/hal/led.py` | `StatusLed` (sysfs LED or `gpio:<pinspec>`), patterns `off/on/heartbeat/blink_fast/blink_double/sos` |
| `app/hal/wifi.py` | `wifi_interfaces / wifi_interface / supports_ap_mode / parse_iw_modes / phy_name` |
| `app/hal/__main__.py` | `python -m app.hal` → JSON report (board, gpio backend + chips, sensor + one reading, LED, Wi-Fi iface + AP support). Never raises; `-v` for debug logs on stderr |
| `app/config.py` | pins are now `str`; new section "Hardware abstraction (app/hal)" with every setting from spec A |
| `pyproject.toml` | `requires-python = ">=3.10"`, extras `hardware`, `i2c`, new description |
| `tests/test_hal_*.py` | 146 new tests; `tests/test_hal_fakes.py` holds the fake gpiod v1/v2, RPi.GPIO, smbus2, adafruit modules and fake I2C devices (no tests inside; imported as `tests.test_hal_fakes`, which works because pyproject sets `pythonpath = ["."]`) |

## Test results

```
PYTHONPATH=. .venv/bin/python -m pytest -q      →  362 passed in 35 s   (216 existing + 146 new)
```

**ENVIRONMENT CAVEAT — read this before trusting a red suite.** `/usr/bin/nmcli` is now installed in this
container (it was not when the 216-test baseline was green at the start of the run; probably pulled in by
installer work in another worktree) while NetworkManager itself is not running. Every `nmcli` call then blocks on
D-Bus for ~25 s and exits 1 ("Could not create NMClient object"). Five *pre-existing* tests assume
"no nmcli → WiFiService simulates success" (see the comment in `tests/test_network.py:46`) and fail, and the
suite takes ~7.5 min, **with or without the HAL changes** (reproduced on the three files alone, no HAL tests
collected):

- `tests/test_farmer_flow.py::test_recovery_resubmit_keeps_account_and_name`
- `tests/test_farmer_flow.py::test_recovery_mode_clears_after_successful_join`
- `tests/test_network.py::test_network_status_reports_configured_ssid`
- `tests/test_network.py::test_network_connect_updates_device_config`
- `tests/test_password_reset.py::test_reset_forbidden_outside_setup_mode`

The 362-pass figure above was produced with nmcli hidden from `PATH` (a shim dir mirroring `/usr/bin` minus
`nmcli`). Suggested durable fixes (files owned by agent E / the integrator):

1. `app/services/wifi_service.py`: detect "NetworkManager unavailable" once (e.g. `nmcli -t -f RUNNING general`
   with a 3 s timeout, or `FileNotFoundError`) and take the existing *simulated* path in both cases; or
2. `tests/conftest.py`: set an env such as `INCUBATOR_NMCLI_BIN=/nonexistent` that `_nmcli()` honours.

## Spec deviations and judgement calls

- `get_gpio_backend()` raises `GpioBackendUnavailable` (a `RuntimeError` subclass) for an *unknown* backend name
  too, not `ValueError`, so a caller that catches `RuntimeError` to fall back to mock keeps working. Aliases
  `gpiod-v1 / gpiod_v1 / gpiod1` and `gpiod-v2 / …` force an API generation.
- `get_gpio_backend(..., dev_dir=, sysfs_root=)` keyword-only test hooks exist (default `/dev`, `/sys/class/gpio`);
  `app.hal.gpio.chips.DEV_DIR` is the module attribute for the device listing.
- gpiod chip resolution: an explicit `chip` hint is used directly for bare offsets; a line *name* is searched
  hint-chip first, then every chip. Positive name lookups are cached per backend instance (no negative cache so
  late-appearing chips still work).
- sysfs backend always writes to `export`; a failed write (the kernel's EBUSY = already exported, e.g. by a crashed
  previous run) marks the line "not ours" and it is **not** unexported on release. Chip base is found through the
  `gpiochip<base>/device/gpiochipN` link; when that link is missing the Nth sysfs chip by base is used with a
  warning. Bias cannot be set via sysfs (debug log).
- `get_sensor()` with an explicit backend that is unavailable returns `None` and logs a warning (it does not
  raise), matching the `Sensor | None` signature and the `no_sensor` path in spec D. An unknown name logs a warning
  and falls back to `auto`. Extra keyword hooks: `dev_dir=`, `iio_root=`, `bus_factory=`.
- IIO: the "|value| > 200 → milli-units" heuristic is applied to `_input` channels as well as to unscaled `_raw`
  ones (the IIO ABI reports processed temperature/humidity in milli-units: the dht11 driver gives 37500). After
  `raw × scale` the same heuristic applies. `SENSOR_IIO_DEVICE` accepts `iio:device0`, `0`, a full path or a
  device *name* (`dht11`).
- AHT20 verifies the CRC byte by default (`AHT20Sensor(bus, addr, verify_crc=False)` for an AHT10, which has none).
- HTU21D `probe()` requires a CRC-valid temperature conversion, not just an ACK, because 0x40 is also the INA219
  address used by the heater-current sensor (tested with a fake INA219).
- BME280 reading carries an extra `pressure_hpa` key; a BMP280 (chip id 0x58/0x56/0x57) reports `humidity_pct=None`
  and `sensor="bmp280"`. Compensation verified against the BMP280 datasheet example (25.08 °C / 100653 Pa) and the
  datasheet double-precision humidity formula.
- I2C drivers talk to a tiny `I2CBus` interface (`write_bytes/read_bytes/read_block/…`); `SMBus2Bus` is the smbus2
  implementation (`i2c_rdwr` raw transfers). Tests replay datasheet bytes through a `FakeBus`.
- `StatusLed`: `gpio:!<pin>` = active-low LED; `on`/`off` are written synchronously under an I/O lock and the worker
  re-checks the pattern under that lock before every step, so a steady state is never overwritten by a stale blink
  step. A trigger that is already `none` is left untouched (nothing written on open or close).
  `StatusLed.create("gpio:…")` without a backend builds `get_gpio_backend("auto")` itself.
- `wifi_interfaces()` lists `wl*` names first. `supports_ap_mode()` uses `iw phy <phy> info` when
  `/sys/class/net/<iface>/phy80211/name` exists, else `iw list`; returns `None` when `iw` is missing, fails, or
  prints no "Supported interface modes" block.
- `detect_board()` on x86 uses `/sys/class/dmi/id/product_name` as the model (ignoring OEM placeholders).
- `app/config.py`: `gpio_backend`/`sensor_backend` are lower-cased and stripped; pins are stripped; `sensor_i2c_addr`
  parses hex or decimal (`int(x, 0)`); `setup_state_path` default comes from `_default_setup_state_path()`
  (`/run/incubator/setup-state.json` when that dir is writable, else `<tempdir>/incubator_setup_state.json`).
- Python 3.10: every HAL module uses `from __future__ import annotations`; the whole package and tests parse with
  `ast` at `feature_version=(3, 10)`. Nothing uses `match`, `ExceptionGroup`, `tomllib` or `typing.Self`.
- Git author on my commits is a neutral `incubator-dev <dev@incubator.local>` (the worktree's configured identity
  would have attributed them to an AI tool, which the task forbids). No test was deleted or weakened.

## Cross-file requests (files owned by other agents)

### Consumers of the `gpio_*_pin` settings, which are now `str` (e.g. `"17"`, `"GPIO17"`, `"gpiochip1:5"`)

The current suite is green with strings because nothing in the mock path does arithmetic on them, but these spots
must change when the services move onto the HAL (agent D):

1. `app/services/gpio_service.py` (agent D)
   - lines 55–64: constructor type hints `*_pin: int` → `str` (or `int | str`).
   - line 129 `_GPIO.setup(pin, …)` / 164–179 `_GPIO.output/input(pin)` / 355–359: RPi.GPIO needs ints — replace with
     `backend.request_output(parse_pin(pin), initial=…)` / `.request_input(parse_pin(pin), pull="up")` from `app.hal.gpio`.
   - line 135 `getattr(board, f"D{self.dht_pin}")` works for `"4"` but not `"GPIO4"`/`"gpiochip0:4"` → use
     `app.hal.sensors.dht.bcm_from_pin()` or simply `get_sensor(...)`.
   - line 138 `logger.info("DHT22 initialised on GPIO%d", self.dht_pin)` → `%s` (with a str, logging prints a
     traceback to stderr instead of the message).
2. `app/services/button_service.py` (agent D)
   - line 35 `gpio_pin: int = 18` → `gpio_pin: str = "18"`.
   - line 60 `_GPIO.input(self.gpio_pin)` needs an int → per spec D use `read_pressed=gpio_service.read_button`.
   - line 97 `"… BCM pin %d"` → `%s`.
3. `app/main.py` lines 126–135, 216 and `app/control/loop.py` lines 477–486 (agent D): pass the settings through
   unchanged — fine once `GPIOService`/`SetupButtonService` accept strings.
4. `app/routes/web.py` lines 389 and 419 (agent E) pass `dht_pin` to templates; `app/templates/status.html` lines
   26/29 and `help.html` line 37 (agent C) render `GPIO{{ dht_pin }}` — correct for `"4"`, but a line-name pin would
   read "GPIOGPIO4". Cosmetic: render `{{ dht_pin }}` with the "GPIO" prefix only when it is all digits, e.g.
   `{% if dht_pin|string is match('^\d+$') %}GPIO{% endif %}{{ dht_pin }}` or have web.py pass a display label.

### How to consume the HAL (for agent D)

```python
from app.hal import GpioBackendUnavailable, get_gpio_backend, get_sensor, parse_pin

try:
    backend = get_gpio_backend(settings.gpio_backend, settings.gpio_chip)
except GpioBackendUnavailable:
    if not settings.gpio_mock:
        raise
    backend = get_gpio_backend("mock")

heater = backend.request_output(parse_pin(settings.gpio_heater_pin), initial=relay_off_level, consumer="incubator-heater")
heater.set(level)            # True = line HIGH
button = backend.request_input(parse_pin(settings.gpio_setup_button_pin), pull="up")
pressed = not button.get()   # pulled-up button: pressed = LOW
backend.name                 # "gpiod" | "rpi" | "sysfs" | "mock"   (for /api/device/info)

sensor = get_sensor(
    settings.sensor_backend,
    i2c_bus=settings.sensor_i2c_bus,
    i2c_addr=settings.sensor_i2c_addr,
    iio_device=settings.sensor_iio_device,
    dht_pin=settings.gpio_dht_pin,
)                            # Sensor | None ; sensor.name ; sensor.read() -> {"ok", "temperature_c", "humidity_pct", "mock", "sensor", ["error"]}
```

- `read()` never raises; treat `ok=False` as "no reading" (heater OFF). `mock` is `True` only for `MockSensor`.
- In tests, `MockGpioBackend.lines["18"]` / `.set_input_value("18", False)` simulate electrical levels.
- `backend.close()` releases every line; `sensor.close()` closes an owned I2C bus; `StatusLed.close()` restores the
  LED trigger and stops the pattern thread.
- `StatusLed.create(settings.status_led, gpio_backend=backend)` → `StatusLed | None`; patterns per spec B.

### For agent B (installer/docs)

- `python -m app.hal` prints `{"board": …, "gpio": {"requested", "available", "chips", "backend", "api_version",
  "chip_info"}, "sensor": {"requested", "sensor", "reading"}, "led": {"requested", "sysfs_leds", "led"},
  "wifi": {"interfaces", "interface", "env_WIFI_IFACE", "supports_ap_mode"}, "python"}`.
- `pip install -e ".[hardware]"` pulls `gpiod>=2.1` (needs `libgpiod-dev` + `python3-dev` to build) and `smbus2`.
  Debian's `python3-libgpiod` (v1 API on bookworm, v2 on trixie) is equally fine with `--system-site-packages`; the
  HAL supports both API generations.
- New env knobs documented in `app/config.py`: `GPIO_BACKEND`, `GPIO_CHIP`, `SENSOR_BACKEND`, `SENSOR_I2C_BUS`,
  `SENSOR_I2C_ADDR`, `SENSOR_IIO_DEVICE`, `STATUS_LED` (incl. `gpio:<pin>` / `gpio:!<pin>`), `INCUBATOR_SOUND_FEEDBACK`,
  `INCUBATOR_FACTORY_RESET_HOLD_SECONDS`, `WIFI_IFACE`, `INCUBATOR_SETUP_STATE_PATH`, `CONTROL_PERSIST_PATH`.
