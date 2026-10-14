"""
Wi-Fi manager built on NetworkManager (nmcli).

Runs on any Linux SBC that uses NetworkManager: the interface name comes from
the HAL (``WIFI_IFACE`` env, else the first wireless interface, else wlan0).
User-supplied strings are validated before they reach nmcli; every argument is
passed as an argv list (no shell), so validation exists to catch typos early
and keep nmcli's parser happy — not as an injection barrier.

AP mode:  nmcli creates a "hotspot" connection on the Wi-Fi interface.
          A single-radio board (Pi Zero 2 W) cannot be in AP and client mode
          simultaneously, so the AP is torn down before connecting as a client.

Availability:
  * ``INCUBATOR_NMCLI_BIN`` overrides the nmcli executable. The test suite
    points it at a path that does not exist so a developer's real
    NetworkManager is never touched.
  * Before any state-changing call the service probes ``nmcli general``
    (3 s timeout, cached 60 s). When NetworkManager is unreachable AND the
    process runs in mock mode (``GPIO_MOCK=true`` or ``INCUBATOR_WIFI_MOCK=true``)
    the call simulates success so the onboarding UI works on a laptop. On a
    real device the same condition logs an ERROR and the call fails honestly
    (False / [] / None) so nothing ever pretends to be online.
  * Every subprocess call carries a timeout; nmcli's own 90 s connect wait is
    bounded by ``NMCLI_CONNECT_TIMEOUT``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..hal.wifi import supports_ap_mode, wifi_interface

logger = logging.getLogger(__name__)

_SAFE_PASS_RE = re.compile(r"^[\x20-\x7E]{8,63}$")
_SAFE_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_HOTSPOT_CON_NAME = "incubator-hotspot"
# NetworkManager's own `nmcli dev wifi hotspot` profile name (compared lower-case).
_NM_DEFAULT_HOTSPOT_NAME = "hotspot"

NMCLI_BIN_ENV = "INCUBATOR_NMCLI_BIN"
WIFI_MOCK_ENV = "INCUBATOR_WIFI_MOCK"
NMCLI_TIMEOUT = 30.0            # ordinary queries / modifies
NMCLI_CONNECT_TIMEOUT = 100.0   # `connection up` / `device wifi connect` (nmcli itself waits up to 90 s)
NM_PROBE_TIMEOUT = 3.0
NM_PROBE_TTL = 60.0
TOOL_TIMEOUT = 10.0             # rfkill / iw

SSID_MAX_BYTES = 32

# Applied to every client profile so the home router registers the device's
# hostname (DHCP option 12) and mDNS answers for <hostname>.local.
_CLIENT_PROFILE_TUNING = ("connection.mdns", "2", "ipv4.dhcp-send-hostname", "yes")

_PROBE_ARGS = ("-t", "-f", "RUNNING", "general")


def valid_ssid(ssid: object) -> bool:
    """1–32 bytes of UTF-8, printable, with at least one non-blank character.

    Any printable non-control character is fine ("John's iPhone",
    "Smith Family (2.4GHz)", "Barn+Office", "Wi-Fi 5G: Home", accented or CJK
    names): arguments reach nmcli as an argv list, never through a shell.
    """
    if not isinstance(ssid, str) or not ssid.strip():
        return False
    try:
        size = len(ssid.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    if not 1 <= size <= SSID_MAX_BYTES:
        return False
    return all(ch.isprintable() for ch in ssid)


def _safe_ssid(ssid: str) -> str:
    if not valid_ssid(ssid):
        raise ValueError(f"SSID must be 1-32 bytes of printable text: {ssid!r}")
    return ssid


def _safe_pass(password: str) -> str:
    if password and not _SAFE_PASS_RE.match(password):
        raise ValueError("WiFi password must be 8-63 printable ASCII characters")
    return password


def _timeout_for(args: tuple[str, ...]) -> float:
    """Pick the subprocess timeout from the nmcli verb being run."""
    if tuple(args) == _PROBE_ARGS:
        return NM_PROBE_TIMEOUT
    if args[:2] == ("connection", "up") or args[:3] == ("device", "wifi", "connect"):
        return NMCLI_CONNECT_TIMEOUT
    return NMCLI_TIMEOUT


def _nmcli(*args: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run nmcli with a timeout.

    ``INCUBATOR_NMCLI_BIN`` swaps the executable while argv[0] stays "nmcli",
    so log lines (and test doubles that match on argv) read the same.
    """
    kwargs: dict[str, Any] = {}
    override = (os.environ.get(NMCLI_BIN_ENV) or "").strip()
    if override:
        kwargs["executable"] = override
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=_timeout_for(args) if timeout is None else timeout,
        **kwargs,
    )


