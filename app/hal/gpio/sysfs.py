"""Deprecated-but-everywhere ``/sys/class/gpio`` backend.

Global GPIO numbers are ``<chip base> + <offset>``. The base of ``/dev/gpiochipN``
is found by matching the sysfs ``gpiochip<base>`` directory whose ``device/``
contains the character device directory ``gpiochipN``; when that link is not
available we fall back to the Nth sysfs chip ordered by base (and say so).
Bias (pull-up/down) cannot be set through sysfs — the board's default applies.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from .. import _util
from .base import GpioBackend, InputLine, OutputLine, PinSpec, Pull
from .chips import chip_index

logger = logging.getLogger(__name__)

SYSFS_ROOT = "/sys/class/gpio"
_GPIO_NAME = re.compile(r"^GPIO(\d+)$", re.IGNORECASE)


def is_available(root: str | None = None) -> bool:
    export = os.path.join(root or SYSFS_ROOT, "export")
    try:
        return os.path.exists(export) and os.access(export, os.W_OK)
    except OSError:
        return False


class _SysfsLine(OutputLine, InputLine):
    def __init__(self, backend: "SysfsGpioBackend", number: int, exported_by_us: bool) -> None:
        self._backend = backend
        self.number = number
        self._dir = os.path.join(backend.root, f"gpio{number}")
        self._exported_by_us = exported_by_us
        self._released = False

    def set(self, value: bool) -> None:
        _util.write_text(os.path.join(self._dir, "value"), "1" if value else "0")

    def get(self) -> bool:
        text = _util.read_stripped(os.path.join(self._dir, "value"), "0") or "0"
        return text != "0"

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._exported_by_us:
            _util.write_text(os.path.join(self._backend.root, "unexport"), str(self.number))
        self._backend._forget(self)


class SysfsGpioBackend(GpioBackend):
    name = "sysfs"

    def __init__(self, chip: str = "", root: str | None = None, settle_seconds: float = 0.5) -> None:
        self.root = root or SYSFS_ROOT
        if not is_available(self.root):
            raise RuntimeError(f"{self.root}/export is not writable (need root or a udev rule)")
        self._chip_hint = (chip or "").strip()
        self._settle = settle_seconds
        self._base_cache: dict[int, int] = {}
        self._lines: list[_SysfsLine] = []
        self._lock = threading.Lock()

    # -- chip base resolution ---------------------------------------------------
    def _sysfs_chips(self) -> list[tuple[int, str]]:
        """``[(base, dir)]`` for every ``gpiochip*`` directory under root, by base."""
        out: list[tuple[int, str]] = []
        try:
            names = os.listdir(self.root)
        except OSError:
            return out
        for name in names:
            if not name.startswith("gpiochip"):
                continue
            path = os.path.join(self.root, name)
            base = _util.read_int(os.path.join(path, "base"))
            if base is None:
                tail = name[len("gpiochip"):]
                base = int(tail) if tail.isdigit() else None
            if base is not None:
                out.append((base, path))
        out.sort()
        return out

    def chip_base(self, chip: str) -> int:
        idx = chip_index(chip)
        if idx is None:
            raise ValueError(f"sysfs backend needs gpiochip<N>, got {chip!r}")
        with self._lock:
            if idx in self._base_cache:
                return self._base_cache[idx]
        chips = self._sysfs_chips()
        base: int | None = None
        for b, path in chips:
            if os.path.isdir(os.path.join(path, "device", f"gpiochip{idx}")):
                base = b
                break
        if base is None and idx < len(chips):
            base = chips[idx][0]
            if len(chips) > 1 or base != 0:
                logger.warning("sysfs gpio: assuming /dev/gpiochip%d has base %d (heuristic; set GPIO_CHIP or use gpiod)", idx, base)
        if base is None:
            base = 0
            if chips or idx:
                logger.warning("sysfs gpio: could not find the base of gpiochip%d; assuming 0", idx)
        with self._lock:
            self._base_cache[idx] = base
        return base

    def global_number(self, pin: PinSpec) -> int:
        if pin.offset is not None:
            offset = pin.offset
        else:
            m = _GPIO_NAME.match(pin.name or "")
            if not m:
                raise ValueError(f"sysfs backend cannot look up line names ({pin.name!r}); use <chip>:<offset>")
            offset = int(m.group(1))
        chip = pin.chip or self._chip_hint or "gpiochip0"
        return self.chip_base(chip) + offset

    def chips(self) -> list[dict]:
        return [
            {
                "path": path,
                "base": base,
                "label": _util.read_stripped(os.path.join(path, "label")),
                "num_lines": _util.read_int(os.path.join(path, "ngpio")),
            }
            for base, path in self._sysfs_chips()
        ]

    # -- requests ----------------------------------------------------------------
    def _export(self, number: int) -> tuple[str, bool]:
        gpio_dir = os.path.join(self.root, f"gpio{number}")
        exported = False
        if not os.path.isdir(gpio_dir):
            exported = _util.write_text(os.path.join(self.root, "export"), str(number))
        # udev may take a moment to make the new attributes writable.
        deadline = time.monotonic() + self._settle
        direction = os.path.join(gpio_dir, "direction")
        while not (os.path.exists(direction) and os.access(direction, os.W_OK)):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        return gpio_dir, exported

    def request_output(self, pin: PinSpec, initial: bool, consumer: str = "incubator") -> OutputLine:
        number = self.global_number(pin)
        gpio_dir, exported = self._export(number)
        # "high"/"low" set direction and the initial level atomically.
        if not _util.write_text(os.path.join(gpio_dir, "direction"), "high" if initial else "low"):
            raise RuntimeError(f"cannot configure gpio{number} as output under {self.root}")
        line = _SysfsLine(self, number, exported)
        with self._lock:
            self._lines.append(line)
        return line

    def request_input(self, pin: PinSpec, pull: Pull = "up", consumer: str = "incubator") -> InputLine:
        number = self.global_number(pin)
        gpio_dir, exported = self._export(number)
        if not _util.write_text(os.path.join(gpio_dir, "direction"), "in"):
            raise RuntimeError(f"cannot configure gpio{number} as input under {self.root}")
        if pull != "none":
            logger.debug("sysfs gpio cannot set pull-%s on gpio%d; relying on the board default", pull, number)
        line = _SysfsLine(self, number, exported)
        with self._lock:
            self._lines.append(line)
        return line

    def _forget(self, line: _SysfsLine) -> None:
        with self._lock:
            if line in self._lines:
                self._lines.remove(line)

    def close(self) -> None:
        with self._lock:
            lines = list(self._lines)
        for line in lines:
            line.release()
