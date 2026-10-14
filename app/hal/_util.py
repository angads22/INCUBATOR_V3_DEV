"""Small helpers shared by the HAL modules (stdlib only)."""

from __future__ import annotations

import importlib
import logging
import os
from types import ModuleType

logger = logging.getLogger(__name__)


def optional_import(name: str) -> ModuleType | None:
    """Import ``name`` and return the module, or None when it is unavailable.

    Hardware libraries (gpiod, smbus2, RPi.GPIO, adafruit_dht) are never
    imported at module load time anywhere in ``app.hal`` — only through this
    helper, inside the function or class that needs them — so the package
    imports cleanly on a laptop or in CI. Any exception raised while importing
    (not only ImportError: broken native extensions raise OSError/RuntimeError)
    counts as "unavailable".
    """
    try:
        return importlib.import_module(name)
    except Exception as exc:
        logger.debug("optional module %s unavailable: %s", name, exc)
        return None


def read_text(path: str, default: str | None = None) -> str | None:
    """Read a small sysfs/procfs file; return ``default`` on any error."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def read_stripped(path: str, default: str | None = None) -> str | None:
    text = read_text(path)
    if text is None:
        return default
    return text.replace("\x00", "").strip()


def write_text(path: str, value: str) -> bool:
    """Write a sysfs attribute. Returns False (and logs at debug) on failure."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
        return True
    except OSError as exc:
        logger.debug("write %s <- %r failed: %s", path, value, exc)
        return False


def read_int(path: str, default: int | None = None) -> int | None:
    text = read_stripped(path)
    if text is None or text == "":
        return default
    try:
        return int(text, 0)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return default


def read_float(path: str, default: float | None = None) -> float | None:
    text = read_stripped(path)
    if text is None or text == "":
        return default
    try:
        return float(text)
    except ValueError:
        return default


def exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except OSError:
        return False
