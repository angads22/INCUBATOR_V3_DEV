from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_SAFE_SSID_RE = re.compile(r"^[\w\s\-\.@#!]{1,32}$")
_SAFE_PASS_RE = re.compile(r"^[\x20-\x7E]{0,63}$")


def _validate_ssid(ssid: str) -> str:
    """Raise ValueError if ssid contains characters unsafe for nmcli args."""
    if not _SAFE_SSID_RE.match(ssid):
        raise ValueError(f"SSID contains unsafe characters: {ssid!r}")
    return ssid


def _validate_password(password: str) -> str:
    """Raise ValueError if password contains non-printable ASCII characters."""
    if password and not _SAFE_PASS_RE.match(password):
        raise ValueError("Wi-Fi password contains unsafe characters")
    return password


@dataclass(frozen=True)
class WiFiNetwork:
    ssid: str
    strength: int
    secure: bool


class WiFiService:
    """Local-first Wi-Fi/AP manager with safe fallbacks.

    Uses nmcli when present; otherwise returns mock values so onboarding UI works
    on developer machines.
    """

    def scan_networks(self) -> list[WiFiNetwork]:
        try:
            out = subprocess.check_output(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            networks: list[WiFiNetwork] = []
            for line in out.splitlines():
                ssid, signal, security = (line.split(":", maxsplit=2) + ["", "", ""])[:3]
                if not ssid.strip():
                    continue
                networks.append(WiFiNetwork(ssid=ssid.strip(), strength=int(signal or 0), secure=bool(security.strip())))
            return networks[:20]
        except Exception:
            return [
                WiFiNetwork(ssid="FarmNet-2.4G", strength=82, secure=True),
                WiFiNetwork(ssid="BarnOffice", strength=67, secure=True),
                WiFiNetwork(ssid="Guest", strength=41, secure=False),
            ]

    def start_hotspot(self, hotspot_ssid: str, hotspot_pass: str) -> bool:
        try:
            subprocess.check_call(
                ["nmcli", "device", "wifi", "hotspot", "ssid", hotspot_ssid, "password", hotspot_pass],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    def stop_hotspot(self) -> None:
        try:
            subprocess.check_call(["nmcli", "connection", "down", "Hotspot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return

    def connect_client(self, ssid: str, password: str) -> bool:
        try:
            ssid = _validate_ssid(ssid)
            password = _validate_password(password)
            cmd = ["nmcli", "device", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False
