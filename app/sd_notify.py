"""Tiny sd_notify client — the app-level watchdog layer.

Redundancy ladder for a wedged incubator, from inner to outer:
  1. ``Restart=always``      — covers the process EXITING;
  2. **this module**         — covers the process HANGING: the service units
     run ``Type=notify`` + ``WatchdogSec=``; the app sends READY=1 once up
     and WATCHDOG=1 from its poll loops. If the loops stop (deadlock, stuck
     thread, wedged event loop) the pings stop and systemd restarts the
     service — something Restart= alone can never see;
  3. hardware watchdog       — covers the KERNEL/PID1 hanging (reboots).

Pure stdlib (AF_UNIX datagram to $NOTIFY_SOCKET, abstract-namespace aware).
On a dev box NOTIFY_SOCKET is unset and every call is a no-op; nothing here
may ever raise into the caller.
"""

from __future__ import annotations

import logging
import os
import socket

logger = logging.getLogger(__name__)


def sd_notify(state: str) -> bool:
    """Send one sd_notify state string. True if it was actually sent."""
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return False
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]  # abstract socket namespace
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(addr)
            sock.sendall(state.encode())
        return True
    except Exception as exc:  # noqa: BLE001 — the watchdog must never hurt the app
        logger.debug("sd_notify(%r) failed: %s", state, exc)
        return False


def sd_ready() -> bool:
    """Tell systemd the service is up (required once under Type=notify)."""
    return sd_notify("READY=1")


def sd_watchdog_ping() -> bool:
    """Heartbeat for WatchdogSec — call from the service's main loop."""
    return sd_notify("WATCHDOG=1")