# ----------------------------------------------------------------------------
# NetworkManager availability probe (cached)
# ----------------------------------------------------------------------------

_nm_probe: dict[str, Any] = {"ts": 0.0, "ok": None}
_nm_probe_lock = threading.Lock()


def nm_available(force: bool = False) -> bool:
    """True when ``nmcli general`` answers with rc 0, i.e. the NetworkManager
    daemon is running and reachable. The answer is cached for NM_PROBE_TTL s."""
    now = time.time()
    with _nm_probe_lock:
        cached = _nm_probe["ok"]
        if not force and cached is not None and 0 <= now - _nm_probe["ts"] < NM_PROBE_TTL:
            return bool(cached)
    try:
        res = _nmcli(*_PROBE_ARGS, check=False)
        ok = res.returncode == 0
    except FileNotFoundError:
        ok = False
    except subprocess.TimeoutExpired:
        logger.warning("nmcli did not answer within %.0fs — treating NetworkManager as unavailable", NM_PROBE_TIMEOUT)
        ok = False
    except Exception as exc:  # noqa: BLE001
        logger.debug("NetworkManager probe failed: %s", exc)
        ok = False
    with _nm_probe_lock:
        _nm_probe["ok"] = ok
        _nm_probe["ts"] = now
    return ok


def reset_nm_probe_cache() -> None:
    """Forget the cached probe result (tests; after installing NetworkManager)."""
    with _nm_probe_lock:
        _nm_probe["ok"] = None
        _nm_probe["ts"] = 0.0


def wifi_mock_allowed() -> bool:
    """Simulated networking is acceptable on a dev box only, never on an appliance."""
    return bool(settings.gpio_mock) or (os.environ.get(WIFI_MOCK_ENV) or "").strip().lower() == "true"


@dataclass(frozen=True)
class WiFiNetwork:
    ssid: str
    strength: int
    secure: bool


