"""Cheap, never-raising board detection for any Linux SBC."""

from __future__ import annotations

import logging
import os
import platform
import re
from dataclasses import asdict, dataclass, field

from . import _util
from .gpio.chips import list_gpio_chips

logger = logging.getLogger(__name__)

_I2C_RE = re.compile(r"^i2c-(\d+)$")


@dataclass(frozen=True)
class BoardInfo:
    model: str = "Generic Linux"
    family: str = "generic"  # raspberrypi | armbian | generic
    is_raspberry_pi: bool = False
    arch: str = ""
    distro_id: str = ""
    distro_version: str = ""
    boot_config: str | None = None
    cpu_serial: str | None = None
    machine_id: str | None = None
    has_systemd: bool = False
    gpio_chips: list[str] = field(default_factory=list)
    i2c_buses: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def short_model(self) -> str:
        return self.model.split("\n")[0][:64]


def _p(root: str, *parts: str) -> str:
    return os.path.join(root, *[p.lstrip("/") for p in parts])


def _read_model(root: str) -> str:
    for rel in ("proc/device-tree/model", "sys/firmware/devicetree/base/model"):
        text = _util.read_stripped(_p(root, rel))
        if text:
            return text
    # x86 boxes: DMI product name is the closest thing to a model string.
    text = _util.read_stripped(_p(root, "sys/class/dmi/id/product_name"))
    if text and text.lower() not in ("to be filled by o.e.m.", "system product name", "default string"):
        return text
    return "Generic Linux"


def _read_os_release(root: str) -> tuple[str, str]:
    distro_id = ""
    version = ""
    for rel in ("etc/os-release", "usr/lib/os-release"):
        text = _util.read_text(_p(root, rel))
        if not text:
            continue
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if key.strip() == "ID":
                distro_id = value
            elif key.strip() == "VERSION_ID":
                version = value
        if distro_id or version:
            break
    return distro_id, version


def _read_cpu_serial(root: str) -> str | None:
    text = _util.read_text(_p(root, "proc/cpuinfo"))
    if not text:
        return None
    for line in text.splitlines():
        if line.lower().startswith("serial"):
            _, _, value = line.partition(":")
            value = value.strip()
            if value and set(value) != {"0"}:
                return value
    return None


def _list_i2c_buses(root: str) -> list[int]:
    try:
        names = os.listdir(_p(root, "dev"))
    except OSError:
        return []
    buses = sorted(int(m.group(1)) for m in (_I2C_RE.match(n) for n in names) if m)
    return buses


def detect_board(root: str = "/") -> BoardInfo:
    """Inspect procfs/sysfs under ``root`` (a parameter so tests can fake a tree)."""
    try:
        model = _read_model(root)
        distro_id, distro_version = _read_os_release(root)
        is_pi = "raspberry pi" in model.lower()
        is_armbian = _util.exists(_p(root, "etc/armbian-release"))
        if is_pi:
            family = "raspberrypi"
        elif is_armbian:
            family = "armbian"
        else:
            family = "generic"

        boot_config: str | None = None
        candidates: list[str] = []
        if is_pi:
            candidates = ["boot/firmware/config.txt", "boot/config.txt"]
        elif is_armbian:
            candidates = ["boot/armbianEnv.txt"]
        for rel in candidates:
            path = _p(root, rel)
            if _util.exists(path):
                boot_config = path if root in ("/", "") else "/" + rel
                break

        gpio_chips = [os.path.basename(p) for p in list_gpio_chips(_p(root, "dev"))]
        return BoardInfo(
            model=model,
            family=family,
            is_raspberry_pi=is_pi,
            arch=platform.machine() or "",
            distro_id=distro_id,
            distro_version=distro_version,
            boot_config=boot_config,
            cpu_serial=_read_cpu_serial(root),
            machine_id=_util.read_stripped(_p(root, "etc/machine-id")) or None,
            has_systemd=os.path.isdir(_p(root, "run/systemd/system")),
            gpio_chips=gpio_chips,
            i2c_buses=_list_i2c_buses(root),
        )
    except Exception as exc:  # never let diagnostics take the app down
        logger.debug("detect_board failed: %s", exc)
        try:
            arch = platform.machine() or ""
        except Exception:
            arch = ""
        return BoardInfo(arch=arch)
