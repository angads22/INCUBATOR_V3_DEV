"""gpiochip device discovery (shared by the gpiod backends and board detection).

``DEV_DIR`` is a module attribute so tests (and odd container layouts) can point
the HAL at a fake ``/dev``.
"""

from __future__ import annotations

import os
import re

DEV_DIR = "/dev"

_CHIP_RE = re.compile(r"^gpiochip(\d+)$")


def list_gpio_chips(dev_dir: str | None = None) -> list[str]:
    """Return the ``/dev/gpiochipN`` paths that exist, sorted by N."""
    base = dev_dir or DEV_DIR
    try:
        names = os.listdir(base)
    except OSError:
        return []
    found: list[tuple[int, str]] = []
    for name in names:
        m = _CHIP_RE.match(name)
        if m:
            found.append((int(m.group(1)), os.path.join(base, name)))
    found.sort()
    return [path for _, path in found]


def chip_index(chip: str) -> int | None:
    """``gpiochip4`` / ``/dev/gpiochip4`` / ``4`` -> 4; None when not that shape."""
    tail = os.path.basename(str(chip).strip())
    if tail.isdigit():
        return int(tail)
    m = _CHIP_RE.match(tail)
    return int(m.group(1)) if m else None


def normalize_chip_path(chip: str, dev_dir: str | None = None) -> str:
    """Turn any accepted chip spelling into a device path."""
    chip = str(chip).strip()
    base = dev_dir or DEV_DIR
    if os.path.sep in chip:
        return chip
    if chip.isdigit():
        return os.path.join(base, f"gpiochip{chip}")
    return os.path.join(base, chip)