class WiFiService:
    """NetworkManager wrapper — safe, local-first, with explicit dev fallbacks."""

    def __init__(self, country: str | None = None, *, iface: str | None = None, ap_ip: str | None = None) -> None:
        # Regulatory country drives `iw reg set` so the radio is allowed to
        # transmit on the local channels. Validated because it is shelled out.
        c = (country or settings.wifi_country or "US").strip().upper()
        if not _SAFE_COUNTRY_RE.match(c):
            logger.warning("Invalid Wi-Fi country %r — defaulting to 'US'", c)
            c = "US"
        self._country = c
        # Interface from the HAL: WIFI_IFACE env wins, else the first wireless
        # interface found, else "wlan0".
        self._iface = (iface or settings.wifi_iface or wifi_interface() or "wlan0").strip()
        # Fixed AP address. NM's "shared" method only defaults to 10.42.0.1 for
        # the FIRST shared connection, so the address is set explicitly.
        self._ap_ip = (ap_ip or settings.ap_ip or "10.42.0.1").strip()
        # Nearby networks captured just before the AP came up. The single-radio
        # Pi can't scan while hosting its own hotspot, so the onboarding list is
        # served from this cache instead of an (empty) live scan.
        self._cached_networks: list[WiFiNetwork] = []
        self._ap_prefix = (settings.ap_ssid_prefix or "Incubator").strip()
        self._own_ap_re = re.compile(rf"^{re.escape(self._ap_prefix)}-[A-Za-z0-9]{{4}}$", re.IGNORECASE)
        # SSID of the AP this process last brought up (None once stopped).
        self._hotspot_ssid: str | None = None
        # Mock-mode bookkeeping so a dev laptop still shows a coherent state.
        self._mock_hotspot_ssid: str | None = None
        self._mock_connected_ssid: str | None = None
        self._last_nm_error_log = 0.0

    @property
    def iface(self) -> str:
        return self._iface

    @property
    def ap_ip(self) -> str:
        return self._ap_ip

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def _nm_or_mock(self, what: str) -> bool | None:
        """True → NetworkManager is reachable, do the real thing.
        False → unreachable but mock mode is allowed: simulate.
        None → unreachable on a real device: ERROR logged (rate-limited), caller fails."""
        if nm_available():
            return True
        if wifi_mock_allowed():
            logger.debug("%s: NetworkManager unavailable — simulated (mock mode)", what)
            return False
        now = time.time()
        if now - self._last_nm_error_log >= NM_PROBE_TTL:
            logger.error(
                "%s: NetworkManager is not available (is network-manager installed and running?) — "
                "Wi-Fi management disabled until it is",
                what,
            )
            self._last_nm_error_log = now
        return None

    def _is_own_ap_ssid(self, ssid: str | None) -> bool:
        """Is this SSID the setup AP this device broadcasts (now or by name)?"""
        if not ssid:
            return False
        if self._hotspot_ssid and ssid == self._hotspot_ssid:
            return True
        return bool(self._own_ap_re.match(ssid))

    def _is_ap_profile_name(self, name: str, ssid: str = "") -> bool:
        """Profiles that are OURS by name: the fixed con-name, NetworkManager's
        default "Hotspot", or ``<prefix>-XXXX`` (4 alphanumerics). Anything else
        — "Johns Hotspot", "IncubatorHome" — is a home network and is kept."""
        clean = name.strip()
        low = clean.lower()
        if low in (_HOTSPOT_CON_NAME, _NM_DEFAULT_HOTSPOT_NAME):
            return True
        if self._own_ap_re.match(clean):
            return True
        prefix = ssid.rsplit("-", 1)[0].strip() if ssid and "-" in ssid else ""
        if prefix and prefix.lower() != self._ap_prefix.lower():
            return bool(re.match(rf"^{re.escape(prefix)}-[A-Za-z0-9]{{4}}$", clean, re.IGNORECASE))
        return False

    # ------------------------------------------------------------------
    # Radio readiness
    # ------------------------------------------------------------------

    def _prepare_radio(self) -> None:
        """Unblock the WLAN radio and set the regulatory domain.

        On RPi OS Bookworm the radio stays rfkill soft-blocked until a Wi-Fi
        country is set, which makes `nmcli ... hotspot` fail. Best-effort: on
        a box without rfkill/iw the missing tools must not abort onboarding.
        """
        for cmd in (["rfkill", "unblock", "wlan"], ["iw", "reg", "set", self._country]):
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=TOOL_TIMEOUT)
                if res.returncode != 0:
                    logger.warning("%s failed (rc=%d): %s", " ".join(cmd), res.returncode, res.stderr.strip())
            except FileNotFoundError:
                logger.debug("%s not found — skipping radio prep", cmd[0])
            except subprocess.TimeoutExpired:
                logger.warning("%s timed out after %.0fs", " ".join(cmd), TOOL_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error running %s: %s", " ".join(cmd), exc)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_raw(self, rescan: str = "no") -> list[WiFiNetwork]:
        """nmcli list of nearby networks (may be empty while the AP is up).

        Our own setup hotspot is filtered out so the operator is never offered
        their own device as a network to join.
        """
        try:
            # rescan="no" reads NetworkManager's CACHED list — crucial while the
            # AP is up: forcing a rescan takes the single radio off AP duty and
            # drops the hotspot (kicks the phone off mid-setup). Only the pre-AP
            # prescan (radio free) passes rescan="yes".
            result = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", rescan, check=False)
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("WiFi scan failed: %s", exc)
            return []
        networks: list[WiFiNetwork] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split(":", maxsplit=2)
            if len(parts) < 2:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            if self._is_own_ap_ssid(ssid):   # skip our own hotspot
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
        nets = self._scan_raw(rescan="yes")   # radio is free here — safe to rescan
        if nets:
            self._cached_networks = nets
            logger.info("Pre-scan cached %d nearby network(s) for onboarding", len(nets))

    def scan_networks(self) -> list[WiFiNetwork]:
        # NEVER force a rescan here — this runs during the wizard while the AP is
        # up, and a rescan would drop the hotspot. Prefer the pre-scan cache
        # (captured before the AP), then NetworkManager's cached list.
        if self._cached_networks:
            return list(self._cached_networks)
        cached = self._scan_raw(rescan="no")
        if cached:
            return cached
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
        nm = self._nm_or_mock("is_hotspot_up")
        if nm is None:
            return False
        if nm is False:
            return self._mock_hotspot_ssid == ssid
        try:
            active = _nmcli("-t", "-f", "NAME,STATE", "connection", "show", "--active", check=False)
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
        except Exception:  # noqa: BLE001 — FileNotFoundError / timeout / anything: not up
            return False
        return cur_ssid == ssid and bool(cur_keymgmt) == secured

    def start_hotspot(self, ssid: str, password: str) -> bool:
        """Create an NM hotspot connection and activate it (idempotent)."""
        try:
            ssid = _safe_ssid(ssid)
            password = _safe_pass(password)
        except ValueError as exc:
            logger.warning("Hotspot rejected — invalid credentials: %s", exc)
            return False
        nm = self._nm_or_mock("start_hotspot")
        if nm is None:
            return False
        if nm is False:
            logger.info("Hotspot '%s' simulated (mock mode, no NetworkManager)", ssid)
            self._mock_hotspot_ssid = ssid
            self._mock_connected_ssid = None
            self._hotspot_ssid = ssid
            return True
        try:
            # Idempotent: if the exact AP we want is already up, leave it running
            # so a still-connected phone is never dropped.
            if self.is_hotspot_up(ssid, bool(password)):
                logger.info("Hotspot '%s' already up (%s) — leaving it running", ssid, "secured" if password else "open")
                self._hotspot_ssid = ssid
                return True
            # Bookworm soft-blocks the radio until a country is set — unblock it
            # and apply the regulatory domain before asking nmcli for an AP.
            self._prepare_radio()
            # Grab the list of nearby networks NOW, while the radio is still free.
            # Once the AP is up the single radio can't scan, so this is the only
            # chance to give the onboarding wizard a real network list.
            self.prescan()
            # Purge stale AP profiles (ours by name or any AP-mode profile) so
            # exactly one setup SSID exists. Home-network profiles are kept.
            self._purge_ap_connections(ssid)
            # Build the AP connection EXPLICITLY. Do NOT use `nmcli device wifi
            # hotspot` for the open case: with no password it auto-generates a
            # random WPA2 key and brings the AP up *locked*. Creating the
            # connection by hand lets us leave the security setting off entirely
            # (= a genuinely open network) or add WPA-PSK only when a password
            # is configured.
            add = _nmcli(
                "connection", "add", "type", "wifi",
                "ifname", self._iface, "con-name", _HOTSPOT_CON_NAME,
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
                # NM only defaults a shared connection to 10.42.0.1 when it is the
                # FIRST one; pin the address so the captive portal, dnsmasq
                # hijack and every printed URL agree.
                "ipv4.addresses", f"{self._ap_ip}/24",
                "ipv6.method", "disabled",
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
                logger.info("Hotspot '%s' started on %s (%s)", ssid, self._iface, "secured" if password else "open")
                self._hotspot_ssid = ssid
                return True
            logger.error("nmcli up hotspot failed (rc=%d): %s", result.returncode, result.stderr.strip())
        except subprocess.TimeoutExpired as exc:
            logger.error("nmcli timed out while starting the hotspot: %s", exc)
        except FileNotFoundError:
            logger.error("nmcli disappeared while starting the hotspot")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error starting hotspot: %s", exc)
        return False

    def _purge_ap_connections(self, ssid: str) -> None:
        """Delete every saved Wi-Fi AP profile so exactly one setup SSID can exist.

        A profile is purged when it is ours BY NAME (the fixed con-name,
        NetworkManager's default "Hotspot", or ``<prefix>-XXXX``) or when its
        802-11-wireless.mode is "ap" regardless of name. Client profiles whose
        name merely contains "hotspot"/"incubator" ("Johns Hotspot") are home
        networks and are left alone. Best-effort and never fatal.
        """
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
            if not (self._is_ap_profile_name(name, ssid) or self._is_ap_mode(name)):
                continue
            try:
                _nmcli("connection", "down", name, check=False)
                _nmcli("connection", "delete", name, check=False)
                logger.info("Purged stale AP connection '%s'", name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not purge '%s': %s", name, exc)

    def _is_ap_mode(self, name: str) -> bool:
        """True if the named wifi connection is an access-point profile."""
        try:
            mode = _nmcli("-g", "802-11-wireless.mode", "connection", "show", name, check=False).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        return mode.lower() == "ap"

    def network_debug(self) -> dict:
        """Snapshot of NM connections + active Wi-Fi APs for diagnostics."""
        info: dict[str, Any] = {
            "connections": [], "active_aps": [], "nmcli": True,
            "nm_available": False, "iface": self._iface, "mock": wifi_mock_allowed(),
        }
        if not nm_available():
            info["nmcli"] = False
            return info
        info["nm_available"] = True
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
        self._hotspot_ssid = None
        nm = self._nm_or_mock("stop_hotspot")
        if nm is None:
            return
        if nm is False:
            self._mock_hotspot_ssid = None
            logger.debug("Hotspot stop simulated (mock mode)")
            return
        try:
            _nmcli("connection", "down", _HOTSPOT_CON_NAME, check=False)
            _nmcli("connection", "delete", _HOTSPOT_CON_NAME, check=False)
            logger.info("Hotspot stopped")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error stopping hotspot: %s", exc)

    def hotspot_status(self) -> dict:
        """Return whether the hotspot connection is currently active."""
        if not nm_available():
            if wifi_mock_allowed() and self._mock_hotspot_ssid:
                return {"active": True, "connection": _HOTSPOT_CON_NAME, "mock": True}
            return {"active": False}
        try:
            if self._hotspot_connection_active():
                return {"active": True, "connection": _HOTSPOT_CON_NAME}
        except Exception:  # noqa: BLE001
            pass
        return {"active": False}

    def _hotspot_connection_active(self) -> bool:
        """Is our AP connection active in NetworkManager right now?"""
        try:
            result = _nmcli("-t", "-f", "NAME,STATE", "connection", "show", "--active", check=False)
        except Exception:  # noqa: BLE001
            return False
        for line in result.stdout.splitlines():
            name, _, state = line.rpartition(":")
            if name == _HOTSPOT_CON_NAME and "activ" in state.lower():
                return True
        return False

    def has_ap_support(self) -> bool | None:
        """Whether the radio can host an AP (None = unknown, e.g. `iw` missing)."""
        try:
            return supports_ap_mode(self._iface)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Client mode
    # ------------------------------------------------------------------

    def connect_client(self, ssid: str, password: str) -> bool:
        """Connect the Wi-Fi interface to a network as a client.

        This runs seconds after the setup AP was torn down, and the single
        radio has not scanned since before the AP came up — NetworkManager's
        scan cache is stale/empty. ``nmcli device wifi connect`` refuses to
        join an SSID it cannot see ("No network with SSID ..."), which used to
        make the first join after onboarding fail almost every time. So:

          1. rescan now that the radio is free, and wait for the SSID to show;
          2. try the normal ``device wifi connect`` (creates a saved profile);
          3. if that still fails, build an explicit connection profile and
             activate it — profile activation probes for the network directly
             and does not depend on the scan cache at all.
        """
        try:
            ssid = _safe_ssid(ssid)
            if password:
                password = _safe_pass(password)
        except ValueError as exc:
            logger.warning("WiFi connect rejected — invalid credentials: %s", exc)
            return False
        nm = self._nm_or_mock("connect_client")
        if nm is None:
            return False
        if nm is False:
            logger.info("WiFi connect to '%s' simulated (mock mode, no NetworkManager)", ssid)
            self._mock_hotspot_ssid = None
            self._mock_connected_ssid = ssid
            return True
        try:
            self._prepare_radio()
            self._wait_for_ssid_visible(ssid, timeout=20.0)
            cmd = ["device", "wifi", "connect", ssid, "ifname", self._iface]
            if password:
                cmd += ["password", password]
            result = _nmcli(*cmd, check=False)
            if result.returncode == 0:
                self._tune_client_profile(ssid)
                logger.info("Connected to WiFi '%s'", ssid)
                return True
            logger.warning(
                "WiFi connect failed (rc=%d): %s — retrying via explicit profile",
                result.returncode, result.stderr.strip(),
            )
            return self._connect_via_profile(ssid, password)
        except subprocess.TimeoutExpired as exc:
            logger.warning("nmcli timed out while joining '%s': %s", ssid, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error connecting to WiFi: %s", exc)
        return False

    def _tune_client_profile(self, name: str) -> None:
        """mDNS on + send our hostname with DHCP so routers list the device by name."""
        try:
            _nmcli("connection", "modify", name, *_CLIENT_PROFILE_TUNING, check=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not tune profile '%s': %s", name, exc)

    def _wait_for_ssid_visible(self, ssid: str, timeout: float = 20.0) -> bool:
        """Trigger a rescan (radio is free now) and wait for ``ssid`` to appear.

        Best-effort: returns False on timeout, but callers proceed anyway — the
        profile fallback can associate without the SSID ever entering the list.
        """
        _nmcli("device", "wifi", "rescan", check=False)
        deadline = time.time() + max(2.0, timeout)
        while time.time() < deadline:
            if any(n.ssid == ssid for n in self._scan_raw(rescan="no")):
                return True
            time.sleep(2)
        logger.info("SSID '%s' not visible after rescan — will probe directly", ssid)
        return False

    def _connect_via_profile(self, ssid: str, password: str) -> bool:
        """Join by building an explicit NM profile and activating it.

        Activation probes for the network itself (``hidden yes`` forces active
        scanning), so it works even when the scan cache never caught up. On
        success the profile persists with autoconnect for future boots; on
        failure it is deleted so a dead profile never fights the fallback AP.
        """
        _nmcli("connection", "delete", ssid, check=False)   # clear any stale twin
        add = _nmcli(
            "connection", "add", "type", "wifi",
            "ifname", self._iface, "con-name", ssid,
            "autoconnect", "yes", "ssid", ssid,
            check=False,
        )
        if add.returncode != 0:
            logger.error("nmcli add profile for '%s' failed: %s", ssid, add.stderr.strip())
            return False
        mods = ["802-11-wireless.hidden", "yes", *_CLIENT_PROFILE_TUNING]
        if password:
            mods += [
                "802-11-wireless-security.key-mgmt", "wpa-psk",
                "802-11-wireless-security.psk", password,
            ]
        _nmcli("connection", "modify", ssid, *mods, check=False)
        up = _nmcli("connection", "up", ssid, check=False)
        if up.returncode == 0 and self._confirm_connected(ssid, timeout=20.0):
            logger.info("Connected to WiFi '%s' via explicit profile", ssid)
            return True
        logger.warning("Profile activation for '%s' failed: %s", ssid, up.stderr.strip())
        _nmcli("connection", "delete", ssid, check=False)
        return False

    def _confirm_connected(self, ssid: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + max(2.0, timeout)
        while time.time() < deadline:
            if self.get_connected_ssid() == ssid:
                return True
            time.sleep(2)
        return False

    def try_saved_connection(self, ssid: str, timeout: float = 25.0) -> bool:
        """Bring up the SAVED NetworkManager profile for ``ssid`` and wait.

        Used by the connectivity watchdog to retry the operator's Wi-Fi after a
        fallback to the setup AP (NM stores the credentials from the original
        join, so no password is needed here). Returns True once associated.
        """
        try:
            ssid = _safe_ssid(ssid)
        except ValueError:
            return False
        nm = self._nm_or_mock("try_saved_connection")
        if nm is None:
            return False
        if nm is False:
            self._mock_hotspot_ssid = None
            self._mock_connected_ssid = ssid
            return True
        try:
            _nmcli("connection", "up", "id", ssid, check=False)
        except subprocess.TimeoutExpired:
            logger.warning("nmcli timed out bringing up saved profile '%s'", ssid)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not activate saved profile '%s': %s", ssid, exc)
            return False
        deadline = time.time() + max(5.0, timeout)
        while time.time() < deadline:
            if self.get_connected_ssid() == ssid:
                return True
            time.sleep(2)
        return False

    def has_saved_profile(self, ssid: str) -> bool:
        """Does NetworkManager still hold a profile named ``ssid``?

        The watchdog uses this to stop retrying a network whose profile was
        deleted (it would fail instantly and flap forever otherwise).
        """
        if not valid_ssid(ssid):
            return False
        nm = self._nm_or_mock("has_saved_profile")
        if nm is None:
            return False
        if nm is False:
            return True
        try:
            res = _nmcli("-g", "connection.id", "connection", "show", ssid, check=False)
        except Exception:  # noqa: BLE001
            return False
        return res.returncode == 0

    def forget_all_networks(self) -> int:
        """Delete every saved client Wi-Fi profile (factory reset).

        The setup-AP profile is managed by start/stop_hotspot and is left in
        place so a reset performed over the AP never drops the operator.
        """
        nm = self._nm_or_mock("forget_all_networks")
        if nm is None:
            return 0
        if nm is False:
            self._mock_connected_ssid = None
            logger.info("forget_all_networks simulated (mock mode)")
            return 0
        try:
            res = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show", check=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list connections to forget: %s", exc)
            return 0
        count = 0
        for line in res.stdout.splitlines():
            name, _, ctype = line.rpartition(":")
            if not name or ("wireless" not in ctype and "wifi" not in ctype):
                continue
            if name == _HOTSPOT_CON_NAME:
                continue
            try:
                _nmcli("connection", "down", name, check=False)
                _nmcli("connection", "delete", name, check=False)
                count += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not delete profile '%s': %s", name, exc)
        logger.info("Forgot %d saved Wi-Fi network(s)", count)
        return count

    def get_connected_ssid(self) -> str | None:
        """Return the SSID of the home/barn network we are joined to, or None.

        Our own setup AP shows up as the "active" network while we host it, so
        it is excluded (by the SSID we broadcast / the ``<prefix>-XXXX`` shape,
        and by checking whether the hotspot connection is active). The
        watchdog must never believe the device is online when it is only
        serving its own hotspot.
        """
        if not nm_available():
            return self._mock_connected_ssid if wifi_mock_allowed() else None
        try:
            result = _nmcli("-t", "-f", "ACTIVE,SSID", "dev", "wifi", check=False)
            active: str | None = None
            for line in result.stdout.splitlines():
                parts = line.split(":", maxsplit=1)
                if len(parts) == 2 and parts[0].strip().lower() == "yes":
                    active = parts[1].strip() or None
                    break
            if not active:
                return None
            if self._is_own_ap_ssid(active) or self._hotspot_connection_active():
                return None
            return active
        except Exception:  # noqa: BLE001
            return None
