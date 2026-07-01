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
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

_SAFE_SSID_RE = re.compile(r"^[\w\s\-\.@#!]{1,32}$")
_SAFE_PASS_RE = re.compile(r"^[\x20-\x7E]{8,63}$")
_SAFE_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
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

    def __init__(self, country: str | None = None) -> None:
        # Regulatory country drives `iw reg set` so the radio is allowed to
        # transmit on the local channels. Validated because it is shelled out.
        c = (country or settings.wifi_country or "US").strip().upper()
        if not _SAFE_COUNTRY_RE.match(c):
            logger.warning("Invalid Wi-Fi country %r — defaulting to 'US'", c)
            c = "US"
        self._country = c
        # Nearby networks captured just before the AP came up. The single-radio
        # Pi can't scan while hosting its own hotspot, so the onboarding list is
        # served from this cache instead of an (empty) live scan.
        self._cached_networks: list[WiFiNetwork] = []
        self._ap_prefix = (settings.ap_ssid_prefix or "Incubator").strip()

    # ------------------------------------------------------------------
    # Radio readiness
    # ------------------------------------------------------------------

    def _prepare_radio(self) -> None:
        """Unblock the WLAN radio and set the regulatory domain.

        On RPi OS Bookworm the radio stays rfkill soft-blocked until a Wi-Fi
        country is set, which makes `nmcli ... hotspot` fail. This is
        best-effort: on a dev box without rfkill/iw the missing tools must not
        abort onboarding, so FileNotFoundError is downgraded to debug.
        """
        for cmd in (["rfkill", "unblock", "wlan"], ["iw", "reg", "set", self._country]):
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if res.returncode != 0:
                    logger.warning("%s failed (rc=%d): %s", " ".join(cmd), res.returncode, res.stderr.strip())
            except FileNotFoundError:
                logger.debug("%s not found — skipping radio prep", cmd[0])
            except Exception as exc:
                logger.warning("Error running %s: %s", " ".join(cmd), exc)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_raw(self) -> list[WiFiNetwork]:
        """Live nmcli scan of nearby networks (may be empty while the AP is up).

        Our own setup hotspot is filtered out so the operator is never offered
        their own device as a network to join.
        """
        try:
            result = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes", check=False)
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("WiFi scan failed: %s", exc)
            return []
        networks: list[WiFiNetwork] = []
        seen: set[str] = set()
        own = self._ap_prefix.lower()
        for line in result.stdout.splitlines():
            parts = line.split(":", maxsplit=2)
            if len(parts) < 2:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            if own and ssid.lower().startswith(own):   # skip our own hotspot
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

    def prescan(self) -> None:
        """Capture nearby networks WHILE the radio is free (before the AP).

        Called right before the hotspot is created, so the onboarding wizard can
        show the operator their real home/barn networks even though a live scan
        would return nothing once the single radio is busy hosting the AP.
        """
        nets = self._scan_raw()
        if nets:
            self._cached_networks = nets
            logger.info("Pre-scan cached %d nearby network(s) for onboarding", len(nets))

    def scan_networks(self) -> list[WiFiNetwork]:
        # Prefer a live scan; if the radio is busy hosting the AP (empty result),
        # fall back to the pre-scan captured before the hotspot came up.
        live = self._scan_raw()
        if live:
            return live
        if self._cached_networks:
            logger.info("Serving %d pre-scanned network(s) (radio busy hosting AP)", len(self._cached_networks))
            return list(self._cached_networks)
        return [
            WiFiNetwork(ssid="FarmNet-2.4G", strength=82, secure=True),
            WiFiNetwork(ssid="BarnOffice", strength=67, secure=True),
            WiFiNetwork(ssid="Guest", strength=41, secure=False),
        ]

    # ------------------------------------------------------------------
    # Hotspot (AP mode for onboarding)
    # ------------------------------------------------------------------

    def is_hotspot_up(self, ssid: str, secured: bool) -> bool:
        """True if our hotspot is already active with the SAME ssid + security.

        Lets start_hotspot be idempotent: if the AP the caller wants is already
        serving the phone, we must NOT tear it down (that disconnects the client
        mid-onboarding — the "kicked out the moment I hit setup" symptom).
        """
        try:
            active = _nmcli("-t", "-f", "NAME,STATE", "connection", "show", "--active", check=False)
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001
            return False
        is_active = False
        for line in active.stdout.splitlines():
            name, _, state = line.rpartition(":")
            if name == _HOTSPOT_CON_NAME and "activ" in state.lower():
                is_active = True
                break
        if not is_active:
            return False
        # Confirm the live SSID + security match what we would create.
        cur_ssid = _nmcli("-g", "802-11-wireless.ssid", "connection", "show", _HOTSPOT_CON_NAME, check=False).stdout.strip()
        cur_keymgmt = _nmcli("-g", "802-11-wireless-security.key-mgmt", "connection", "show", _HOTSPOT_CON_NAME, check=False).stdout.strip()
        cur_secured = bool(cur_keymgmt)
        return cur_ssid == ssid and cur_secured == secured

    def start_hotspot(self, ssid: str, password: str) -> bool:
        """Create an NM hotspot connection and activate it (idempotent)."""
        try:
            ssid = _safe_ssid(ssid)
            password = _safe_pass(password)
            # Idempotent: if the exact AP we want is already up, leave it running
            # so a still-connected phone is never dropped.
            if self.is_hotspot_up(ssid, bool(password)):
                logger.info("Hotspot '%s' already up (%s) — leaving it running", ssid, "secured" if password else "open")
                return True
            # Bookworm soft-blocks the radio until a country is set — unblock it
            # and apply the regulatory domain before asking nmcli for an AP.
            self._prepare_radio()
            # Grab the list of nearby networks NOW, while wlan0 is still free.
            # Once the AP is up the single radio can't scan, so this is the only
            # chance to give the onboarding wizard a real network list.
            self.prescan()
            # Purge EVERY stale AP profile first — not just our fixed con-name.
            # Earlier images, NetworkManager's own "Hotspot" profile, and old
            # device-id-suffixed SSIDs leave saved AP connections in
            # /etc/NetworkManager/system-connections that auto-activate, so the
            # phone sees several "Incubator-XXXX" networks (often locked with an
            # old key). Collapse them to the single open AP we're about to start.
            self._purge_ap_connections(ssid)
            # Build the AP connection EXPLICITLY. Do NOT use `nmcli device wifi
            # hotspot` for the open case: with no password it auto-generates a
            # random WPA2 key and brings the AP up *locked* — which is exactly
            # why the setup network kept asking for a password. Creating the
            # connection by hand lets us leave the security setting off entirely
            # (= a genuinely open network) or add WPA-PSK only when a password
            # is configured.
            add = _nmcli(
                "connection", "add", "type", "wifi",
                "ifname", "wlan0", "con-name", _HOTSPOT_CON_NAME,
                "autoconnect", "no", "ssid", ssid,
                check=False,
            )
            if add.returncode != 0:
                logger.error("nmcli add hotspot failed (rc=%d): %s", add.returncode, add.stderr.strip())
                return False
            mods = [
                "802-11-wireless.mode", "ap",
                "802-11-wireless.band", "bg",
                "ipv4.method", "shared",
            ]
            # Only attach security when a password is set; otherwise the
            # connection has no wifi-sec at all and broadcasts OPEN.
            if password:
                mods += [
                    "802-11-wireless-security.key-mgmt", "wpa-psk",
                    "802-11-wireless-security.psk", password,
                ]
            _nmcli("connection", "modify", _HOTSPOT_CON_NAME, *mods, check=False)
            result = _nmcli("connection", "up", _HOTSPOT_CON_NAME, check=False)
            if result.returncode == 0:
                logger.info("Hotspot '%s' started (%s)", ssid, "secured" if password else "open")
                return True
            logger.error("nmcli up hotspot failed (rc=%d): %s", result.returncode, result.stderr.strip())
        except ValueError as exc:
            logger.warning("Hotspot rejected — invalid credentials: %s", exc)
        except FileNotFoundError:
            logger.debug("nmcli not found — hotspot start simulated")
            return True  # Allow onboarding to proceed in dev mode
        except Exception as exc:
            logger.warning("Unexpected error starting hotspot: %s", exc)
        return False

    def _purge_ap_connections(self, ssid: str) -> None:
        """Delete every saved Wi-Fi AP profile so exactly one SSID can exist.

        Matches two ways, because a stale locked twin can survive either check:
          * by NAME — our con-name, NetworkManager's default "Hotspot", anything
            containing "incubator"/"hotspot", or the SSID prefix; and
          * by MODE — any wifi connection whose 802-11-wireless.mode is "ap",
            regardless of what it is named.
        Best-effort and never fatal.
        """
        prefix = ssid.split("-", 1)[0].lower() if ssid else "incubator"
        try:
            res = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show", check=False)
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not list connections for purge: %s", exc)
            return
        for line in res.stdout.splitlines():
            # NAME may contain ':'; TYPE is the last field.
            name, _, ctype = line.rpartition(":")
            if not name or ("wireless" not in ctype and "wifi" not in ctype):
                continue
            low = name.lower()
            name_match = (
                low == _HOTSPOT_CON_NAME
                or "hotspot" in low
                or "incubator" in low
                or (prefix and low.startswith(prefix))
            )
            if not name_match and not self._is_ap_mode(name):
                continue
            _nmcli("connection", "down", name, check=False)
            _nmcli("connection", "delete", name, check=False)
            logger.info("Purged stale AP connection '%s'", name)

    def _is_ap_mode(self, name: str) -> bool:
        """True if the named wifi connection is an access-point profile."""
        try:
            mode = _nmcli("-g", "802-11-wireless.mode", "connection", "show", name, check=False).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        return mode.lower() == "ap"

    def network_debug(self) -> dict:
        """Snapshot of NM connections + active Wi-Fi APs for diagnostics."""
        info: dict[str, Any] = {"connections": [], "active_aps": [], "nmcli": True}
        try:
            conns = _nmcli("-t", "-f", "NAME,UUID,TYPE,ACTIVE", "connection", "show", check=False)
            for line in conns.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 4:
                    info["connections"].append(
                        {"name": parts[0], "uuid": parts[1], "type": parts[2], "active": parts[3] == "yes"}
                    )
            aps = _nmcli("-t", "-f", "ACTIVE,SSID,MODE,SECURITY", "device", "wifi", "list", check=False)
            for line in aps.stdout.splitlines():
                parts = line.split(":")
                if len(parts) >= 4 and parts[0] == "yes":
                    info["active_aps"].append(
                        {"ssid": parts[1], "mode": parts[2], "security": parts[3] or "open"}
                    )
        except FileNotFoundError:
            info["nmcli"] = False
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)
        return info

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
