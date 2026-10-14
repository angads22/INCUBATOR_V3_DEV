"""Linux IIO sensors (``/sys/bus/iio/devices/iio:deviceN``).

Covers the kernel ``dht11`` driver (DHT11/DHT22 via a device-tree overlay) and
any I2C sensor the kernel drives itself (sht3x, bme280, htu21, si7020, hts221,
aht10 ...). Channel files come in two flavours:

* ``in_temp_input`` — processed value; per the IIO ABI in milli-units
  (37500 -> 37.5 degC), but some drivers report plain units;
* ``in_temp_raw`` [+ ``in_temp_offset``] [+ ``in_temp_scale``] —
  ``value = (raw + offset) * scale``.

When no scale file exists and ``|value| > 200`` the number is treated as
milli-units (no incubator runs at 375 degC or 55000 %RH).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .. import _util
from .base import Sensor, reading

logger = logging.getLogger(__name__)

IIO_ROOT = "/sys/bus/iio/devices"
_DEV_RE = re.compile(r"^iio:device(\d+)$")

TEMP_CHANNELS = ("in_temp_input", "in_temp_raw", "in_temp_ambient_input", "in_temp_ambient_raw", "in_temp0_input", "in_temp0_raw")
HUM_CHANNELS = ("in_humidityrelative_input", "in_humidityrelative_raw", "in_humidityrelative0_input", "in_humidityrelative0_raw")


def iio_root(root: str | None = None) -> str:
    return root or IIO_ROOT


def list_iio_devices(root: str | None = None) -> list[str]:
    base = iio_root(root)
    try:
        names = os.listdir(base)
    except OSError:
        return []
    found = sorted((int(m.group(1)), n) for m in (_DEV_RE.match(n) for n in names) if m for n in [m.group(0)])
    return [os.path.join(base, name) for _, name in found]


def _channel_file(dev: str, names: tuple[str, ...]) -> str | None:
    for n in names:
        if _util.exists(os.path.join(dev, n)):
            return os.path.join(dev, n)
    return None


def device_channels(dev: str) -> dict[str, str | None]:
    return {"temperature": _channel_file(dev, TEMP_CHANNELS), "humidity": _channel_file(dev, HUM_CHANNELS)}


def find_iio_device(root: str | None = None, *, require_humidity: bool = True) -> str | None:
    """First iio:device exposing temperature (and humidity unless told otherwise)."""
    for dev in list_iio_devices(root):
        ch = device_channels(dev)
        if ch["temperature"] and (ch["humidity"] or not require_humidity):
            return dev
    return None


def resolve_iio_device(spec: str, root: str | None = None) -> str | None:
    """``iio:device0`` / ``0`` / full path / device *name* (e.g. ``dht11``) -> path."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if os.path.sep in spec:
        return spec if os.path.isdir(spec) else None
    if spec.isdigit():
        spec = f"iio:device{spec}"
    path = os.path.join(iio_root(root), spec)
    if os.path.isdir(path):
        return path
    for dev in list_iio_devices(root):
        if (_util.read_stripped(os.path.join(dev, "name")) or "").lower() == spec.lower():
            return dev
    return None


def read_channel(dev: str, channel_file: str) -> float | None:
    """Apply the IIO scale/offset rules described in the module docstring."""
    value = _util.read_float(channel_file)
    if value is None:
        return None
    base = os.path.basename(channel_file)
    if base.endswith("_raw"):
        stem = base[: -len("_raw")]
        offset = _util.read_float(os.path.join(dev, f"{stem}_offset"))
        scale = _util.read_float(os.path.join(dev, f"{stem}_scale"))
        if scale is None:
            # Some drivers publish one scale per channel *type* (in_temp_scale)
            # even when the channel is indexed (in_temp0_raw).
            generic = re.sub(r"\d+$", "", stem)
            scale = _util.read_float(os.path.join(dev, f"{generic}_scale"))
            if offset is None:
                offset = _util.read_float(os.path.join(dev, f"{generic}_offset"))
        if offset is not None:
            value += offset
        if scale is not None:
            # IIO scale yields milli-units for temp/humidity in most drivers.
            value *= scale
            if abs(value) > 200:
                value /= 1000.0
            return value
    if abs(value) > 200:
        value /= 1000.0
    return value


class IioSensor(Sensor):
    def __init__(self, device: str) -> None:
        self.device = device
        self.device_name = _util.read_stripped(os.path.join(device, "name")) or os.path.basename(device)
        self.name = f"iio:{self.device_name}"
        self._channels = device_channels(device)
        if not self._channels["temperature"]:
            raise RuntimeError(f"{device} exposes no temperature channel")

    def read(self) -> dict[str, Any]:
        t_file = self._channels["temperature"]
        h_file = self._channels["humidity"]
        try:
            temp = read_channel(self.device, t_file) if t_file else None
            hum = read_channel(self.device, h_file) if h_file else None
        except Exception as exc:  # pragma: no cover - defensive
            return reading(self.name, ok=False, error=f"iio read failed: {exc}")
        if temp is None:
            # The dht11 driver returns EIO on a bad checksum; that shows up as an
            # unreadable file for a moment. Report "no reading", never a guess.
            return reading(self.name, ok=False, error="iio read failed (EIO/timeout)")
        if hum is None and h_file:
            return reading(self.name, ok=False, temperature_c=temp, error="iio humidity read failed")
        return reading(self.name, ok=True, temperature_c=temp, humidity_pct=hum)
