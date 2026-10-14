"""Chip/line resolution shared by the libgpiod v1 and v2 backends.

Resolution rules (see the spec):

* ``PinSpec.chip`` given -> that chip (+ offset, or the named line on it).
* ``PinSpec.name`` given -> search every ``/dev/gpiochip*`` for that line name.
* bare offset -> the ``chip`` hint passed to the backend if any; else search the
  chips for a line named ``GPIO<offset>`` (Raspberry Pi convention: this makes
  BCM numbers work on a Pi 4, Pi 5 and Zero regardless of which gpiochip index
  the pinctrl block got); else ``gpiochip0``.

Lookups are cached per backend instance.
"""

from __future__ import annotations

import logging
import os
import threading

from .base import GpioBackend, PinSpec
from .chips import list_gpio_chips, normalize_chip_path

logger = logging.getLogger(__name__)


class GpiodCommon(GpioBackend):
    def __init__(self, chip: str = "", dev_dir: str | None = None) -> None:
        self._chip_hint = (chip or "").strip()
        self._dev_dir = dev_dir
        self._name_cache: dict[str, tuple[str, int]] = {}
        self._lock = threading.RLock()

    # -- to be provided by the version-specific backend ------------------
    def _find_line_on_chip(self, chip_path: str, name: str) -> int | None:
        """Return the offset of the line called ``name`` on ``chip_path`` or None."""
        raise NotImplementedError

    # -- shared ------------------------------------------------------------
    def chip_paths(self) -> list[str]:
        return list_gpio_chips(self._dev_dir)

    def _default_chip(self) -> str:
        if self._chip_hint:
            return normalize_chip_path(self._chip_hint, self._dev_dir)
        chips = self.chip_paths()
        preferred = normalize_chip_path("gpiochip0", self._dev_dir)
        if preferred in chips or not chips:
            return preferred
        return chips[0]

    def find_line(self, name: str, chip: str | None = None) -> tuple[str, int] | None:
        """Locate a line by name; returns ``(chip_path, offset)`` or None."""
        cache_key = f"{chip or ''}|{name}"
        with self._lock:
            hit = self._name_cache.get(cache_key)
        if hit is not None:
            return hit
        if chip:
            candidates = [normalize_chip_path(chip, self._dev_dir)]
        else:
            candidates = self.chip_paths()
            if self._chip_hint:
                hinted = normalize_chip_path(self._chip_hint, self._dev_dir)
                candidates = [hinted] + [c for c in candidates if c != hinted]
        for path in candidates:
            try:
                offset = self._find_line_on_chip(path, name)
            except Exception as exc:  # unreadable chip: keep looking
                logger.debug("line lookup %s on %s failed: %s", name, path, exc)
                continue
            if offset is not None:
                found = (path, int(offset))
                with self._lock:
                    self._name_cache[cache_key] = found
                return found
        return None

    def resolve(self, pin: PinSpec) -> tuple[str, int]:
        """Turn a PinSpec into ``(chip_path, offset)`` or raise LookupError."""
        if pin.name and pin.offset is None:
            found = self.find_line(pin.name, pin.chip)
            if found is None:
                where = pin.chip or ", ".join(self.chip_paths()) or "no gpiochip devices"
                raise LookupError(f"no GPIO line named {pin.name!r} on {where}")
            return found
        assert pin.offset is not None
        if pin.chip:
            return normalize_chip_path(pin.chip, self._dev_dir), pin.offset
        if self._chip_hint:
            return normalize_chip_path(self._chip_hint, self._dev_dir), pin.offset
        found = self.find_line(f"GPIO{pin.offset}")
        if found is not None:
            return found
        return self._default_chip(), pin.offset
