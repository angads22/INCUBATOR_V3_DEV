"""
WiFi manager for Raspberry Pi Zero 2W.

Uses NetworkManager (nmcli) which ships by default on Raspberry Pi OS (Bookworm).
All user-supplied strings are validated before being passed to nmcli to prevent
shell injection.

AP mode:  nmcli creates a "hotspot" connection on wlan0.
          The Pi Zero 2W has a single WiFi adapter — it cannot be in AP and
          client mode simultaneously.  The AP is torn down before connecting
          as a client.

Fallback: If nmcli is unavailable (dev machine) all methods return mock data
          so the onboarding UI still works.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SAFE_SSID_RE = re.compile(r"^[\w\s\-\.@#!]{1,32}$")
_SAFE_PASS_RE = re.compile(r"^[\x20-\x7E]{8,63}$")
_HOTSPOT_CON_NAME = "incubator-hotspot"


def _safe_ssid(ssid: str) -> str:
    if not _SAFE_SSID_RE.match(ssid):
        raise ValueError(f"SSID contains unsafe characters: {ssid!r}")
    return ssid


def _safe_pass(password: str) -> str:
    if password and not _SAFE_PASS_RE.match(password):
        raise ValueError("WiFi password must be 8-63 printable ASCII characters")
    return password


def _nmcli(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True,
        text=True,
        check=check,
    )


@dataclass(frozen=True)
class WiFiNetwork:
    ssid: str
    strength: int
    secure: bool


class WiFiService:
    """NetworkManager wrapper — safe, local-first, with dev fallbacks."""

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_networks(self) -> list[WiFiNetwork]:
        try:
            result = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes", check=False)
            networks: list[WiFiNetwork] = []
            seen: set[str] = set()
            for line in result.stdout.splitlines():
                parts = line.split(":", maxsplit=2)
                if len(parts) < 2:
                    continue
                ssid = parts[0].strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                try:
                    strength = int(parts[1].strip())
                except ValueError:
                    strength = 0
                secure = bool(len(parts) > 2 and parts[2].strip())
                networks.append(WiFiNetwork(ssid=ssid, strength=strength, secure=secure))
            networks.sort(key=lambda n: n.strength, reverse=True)
            return networks[:20]
        except FileNotFoundError:
            logger.debug("nmcli not found — returning mock WiFi networks")
        except Exception as exc:
            logger.warning("WiFi scan failed: %s", exc)
        return [
            WiFiNetwork(ssid="FarmNet-2.4G", strength=82, secure=True),
            WiFiNetwork(ssid="BarnOffice", strength=67, secure=True),
            WiFiNetwork(ssid="Guest", strength=41, secure=False),
        ]

    # ------------------------------------------------------------------
    # Hotspot (AP mode for onboarding)
    # ------------------------------------------------------------------

    def start_hotspot(self, ssid: str, password: str) -> bool:
        """Create an NM hotspot connection and activate it."""
        try:
            ssid = _safe_ssid(ssid)
            password = _safe_pass(password)
            # Delete any stale hotspot connection first
            _nmcli("connection", "delete", _HOTSPOT_CON_NAME, check=False)
            result = _nmcli(
                "device", "wifi", "hotspot",
                "con-name", _HOTSPOT_CON_NAME,
                "ifname", "wlan0",
                "ssid", ssid,
                "password", password,
                check=False,
            )
            if result.returncode == 0:
                logger.info("Hotspot '%s' started", ssid)
                return True
            logger.warning("nmcli hotspot failed (rc=%d): %s", result.returncode, result.stderr.strip())
        except ValueError as exc:
            logger.warning("Hotspot rejected — invalid credentials: %s", exc)
        except FileNotFoundError:
            logger.debug("nmcli not found — hotspot start simulated")
            return True  # Allow onboarding to proceed in dev mode
        except Exception as exc:
            logger.warning("Unexpected error starting hotspot: %s", exc)
        return False

    def stop_hotspot(self) -> None:
        try:
            _nmcli("connection", "down", _HOTSPOT_CON_NAME, check=False)
            _nmcli("connection", "delete", _HOTSPOT_CON_NAME, check=False)
            logger.info("Hotspot stopped")
        except FileNotFoundError:
            logger.debug("nmcli not found — hotspot stop simulated")
        except Exception as exc:
            logger.warning("Error stopping hotspot: %s", exc)

    def hotspot_status(self) -> dict:
        """Return whether the hotspot connection is currently active."""
        try:
            result = _nmcli("-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active", check=False)
            for line in result.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[0] == _HOTSPOT_CON_NAME:
                    return {"active": True, "connection": _HOTSPOT_CON_NAME}
        except Exception:
            pass
        return {"active": False}

    # ------------------------------------------------------------------
    # Client mode
    # ------------------------------------------------------------------

    def connect_client(self, ssid: str, password: str) -> bool:
        """Connect wlan0 to a WiFi network as a client."""
        try:
            ssid = _safe_ssid(ssid)
            cmd = ["device", "wifi", "connect", ssid, "ifname", "wlan0"]
            if password:
                password = _safe_pass(password)
                cmd += ["password", password]
            result = _nmcli(*cmd, check=False)
            if result.returncode == 0:
                logger.info("Connected to WiFi '%s'", ssid)
                return True
            logger.warning("WiFi connect failed (rc=%d): %s", result.returncode, result.stderr.strip())
        except ValueError as exc:
            logger.warning("WiFi connect rejected — invalid credentials: %s", exc)
        except FileNotFoundError:
            logger.debug("nmcli not found — WiFi connect simulated")
            return True
        except Exception as exc:
            logger.warning("Unexpected error connecting to WiFi: %s", exc)
        return False

    def get_connected_ssid(self) -> str | None:
        """Return currently connected SSID, or None."""
        try:
            result = _nmcli("-t", "-f", "ACTIVE,SSID", "dev", "wifi", check=False)
            for line in result.stdout.splitlines():
                parts = line.split(":", maxsplit=1)
                if len(parts) == 2 and parts[0].strip().lower() == "yes":
                    return parts[1].strip() or None
        except Exception:
            pass
        return None
