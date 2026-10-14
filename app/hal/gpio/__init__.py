"""GPIO backends: libgpiod (v2/v1), RPi.GPIO, sysfs, mock.

    from app.hal.gpio import get_gpio_backend, parse_pin
    gpio = get_gpio_backend()                      # auto-detect
    heater = gpio.request_output(parse_pin("17"), initial=True)
    heater.set(False)

Nothing here imports a hardware library at import time.
"""

from __future__ import annotations

import logging
import re

from . import chips as _chips
from . import gpiod_v1 as _v1
from . import gpiod_v2 as _v2
from . import rpi as _rpi
from . import sysfs as _sysfs
from .base import GpioBackend, GpioBackendUnavailable, InputLine, OutputLine, PinSpec, Pull
from .chips import list_gpio_chips
from .mock import MockGpioBackend

logger = logging.getLogger(__name__)

__all__ = [
    "GpioBackend",
    "GpioBackendUnavailable",
    "InputLine",
    "MockGpioBackend",
    "OutputLine",
    "PinSpec",
    "Pull",
    "available_backends",
    "get_gpio_backend",
    "list_gpio_chips",
    "parse_pin",
]

BACKEND_NAMES = ("auto", "gpiod", "rpi", "sysfs", "mock")

_CHIP_PREFIX = re.compile(r"^(?P<chip>(?:/dev/)?gpiochip\d+|\d+(?=:))\s*:\s*(?P<rest>.+)$", re.IGNORECASE)
_LINE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def parse_pin(spec: "int | str | PinSpec") -> PinSpec:
    """Parse ``17``, ``"17"``, ``"gpiochip0:17"``, ``"/dev/gpiochip1:5"``, ``"GPIO17"``, ``"PA12"``.

    A bare number is a line offset (the BCM number on a Raspberry Pi). A chip
    prefix may also be followed by a line name (``gpiochip1:PA12``).
    """
    if isinstance(spec, PinSpec):
        return spec
    if isinstance(spec, bool):
        raise ValueError("pin spec must be a number or string, not bool")
    if isinstance(spec, int):
        return PinSpec(offset=spec)
    if spec is None:
        raise ValueError("pin spec is None")
    text = str(spec).strip()
    if not text:
        raise ValueError("empty pin spec")
    chip: str | None = None
    m = _CHIP_PREFIX.match(text)
    if m:
        chip = m.group("chip")
        if chip.isdigit():
            chip = f"gpiochip{chip}"
        text = m.group("rest").strip()
    if text.isdigit():
        return PinSpec(chip=chip, offset=int(text))
    if text.lower().startswith("0x"):
        try:
            return PinSpec(chip=chip, offset=int(text, 16))
        except ValueError:
            pass
    if _LINE_NAME.match(text):
        return PinSpec(chip=chip, name=text)
    raise ValueError(f"unrecognised pin spec {spec!r} (want 17, gpiochip0:17, /dev/gpiochip1:5, GPIO17 or PA12)")


def available_backends(*, dev_dir: str | None = None, sysfs_root: str | None = None) -> dict[str, bool]:
    """Which concrete backends could be constructed right now (for diagnostics)."""
    has_chips = bool(list_gpio_chips(dev_dir))
    return {
        "gpiod": has_chips and (_v2.is_available() or _v1.is_available()),
        "rpi": _rpi.is_available(),
        "sysfs": _sysfs.is_available(sysfs_root),
        "mock": True,
    }


def get_gpio_backend(
    name: str = "auto",
    chip: str = "",
    *,
    dev_dir: str | None = None,
    sysfs_root: str | None = None,
) -> GpioBackend:
    """Build a GPIO backend.

    ``auto`` tries, in order: libgpiod (v2 API, else v1) when a ``/dev/gpiochip*``
    exists -> RPi.GPIO -> sysfs (when ``/sys/class/gpio/export`` is writable)
    -> mock. An explicit name that is unavailable raises
    :class:`GpioBackendUnavailable` (a RuntimeError) with a hint; callers that
    run with ``GPIO_MOCK=true`` fall back to ``mock`` themselves.
    """
    wanted = (name or "auto").strip().lower().replace("-", "_")
    chip = (chip or "").strip()

    if wanted == "auto":
        chips = list_gpio_chips(dev_dir)
        if chips:
            if _v2.is_available():
                return _v2.GpiodV2Backend(chip=chip, dev_dir=dev_dir)
            if _v1.is_available():
                return _v1.GpiodV1Backend(chip=chip, dev_dir=dev_dir)
            logger.info("GPIO: %d gpiochip device(s) but no python gpiod module; trying other backends", len(chips))
        if _rpi.is_available():
            return _rpi.RpiGpioBackend(chip=chip)
        if _sysfs.is_available(sysfs_root):
            return _sysfs.SysfsGpioBackend(chip=chip, root=sysfs_root)
        logger.info("GPIO: no gpiod / RPi.GPIO / sysfs access available — using the in-memory mock backend")
        return MockGpioBackend(chip=chip)

    if wanted == "mock":
        return MockGpioBackend(chip=chip)

    if wanted in ("gpiod", "gpiod_v2", "gpiod2", "gpiod_v1", "gpiod1"):
        chips = list_gpio_chips(dev_dir)
        if not chips:
            raise GpioBackendUnavailable(
                f"GPIO backend 'gpiod' requested but no {(dev_dir or _chips.DEV_DIR)}/gpiochip* device exists "
                "(is the GPIO controller enabled in the device tree? are you running on the board?)"
            )
        if wanted in ("gpiod", "gpiod_v2", "gpiod2") and _v2.is_available():
            return _v2.GpiodV2Backend(chip=chip, dev_dir=dev_dir)
        if wanted in ("gpiod", "gpiod_v1", "gpiod1") and _v1.is_available():
            return _v1.GpiodV1Backend(chip=chip, dev_dir=dev_dir)
        raise GpioBackendUnavailable(
            "GPIO backend 'gpiod' requested but the python gpiod module is not importable: "
            "pip install 'gpiod>=2.1' (needs libgpiod headers) or apt install python3-libgpiod "
            "(and create the venv with --system-site-packages)"
        )

    if wanted == "rpi":
        if not _rpi.is_available():
            raise GpioBackendUnavailable(
                "GPIO backend 'rpi' requested but RPi.GPIO is not importable (pip install -e '.[pi]' on a Raspberry Pi)"
            )
        return _rpi.RpiGpioBackend(chip=chip)

    if wanted == "sysfs":
        if not _sysfs.is_available(sysfs_root):
            raise GpioBackendUnavailable(
                f"GPIO backend 'sysfs' requested but {(sysfs_root or _sysfs.SYSFS_ROOT)}/export is not writable "
                "(run as root, or enable CONFIG_GPIO_SYSFS; the kernel may have removed it — prefer gpiod)"
            )
        return _sysfs.SysfsGpioBackend(chip=chip, root=sysfs_root)

    raise GpioBackendUnavailable(f"unknown GPIO backend {name!r}; choose one of {', '.join(BACKEND_NAMES)}")
