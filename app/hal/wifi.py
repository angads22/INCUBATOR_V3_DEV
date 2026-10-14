"""Wi-Fi interface discovery without assuming ``wlan0``."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Callable

from . import _util

logger = logging.getLogger(__name__)

SYSFS_NET = "/sys/class/net"
DEFAULT_IFACE = "wlan0"


def wifi_interfaces(sysfs_net: str | None = None) -> list[str]:
    """Interface names under ``/sys/class/net`` that have ``wireless/`` or ``phy80211/``."""
    root = sysfs_net or SYSFS_NET
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        base = os.path.join(root, name)
        if os.path.isdir(os.path.join(base, "wireless")) or os.path.isdir(os.path.join(base, "phy80211")):
            found.append(name)
    # Prefer the conventional name first when several exist.
    found.sort(key=lambda n: (not n.startswith("wl"), n))
    return found


def wifi_interface(sysfs_net: str | None = None, env: dict | None = None) -> str:
    """``WIFI_IFACE`` env if set, else the first detected interface, else ``wlan0``."""
    environ = os.environ if env is None else env
    forced = (environ.get("WIFI_IFACE") or "").strip()
    if forced:
        return forced
    found = wifi_interfaces(sysfs_net)
    return found[0] if found else DEFAULT_IFACE


def phy_name(iface: str, sysfs_net: str | None = None) -> str | None:
    return _util.read_stripped(os.path.join(sysfs_net or SYSFS_NET, iface, "phy80211", "name")) or None


def parse_iw_modes(output: str) -> list[str]:
    """Pull the entries under "Supported interface modes:" out of ``iw phy info``."""
    modes: list[str] = []
    in_block = False
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("Supported interface modes"):
            in_block = True
            continue
        if in_block:
            if line.startswith("*"):
                modes.append(line.lstrip("*").strip())
            elif line and not line.startswith("*"):
                in_block = False
    return modes


def supports_ap_mode(
    iface: str | None = None,
    sysfs_net: str | None = None,
    run: Callable[..., Any] | None = None,
) -> bool | None:
    """True/False from ``iw phy <phy> info``; None when ``iw`` is missing or unsure."""
    iface = iface or wifi_interface(sysfs_net)
    phy = phy_name(iface, sysfs_net)
    cmd = ["iw", "phy", phy, "info"] if phy else ["iw", "list"]
    runner = run or subprocess.run
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=10, check=False)
    except FileNotFoundError:
        logger.debug("iw not installed; cannot tell whether %s supports AP mode", iface)
        return None
    except Exception as exc:
        logger.debug("iw failed: %s", exc)
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    output = getattr(proc, "stdout", "") or ""
    if "Supported interface modes" not in output:
        return None
    return "AP" in parse_iw_modes(output)
