"""
GPIO + sensor access for the incubator, on top of :mod:`app.hal`.

Pins are *specs* (``"17"``, ``"GPIO17"``, ``"gpiochip1:5"``) resolved by the
HAL, so the same service runs on a Raspberry Pi (libgpiod or RPi.GPIO), any
board with a GPIO character device, a sysfs-only kernel, or — explicitly — the
in-memory mock backend used on dev machines and in CI.

Ownership: the web app and the control daemon are separate processes and must
never drive the same line. ``owned_outputs`` restricts which outputs this
instance requests; a ``set_*`` on an un-owned output is refused with
``{"ok": False, "error": "not_owned"}`` (and a one-time warning).

Fail-loud rule: a missing or failed temperature sensor returns ``ok=False`` —
never a fabricated reading — unless this instance was built in mock mode. If
GPIO resolves to the mock backend *without* being asked (no gpiod / RPi.GPIO /
sysfs on a real box) that is logged at WARNING and exposed as
``backend_name == "mock"`` / ``mock_fallback`` so /health can show it.

Wiring reference for the reference Pi build (BCM numbers, override via env):
  GPIO4  → DHT22 data                 GPIO24 → Candle / candling LED
  GPIO12 → Lock relay (active-LOW)    GPIO25 → Alarm / buzzer
  GPIO13 → Door relay (active-LOW)    GPIO27 → Fan relay (active-LOW)
  GPIO17 → Heater relay (active-LOW)  GPIO18 → Setup button (pull-up, pressed = LOW)
  GPIO22 → Turner motor step          GPIO23 → Turner motor direction
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Every output this service knows about, in a stable order.
OUTPUT_NAMES: tuple[str, ...] = ("heater", "fan", "turner", "turner_dir", "candle", "alarm", "lock", "door")
# Outputs that drive a relay module (active-LOW by default); the rest are
# plain logic-level lines (HIGH = on).
RELAY_OUTPUTS: frozenset[str] = frozenset({"heater", "fan", "lock", "door"})
# Outputs the control daemon owns when CONTROL_DAEMON_ENABLED.
DAEMON_OUTPUTS: frozenset[str] = frozenset({"heater", "fan", "turner", "turner_dir"})
# ...and what the web app keeps in that mode.
WEB_OUTPUTS: frozenset[str] = frozenset({"candle", "alarm", "lock", "door"})

# Egg-turner stepping bounds: 1 step minimum; 4000 steps (~20 revolutions of a
# 200-step motor) is more than any turner geometry needs and bounds how long
# a single command can hold the motor (and the caller) busy.
TURNER_MIN_STEPS = 1
TURNER_MAX_STEPS = 4000
TURNER_STEP_SECONDS = 0.001

# Retries for a transient sensor read failure (DHT checksum errors are common).
SENSOR_READ_ATTEMPTS = 3
SENSOR_RETRY_SLEEP = 0.5


def clamp_turner_steps(steps: Any, default: int = 200) -> int:
    """Clamp a requested step count into ``TURNER_MIN_STEPS..TURNER_MAX_STEPS``.

    Accepts anything int()-able; garbage falls back to ``default``. The sign is
    dropped — callers pass direction separately.
    """
    try:
        n = abs(int(steps))
    except (TypeError, ValueError):
        n = default
    if n == 0:
        n = default
    return max(TURNER_MIN_STEPS, min(TURNER_MAX_STEPS, n))


class GPIOService:
    """Low-level GPIO driver. One instance is shared application-wide."""

    def __init__(
        self,
        dht_pin: Any,
        heater_pin: Any,
        fan_pin: Any,
        turner_pin: Any,
        turner_dir_pin: Any,
        candle_pin: Any,
        alarm_pin: Any,
        lock_pin: Any,
        door_pin: Any,
        setup_button_pin: Any,
        relay_active_low: bool = True,
        mock: bool = False,
        heater_current_enabled: bool = False,
        heater_current_bus: int = 1,
        heater_current_addr: int = 0x40,
        heater_current_shunt_ohms: float = 0.1,
        *,
        backend: str = "auto",
        chip: str = "",
        sensor: Any = "auto",
        owned_outputs: "set[str] | frozenset[str] | None" = None,
        sensor_backend: str = "auto",
        sensor_i2c_bus: int = 1,
        sensor_i2c_addr: int = 0,
        sensor_iio_device: str = "",
        request_button: bool = True,
    ) -> None:
        self.dht_pin = dht_pin
        self.heater_pin = heater_pin
        self.fan_pin = fan_pin
        self.turner_pin = turner_pin
        self.turner_dir_pin = turner_dir_pin
        self.candle_pin = candle_pin
        self.alarm_pin = alarm_pin
        self.lock_pin = lock_pin
        self.door_pin = door_pin
        self.setup_button_pin = setup_button_pin
        self.relay_active_low = relay_active_low

        self._pins: dict[str, Any] = {
            "heater": heater_pin,
            "fan": fan_pin,
            "turner": turner_pin,
            "turner_dir": turner_dir_pin,
            "candle": candle_pin,
            "alarm": alarm_pin,
            "lock": lock_pin,
            "door": door_pin,
        }
        if owned_outputs is None:
            self.owned_outputs: frozenset[str] = frozenset(OUTPUT_NAMES)
        else:
            unknown = set(owned_outputs) - set(OUTPUT_NAMES)
            if unknown:
                raise ValueError(f"unknown outputs {sorted(unknown)}; choose from {OUTPUT_NAMES}")
            self.owned_outputs = frozenset(owned_outputs)

        self._mock_requested = bool(mock) or str(backend or "").strip().lower() == "mock"
        self._backend_requested = "mock" if self._mock_requested else (backend or "auto")
        self._chip = chip or ""
        # Effective mock state (updated by setup()). Tests may force it.
        self._mock = self._mock_requested
        self.mock_fallback = False

        self._sensor_spec = sensor
        # Only the process that hosts SetupButtonService may request the button
        # line: gpiod lines are exclusive, and the control daemon never reads it.
        self._request_button = bool(request_button)
        self._sensor_backend = sensor_backend or "auto"
        self._sensor_i2c_bus = sensor_i2c_bus
        self._sensor_i2c_addr = sensor_i2c_addr
        self._sensor_iio_device = sensor_iio_device
        self._sensor: Any = None

        # Optional INA219 heater-current sensor (see read_heater_current).
        self._heater_current_enabled = heater_current_enabled
        self._heater_current_bus_num = heater_current_bus
        self._heater_current_addr = heater_current_addr
        self._heater_current_shunt_ohms = heater_current_shunt_ohms
        self._ina_bus: Any = None
        self._ina_attempted = False

        self._backend: Any = None
        self._lines: dict[str, Any] = {}
        self._button_line: Any = None
        self._lock = threading.RLock()          # outputs + lifecycle
        self._turner_lock = threading.Lock()    # stepping (never under _lock)
        self._sensor_lock = threading.Lock()    # sensor reads
        self._warned_not_owned: set[str] = set()
        self._state: dict[str, Any] = {name: False for name in OUTPUT_NAMES}
        self._initialized = False

    # ------------------------------------------------------------------
    # Introspection (for /health, /api/device/info)
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        if self._backend is not None:
            return getattr(self._backend, "name", "unknown")
        return "mock" if self._mock else str(self._backend_requested)

    @property
    def sensor_name(self) -> "str | None":
        if self._sensor is not None:
            return getattr(self._sensor, "name", "sensor")
        if self._sensor_spec is None:
            return None
        return None if self._initialized else str(self._sensor_backend)

    @property
    def is_mock(self) -> bool:
        return bool(self._mock)

    @property
    def sensor(self) -> Any:
        return self._sensor

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend_name,
            "backend_requested": self._backend_requested,
            "mock": self.is_mock,
            "mock_fallback": self.mock_fallback,
            "sensor": self.sensor_name,
            "owned_outputs": sorted(self.owned_outputs),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_backend(self) -> Any:
        from ..hal.gpio import GpioBackendUnavailable, MockGpioBackend, get_gpio_backend

        if self._mock_requested:
            return MockGpioBackend(chip=self._chip)
        try:
            backend = get_gpio_backend(self._backend_requested, self._chip)
        except GpioBackendUnavailable as exc:
            logger.error("GPIO backend %r unavailable: %s — falling back to MOCK (no pins are driven)",
                         self._backend_requested, exc)
            return MockGpioBackend(chip=self._chip)
        except Exception as exc:  # noqa: BLE001 — never crash startup on GPIO probing
            logger.error("GPIO backend %r failed to initialise: %s — falling back to MOCK", self._backend_requested, exc)
            return MockGpioBackend(chip=self._chip)
        return backend

    def _build_sensor(self) -> Any:
        from ..hal.sensors import MockSensor, Sensor, get_sensor

        spec = self._sensor_spec
        if spec is None:
            return None
        if isinstance(spec, Sensor):
            return spec
        if isinstance(spec, str) and spec.strip().lower() == "mock":
            return MockSensor()
        backend = str(spec).strip().lower() if isinstance(spec, str) and spec.strip().lower() != "auto" else self._sensor_backend
        backend = (backend or "auto").strip().lower()
        if backend == "none":
            return None
        # Only an EXPLICIT mock request may simulate readings. A GPIO backend
        # that merely fell back to mock on a real box must still report the
        # truth about the sensor (no_sensor / read failure), never 37.4 degC.
        if self._mock_requested and backend == "auto":
            return MockSensor()
        try:
            found = get_sensor(
                backend,
                i2c_bus=self._sensor_i2c_bus,
                i2c_addr=self._sensor_i2c_addr,
                iio_device=self._sensor_iio_device,
                dht_pin=self.dht_pin,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sensor detection failed: %s", exc)
            found = None
        if found is None and self._mock_requested:
            logger.info("GPIO mock mode: sensor backend %r found nothing — using the mock sensor", backend)
            return MockSensor()
        return found

    def _off_level(self, name: str) -> bool:
        """Electrical level that means OFF for an output."""
        if name in RELAY_OUTPUTS:
            return bool(self.relay_active_low)
        return False

    def _on_level(self, name: str, on: bool) -> bool:
        if name in RELAY_OUTPUTS and self.relay_active_low:
            return not on
        return bool(on)

    def setup(self) -> None:
        with self._lock:
            if self._initialized:
                return
            from ..hal.gpio import parse_pin

            self._backend = self._build_backend()
            self._mock = getattr(self._backend, "name", "") == "mock"
            self.mock_fallback = self._mock and not self._mock_requested
            if self.mock_fallback:
                logger.warning(
                    "GPIO is running on the MOCK backend although GPIO_MOCK is false: no gpiod, RPi.GPIO or "
                    "sysfs access was found. Outputs are NOT driven. Install python3-libgpiod / pip install gpiod, "
                    "or set GPIO_BACKEND explicitly."
                )
            elif self._mock:
                logger.info("GPIOService: mock mode active")

            for name in OUTPUT_NAMES:
                if name not in self.owned_outputs:
                    continue
                try:
                    pin = parse_pin(self._pins[name])
                    self._lines[name] = self._backend.request_output(
                        pin, initial=self._off_level(name), consumer=f"incubator-{name}",
                    )
                except Exception as exc:  # noqa: BLE001 — one bad pin must not take the rest down
                    logger.error("GPIO: could not request output %s (%r): %s", name, self._pins[name], exc)
                    self._lines[name] = None
            self._button_line = None
            if self._request_button:
                try:
                    self._button_line = self._backend.request_input(
                        parse_pin(self.setup_button_pin), pull="up", consumer="incubator-button",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("GPIO: could not request setup button input (%r): %s", self.setup_button_pin, exc)
                    self._button_line = None

            self._sensor = self._build_sensor()
            self._initialized = True
            logger.info(
                "GPIOService initialised: backend=%s sensor=%s outputs=%s",
                self.backend_name, self.sensor_name, ",".join(sorted(self.owned_outputs)),
            )

    def cleanup(self) -> None:
        with self._lock:
            for name, line in list(self._lines.items()):
                if line is None:
                    continue
                try:
                    line.release()
                except Exception:  # noqa: BLE001
                    pass
            self._lines.clear()
            if self._button_line is not None:
                try:
                    self._button_line.release()
                except Exception:  # noqa: BLE001
                    pass
                self._button_line = None
            if self._backend is not None:
                try:
                    self._backend.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._sensor is not None:
                try:
                    self._sensor.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._ina_bus is not None:
                try:
                    self._ina_bus.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ina_bus = None
            self._initialized = False

    def _ensure_setup(self) -> None:
        if not self._initialized:
            self.setup()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_output(self, name: str, on: bool) -> dict[str, Any]:
        """Drive one named output. Call with ``self._lock`` held."""
        if name not in self.owned_outputs:
            if name not in self._warned_not_owned:
                self._warned_not_owned.add(name)
                logger.warning("GPIO output %r is owned by another process — refusing to drive it", name)
            return {"ok": False, "error": "not_owned", "output": name}
        self._ensure_setup()
        line = self._lines.get(name)
        if line is None:
            self._state[name] = bool(on)
            return {"ok": False, "error": "line_unavailable", "output": name}
        try:
            line.set(self._on_level(name, on))
        except Exception as exc:  # noqa: BLE001
            logger.error("GPIO write %s=%s failed: %s", name, on, exc)
            return {"ok": False, "error": f"write_failed: {exc}", "output": name}
        self._state[name] = bool(on)
        return {"ok": True}

    def _set(self, name: str, on: bool, key: str) -> dict[str, Any]:
        with self._lock:
            result = self._write_output(name, on)
            if not result.get("ok"):
                return result
            return {"ok": True, key: bool(on)}

    # ------------------------------------------------------------------
    # Sensor reads
    # ------------------------------------------------------------------

    def read_temperature_humidity(self) -> dict[str, Any]:
        """Read the chamber sensor. Returns ``{ok, temperature_c, humidity_pct, mock, sensor[, error]}``.

        No sensor → ``ok=False, error="no_sensor"``. A failed read is retried a
        few times (DHT checksum errors are routine) and then reported as
        ``ok=False`` — never replaced by a made-up value. The service lock is not
        held across the retry sleeps.
        """
        self._ensure_setup()
        with self._sensor_lock:
            sensor = self._sensor
            if sensor is None:
                return {
                    "ok": False, "error": "no_sensor", "temperature_c": None, "humidity_pct": None,
                    "mock": False, "sensor": None,
                }
            last: dict[str, Any] = {}
            for attempt in range(SENSOR_READ_ATTEMPTS):
                try:
                    last = dict(sensor.read() or {})
                except Exception as exc:  # noqa: BLE001 — a sensor must never crash the poller
                    last = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if last.get("ok"):
                    out = {
                        "ok": True,
                        "mock": bool(last.get("mock", False)),
                        "temperature_c": last.get("temperature_c"),
                        "humidity_pct": last.get("humidity_pct"),
                        "sensor": last.get("sensor", getattr(sensor, "name", "sensor")),
                    }
                    # Temperature is what the thermostat needs; a temperature-only
                    # sensor (BMP280, DS18B20 via IIO) is a valid reading with
                    # humidity unknown — never a failure.
                    if out["temperature_c"] is None:
                        last = {"ok": False, "error": "incomplete reading"}
                    else:
                        return out
                if attempt < SENSOR_READ_ATTEMPTS - 1 and not last.get("mock"):
                    time.sleep(SENSOR_RETRY_SLEEP)
            error = str(last.get("error") or "sensor read failed")
            logger.warning("sensor %s read failed after %d attempts: %s", getattr(sensor, "name", "?"), SENSOR_READ_ATTEMPTS, error)
            return {
                "ok": False, "error": error, "temperature_c": None, "humidity_pct": None,
                "mock": False, "sensor": getattr(sensor, "name", "sensor"),
            }

    def read_button(self) -> bool:
        """True while the setup button is pressed (pulled-up input, pressed = LOW)."""
        if not self._initialized:
            if self._mock:
                return False
            self.setup()
        line = self._button_line
        if line is None:
            return False
        try:
            return not bool(line.get())
        except Exception as exc:  # noqa: BLE001
            logger.debug("button read failed: %s", exc)
            return False

    def set_mock_button(self, pressed: bool) -> None:
        """Test hook: simulate the button level on the mock backend."""
        self._ensure_setup()
        setter = getattr(self._backend, "set_input_value", None)
        if setter is None or self._button_line is None:
            raise RuntimeError("set_mock_button needs the mock GPIO backend")
        setter(self._button_line.pin, not pressed)

    # ------------------------------------------------------------------
    # Heater-current sensing (optional INA219 on the free I2C bus)
    # ------------------------------------------------------------------

    def _ensure_ina219(self) -> Any:
        """Lazily open the I2C bus for the INA219 (idempotent, best-effort).

        Uses smbus2 / smbus if present; if neither the library nor the bus is
        available the sensor is simply reported absent — the feature is optional
        and must never crash the poller. Nothing is imported or opened unless the
        operator enabled the sensor, so units without it pay nothing.
        """
        if self._ina_bus is not None:
            return self._ina_bus
        if self._ina_attempted:
            return None
        self._ina_attempted = True
        try:
            try:
                from smbus2 import SMBus  # type: ignore
            except ImportError:
                from smbus import SMBus  # type: ignore
            self._ina_bus = SMBus(self._heater_current_bus_num)
            logger.info(
                "INA219 heater-current sensor opened on i2c-%d addr 0x%02x",
                self._heater_current_bus_num, self._heater_current_addr,
            )
        except Exception as exc:  # noqa: BLE001 — optional sensor, degrade gracefully
            logger.warning("INA219 unavailable (heater-current sensing off): %s", exc)
            self._ina_bus = None
        return self._ina_bus

    def read_heater_current(self) -> dict[str, Any]:
        """Read heater-leg current from the INA219, if fitted.

        Returns {"ok", "present", "amps"}. ``present`` is False (and ``amps``
        None) when the sensor is disabled, mocked, or unreadable, so callers can
        cleanly skip the integrity check. Reads the shunt-voltage register (0x01,
        signed, 10 µV/LSB) directly and divides by the shunt resistance — no
        calibration write needed, and the INA219's power-on default config
        already samples the shunt continuously.
        """
        if self._mock or not self._heater_current_enabled:
            return {"ok": True, "present": False, "amps": None}
        bus = self._ensure_ina219()
        if bus is None:
            return {"ok": True, "present": False, "amps": None}
        try:
            raw = bus.read_word_data(self._heater_current_addr, 0x01)
            # SMBus returns the word little-endian; the INA219 is big-endian.
            raw = ((raw & 0xFF) << 8) | (raw >> 8)
            if raw > 0x7FFF:
                raw -= 0x10000
            shunt_v = raw * 1e-5  # 10 µV per LSB
            shunt = self._heater_current_shunt_ohms or 0.1
            amps = abs(shunt_v / shunt)
            return {"ok": True, "present": True, "amps": round(amps, 3)}
        except Exception as exc:  # noqa: BLE001 — a bad read must not break polling
            logger.debug("INA219 read failed: %s", exc)
            return {"ok": False, "present": True, "amps": None}

    # ------------------------------------------------------------------
    # Output control
    # ------------------------------------------------------------------

    def set_heater(self, on: bool) -> dict[str, Any]:
        return self._set("heater", on, "heater")

    def set_fan(self, on: bool) -> dict[str, Any]:
        return self._set("fan", on, "fan")

    def set_candle(self, on: bool) -> dict[str, Any]:
        return self._set("candle", on, "candle")

    def set_alarm(self, on: bool) -> dict[str, Any]:
        return self._set("alarm", on, "alarm")

    def pulse_alarm(self, seconds: float = 0.2) -> dict[str, Any]:
        """Brief buzzer chirp for operator feedback (button press, alarm test).

        A latched alarm (set_alarm) owns the pin — while an alert is sounding
        the chirp is skipped rather than toggling the alarm off behind the
        alert engine's back.  The off-edge runs on a timer so the GPIO lock
        is never held across a sleep.
        """
        with self._lock:
            if self._state["alarm"]:
                return {"ok": True, "pulsed": False, "alarm": True}
            if "alarm" not in self.owned_outputs:
                return {"ok": False, "error": "not_owned", "output": "alarm"}
            self._ensure_setup()
            line = self._lines.get("alarm")
            if line is not None:
                try:
                    line.set(True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("alarm pulse write failed: %s", exc)

        def _off() -> None:
            with self._lock:
                if not self._state["alarm"]:
                    line_ = self._lines.get("alarm")
                    if line_ is not None:
                        try:
                            line_.set(False)
                        except Exception:  # noqa: BLE001
                            pass

        timer = threading.Timer(max(0.05, seconds), _off)
        timer.daemon = True
        timer.start()
        return {"ok": True, "pulsed": True, "alarm": False}

    def set_lock(self, locked: bool) -> dict[str, Any]:
        """Energise relay to LOCK, de-energise to UNLOCK (fail-safe open)."""
        return self._set("lock", locked, "locked")

    def set_door(self, open_: bool) -> dict[str, Any]:
        return self._set("door", open_, "door_open")

    def move_turner(self, steps: int = 200, direction: int = 1) -> dict[str, Any]:
        """Step the egg turner motor. direction=1 forward, direction=-1 reverse.

        ``steps`` is clamped to 1..4000. Stepping happens under a dedicated
        turner lock so a long move never blocks heater/fan/alarm writes.
        """
        steps = clamp_turner_steps(steps)
        direction = -1 if (direction is not None and int(direction) < 0) else 1
        for name in ("turner", "turner_dir"):
            if name not in self.owned_outputs:
                if name not in self._warned_not_owned:
                    self._warned_not_owned.add(name)
                    logger.warning("GPIO output %r is owned by another process — refusing to drive it", name)
                return {"ok": False, "error": "not_owned", "output": name}
        self._ensure_setup()
        with self._turner_lock:
            with self._lock:
                self._state["turner"] = True
                dir_line = self._lines.get("turner_dir")
                step_line = self._lines.get("turner")
            try:
                if dir_line is not None:
                    dir_line.set(direction > 0)
                if step_line is not None and not self._mock:
                    for _ in range(steps):
                        step_line.set(True)
                        time.sleep(TURNER_STEP_SECONDS)
                        step_line.set(False)
                        time.sleep(TURNER_STEP_SECONDS)
                elif step_line is not None:
                    step_line.set(False)
            except Exception as exc:  # noqa: BLE001
                logger.error("turner stepping failed: %s", exc)
                return {"ok": False, "error": f"write_failed: {exc}", "steps": steps, "direction": direction}
            finally:
                with self._lock:
                    self._state["turner"] = False
        return {"ok": True, "steps": steps, "direction": direction}

    def get_state(self) -> dict[str, Any]:
        """Snapshot of all output states (plus backend/mock info)."""
        with self._lock:
            snap = {name: bool(self._state.get(name, False)) for name in OUTPUT_NAMES}
        snap["backend"] = self.backend_name
        snap["mock"] = self.is_mock
        snap["owned"] = sorted(self.owned_outputs)
        return snap
