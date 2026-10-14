"""Temperature/humidity sensor selection.

    from app.hal.sensors import get_sensor
    sensor = get_sensor("auto", i2c_bus=1, dht_pin="4")   # Sensor | None
    sensor.read() -> {"ok", "temperature_c", "humidity_pct", "mock", "sensor"}

Auto order: Linux IIO device exposing temperature + humidity -> I2C probes on
``/dev/i2c-<bus>`` (SHT3x -> AHT20 -> BME280 -> HTU21D) -> adafruit DHT
(library importable and a pin given) -> None.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import dht as _dht
from . import i2c as _i2c
from . import iio as _iio
from .base import Sensor, reading
from .mock import MockSensor

logger = logging.getLogger(__name__)

__all__ = ["Sensor", "MockSensor", "get_sensor", "reading", "SENSOR_BACKENDS"]

SENSOR_BACKENDS = ("auto", "iio", "sht3x", "aht20", "bme280", "htu21d", "dht", "mock", "none")


def _iio_sensor(iio_device: str, iio_root: str | None, *, explicit: bool) -> Sensor | None:
    if iio_device:
        path = _iio.resolve_iio_device(iio_device, iio_root)
        if path is None:
            logger.warning("SENSOR_IIO_DEVICE=%r not found under %s", iio_device, _iio.iio_root(iio_root))
            return None
    else:
        path = _iio.find_iio_device(iio_root, require_humidity=not explicit)
        if path is None:
            return None
    try:
        return _iio.IioSensor(path)
    except Exception as exc:
        logger.warning("IIO device %s unusable: %s", path, exc)
        return None


def _i2c_sensor(
    names: tuple[str, ...],
    i2c_bus: int,
    i2c_addr: int,
    dev_dir: str | None,
    bus_factory: Any,
    *,
    explicit: bool,
) -> Sensor | None:
    dev_path = _i2c.i2c_device_path(i2c_bus, dev_dir)
    if bus_factory is None:
        if not os.path.exists(dev_path):
            if explicit:
                logger.warning("I2C bus %s not present; enable I2C in the device tree / boot config", dev_path)
            return None
        if not _i2c.smbus2_available():
            logger.warning("%s exists but smbus2 is not installed (pip install smbus2); skipping I2C sensors", dev_path)
            return None
        bus_factory = _i2c.open_bus
    try:
        bus = bus_factory(i2c_bus)
    except Exception as exc:
        logger.warning("cannot open I2C bus %s: %s", i2c_bus, exc)
        return None
    sensor = _i2c.probe_i2c(bus, names=names, addr=i2c_addr, owns_bus=True)
    if sensor is None:
        try:
            bus.close()
        except Exception:
            pass
        if explicit:
            logger.warning("no %s sensor answered on I2C bus %s%s", "/".join(names), i2c_bus, f" at 0x{i2c_addr:02X}" if i2c_addr else "")
    return sensor


def _dht_sensor(dht_pin: Any, *, explicit: bool) -> Sensor | None:
    if dht_pin in (None, ""):
        if explicit:
            logger.warning("SENSOR_BACKEND=dht needs GPIO_DHT_PIN")
        return None
    if not _dht.is_available():
        if explicit:
            logger.warning("adafruit_dht not importable; install with pip install -e '.[pi]' (Raspberry Pi only)")
        return None
    try:
        return _dht.DhtSensor(dht_pin)
    except Exception as exc:
        logger.warning("DHT init on pin %s failed: %s", dht_pin, exc)
        return None


def get_sensor(
    backend: str = "auto",
    *,
    i2c_bus: int = 1,
    i2c_addr: int = 0,
    iio_device: str = "",
    dht_pin: Any = None,
    dev_dir: str | None = None,
    iio_root: str | None = None,
    bus_factory: Any = None,
) -> Sensor | None:
    """Return a ready sensor or None (callers report ``no_sensor``; never guess).

    ``dev_dir``/``iio_root``/``bus_factory`` exist for tests and odd layouts.
    """
    name = (backend or "auto").strip().lower()
    if name == "none":
        return None
    if name == "mock":
        return MockSensor()
    if name == "iio":
        return _iio_sensor(iio_device, iio_root, explicit=True)
    if name in _i2c.DRIVERS:
        return _i2c_sensor((name,), i2c_bus, i2c_addr, dev_dir, bus_factory, explicit=True)
    if name == "dht":
        return _dht_sensor(dht_pin, explicit=True)
    if name != "auto":
        logger.warning("unknown SENSOR_BACKEND=%r; choose one of %s — falling back to auto", backend, ", ".join(SENSOR_BACKENDS))

    sensor = _iio_sensor(iio_device, iio_root, explicit=bool(iio_device))
    if sensor is not None:
        logger.info("sensor: %s", sensor.name)
        return sensor
    sensor = _i2c_sensor(_i2c.PROBE_ORDER, i2c_bus, i2c_addr, dev_dir, bus_factory, explicit=False)
    if sensor is not None:
        logger.info("sensor: %s @ bus %s", sensor.name, i2c_bus)
        return sensor
    sensor = _dht_sensor(dht_pin, explicit=False)
    if sensor is not None:
        logger.info("sensor: %s on pin %s", sensor.name, dht_pin)
        return sensor
    logger.info("sensor: none detected (IIO, I2C bus %s, DHT) — readings will report no_sensor", i2c_bus)
    return None
