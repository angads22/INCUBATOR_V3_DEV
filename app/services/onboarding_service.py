"""
Boot-time onboarding orchestrator (any Linux SBC running NetworkManager).

On every startup this service checks whether the device is already configured.
If it is NOT (no Wi-Fi saved), it brings up an open Wi-Fi access point so the
operator can connect a phone and finish setup in the browser.

Flow:
  1. App starts → OnboardingService.boot(db) is called
  2. DeviceConfig has no wifi_ssid → start_hotspot(); setup mode is entered only
     once the AP is really up (a failed AP is retried from the watchdog)
  3. Hotspot SSID: "{ap_ssid_prefix}-{last4_of_device_id}"  e.g. "Incubator-A3F2"
  4. Phone joins the AP → captive sheet → wizard at http://{ap_ip}:8000/onboarding
  5. POST /onboarding/complete → OnboardingService.complete(ssid, password)
  6. Hotspot torn down → device joins the home network → normal operation

Never-unreachable invariant (ensure_reachable, called from the sensor poller):
the device is always on the home Wi-Fi OR its own AP. Long nmcli operations
(join / rejoin) run in a short-lived background thread guarded by a transition
lock, so the poller — and with it the systemd watchdog ping — is never starved,
and no two AP/join sequences ever interleave.

Feedback: ``on_event`` receives "setup_ready" (AP up), "connecting",
"connected" and "failed" so LEDs/buzzer can narrate the flow. It never raises.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DeviceConfig

if TYPE_CHECKING:
    from .wifi_service import WiFiService
    from .setup_mode_service import SetupModeService

logger = logging.getLogger(__name__)

APP_PORT = 8000

# How the AP was requested → the reason recorded in SetupModeService. Only
# "button_hold" / "auto_boot" / "factory_reset" count as physical presence.
_SOURCE_REASON = {
    "button": "button_hold",
    "web": "manual_trigger",
    "fallback": "fallback",
    "factory_reset": "factory_reset",
    "boot": "auto_boot",
}


# ----------------------------------------------------------------------------
# Address helpers (shared with device_service and the routes)
# ----------------------------------------------------------------------------

def short_hostname() -> str:
    """First label of the kernel hostname — what Avahi advertises as <name>.local."""
    try:
        name = socket.gethostname()
    except Exception:  # noqa: BLE001
        name = ""
    return (name or "").strip().split(".")[0] or "incubator"


def candidate_urls(hostname: str | None = None, port: int = APP_PORT) -> list[str]:
    """Durable URLs the wizard probes once the device is on the home network."""
    host = ((hostname or short_hostname()).strip().split(".")[0]) or "incubator"
    return [f"http://{host}.local:{port}", f"http://{host}:{port}", f"http://{host}.lan:{port}"]


LAN_IPS_TTL = 5.0
_lan_ips_cache: dict = {"ts": 0.0, "ips": []}


def lan_ips(*, now: float | None = None) -> list[str]:
    """Non-loopback IPv4 addresses of this host (cached a few seconds — the
    wizard pings every 3 s). Best effort, never raises."""
    now = time.time() if now is None else now
    if 0 <= now - _lan_ips_cache["ts"] < LAN_IPS_TTL:
        return list(_lan_ips_cache["ips"])
    ips = _lan_ips_uncached()
    _lan_ips_cache["ts"] = now
    _lan_ips_cache["ips"] = ips
    return list(ips)


def _lan_ips_uncached() -> list[str]:
    ips: list[str] = []
    try:
        res = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, check=False, timeout=3.0)
        if res.returncode == 0 and res.stdout.strip():
            for iface in json.loads(res.stdout):
                if not isinstance(iface, dict) or iface.get("ifname") == "lo":
                    continue
                for info in iface.get("addr_info") or []:
                    if info.get("family") != "inet" or info.get("scope") == "host":
                        continue
                    addr = str(info.get("local") or "")
                    if addr and not addr.startswith("127.") and addr not in ips:
                        ips.append(addr)
    except Exception as exc:  # noqa: BLE001 — iproute2 missing / odd output
        logger.debug("ip -j addr unavailable: %s", exc)
    if not ips:
        # No iproute2: ask the kernel which source address would route out.
        # A UDP connect() sends nothing on the wire.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("10.255.255.255", 1))
                addr = s.getsockname()[0]
            finally:
                s.close()
            if addr and not addr.startswith("127."):
                ips.append(addr)
        except Exception:  # noqa: BLE001
            pass
    return ips


class OnboardingService:
    def __init__(
        self,
        wifi_service: "WiFiService",
        setup_mode_service: "SetupModeService",
        ap_ssid_prefix: str,
        ap_password: str,
        ap_ip: str,
        auto_hotspot: bool = True,
        ap_password_resolver: Callable[[], str] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._wifi = wifi_service
        self._setup = setup_mode_service
        self._ap_ssid_prefix = ap_ssid_prefix
        self._ap_password = ap_password
        # When provided, this is the authoritative source of the setup-AP
        # password (read from the DB, default open). It overrides the static
        # ap_password so an OTA-updated unit never stays locked behind a stale
        # value baked into /etc/incubator.env.
        self._ap_password_resolver = ap_password_resolver
        self._ap_ip = ap_ip
        self._auto_hotspot = auto_hotspot
        self._on_event = on_event
        self._hotspot_active = False
        self._captive = None  # CaptivePortalResponder, lazily created with the hotspot
        # Seconds to keep the AP up after "finish" so the wizard's connecting
        # screen can load before the single radio switches to the home network.
        self._complete_grace_seconds = 12.0
        # ── Connectivity watchdog state (never-unreachable invariant) ──
        self._last_device_id = ""       # remembered for fallback AP naming
        self._fallback_active = False   # AP raised by the watchdog, not the user
        self._offline_polls = 0         # consecutive polls with no network at all
        self._polls_in_fallback = 0     # polls spent in fallback (drives rejoin retry)
        # ~4 polls * 30 s poll interval ≈ 2 min of zero-network before fallback.
        self._offline_polls_threshold = 4
        # Retry the saved Wi-Fi every ~20 polls (≈10 min) while in fallback.
        self._fallback_retry_polls = 20
        # An AP was requested (boot/button) but did not come up → the watchdog
        # retries it every poll until it does (or the device is online).
        self._ap_retry_source: str | None = None
        # Operator (button / wizard) session bookkeeping — monotonic seconds.
        self._clock: Callable[[], float] = time.monotonic
        self._manual_session_started_at: float | None = None
        self._last_activity_at: float | None = None
        # An idle operator session on a configured device returns to the saved
        # Wi-Fi after this long without any wizard request.
        self._manual_idle_timeout = 600.0
        # Saved SSID whose NetworkManager profile turned out to be gone (log once).
        self._missing_profile_logged: str | None = None
        # One transition (AP flip / join / rejoin) at a time, across the poller
        # thread, request threads, background tasks and the button thread.
        self._lock = threading.RLock()
        self._transitions_active = 0
        self._transition_thread: threading.Thread | None = None
        # ensure_reachable waits this long for a background transition before
        # returning: long enough for a mocked/instant one to settle, far below
        # the poll interval and the systemd watchdog for a real nmcli join.
        self._transition_join_timeout = 1.0

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _password(self) -> str:
        if self._ap_password_resolver is not None:
            try:
                return self._ap_password_resolver() or ""
            except Exception as exc:  # noqa: BLE001 — never block onboarding on this
                logger.warning("AP password resolver failed (%s) — using open network", exc)
                return ""
        return self._ap_password

    def _emit(self, name: str) -> None:
        """Hand a feedback event to LEDs/buzzer. Never raises, never blocks setup."""
        if self._on_event is None:
            return
        try:
            self._on_event(name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("feedback event %s failed: %s", name, exc)

    def _setup_is_active(self) -> bool:
        fn = getattr(self._setup, "is_setup_mode", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:  # noqa: BLE001
            return False

    def _connected_ssid(self) -> str | None:
        try:
            return self._wifi.get_connected_ssid()
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_connected_ssid failed: %s", exc)
            return None

    def _try_saved(self, ssid: str) -> bool:
        try:
            return bool(self._wifi.try_saved_connection(ssid))
        except Exception as exc:  # noqa: BLE001
            logger.warning("try_saved_connection('%s') failed: %s", ssid, exc)
            return False

    def _saved_profile_exists(self, ssid: str) -> bool:
        fn = getattr(self._wifi, "has_saved_profile", None)
        if not callable(fn):
            return True
        try:
            return bool(fn(ssid))
        except Exception:  # noqa: BLE001
            return True

    def _ap_url(self) -> str:
        return f"http://{self._ap_ip}:{APP_PORT}"

    def _make_ssid(self, device_id: str) -> str:
        suffix = device_id[-4:].upper() if device_id else "XXXX"
        return f"{self._ap_ssid_prefix}-{suffix}"

    # ------------------------------------------------------------------
    # Transition lock — one AP flip / join at a time
    # ------------------------------------------------------------------

    @contextmanager
    def _transition(self) -> Iterator[None]:
        self._lock.acquire()
        self._transitions_active += 1
        try:
            yield
        finally:
            self._transitions_active -= 1
            self._lock.release()

    def _transition_in_progress(self) -> bool:
        thread = self._transition_thread
        return thread is not None and thread.is_alive()

    def transition_in_progress(self) -> bool:
        """True while a join/rejoin or AP flip is running (poller or request)."""
        return self._transitions_active > 0 or self._transition_in_progress()

    def _start_transition(self, target: Callable[[], None]) -> bool:
        """Run ``target`` under the transition lock on a short-lived daemon
        thread. Returns False if one is already running."""
        if self._transition_in_progress():
            return False
        thread = threading.Thread(target=self._run_transition, args=(target,), name="wifi-transition", daemon=True)
        self._transition_thread = thread
        thread.start()
        # A mocked/instant transition settles before we return; a real nmcli
        # join keeps running in the background and the poller carries on.
        thread.join(self._transition_join_timeout)
        return True

    def _run_transition(self, target: Callable[[], None]) -> None:
        try:
            with self._transition():
                target()
        except Exception as exc:  # noqa: BLE001 — a failed transition must never kill the thread noisily
            logger.warning("Network transition failed: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def boot(self, db: Session, device_id: str) -> None:
        """Called once at app startup. Starts the hotspot if the device needs setup."""
        self._last_device_id = device_id or self._last_device_id
        config = db.scalar(select(DeviceConfig).limit(1))
        needs_setup = not config or not config.wifi_ssid
        if not needs_setup:
            self._reconcile_configured_boot(config)
            return
        if not self._auto_hotspot:
            return
        ssid = self._make_ssid(device_id)
        logger.info("Device not configured — starting setup hotspot '%s' at %s", ssid, self._ap_ip)
        ok = self._wifi.start_hotspot(ssid, self._password())
        self._hotspot_active = ok
        if ok:
            # Setup mode is entered only once the AP really exists; a stale
            # "setup mode on" with no AP would unlock /api/reset-password for
            # nothing and tell the wizard a hotspot is up when it is not.
            self._setup.enter_setup_mode("auto_boot")
            self._ap_retry_source = None
            self._start_captive_responder()
            self._emit("setup_ready")
            logger.info("Hotspot active. Connect to '%s' and open %s", ssid, self._ap_url())
        else:
            # wlan not managed yet / rfkill / NM still starting: the watchdog
            # retries on every poll instead of leaving the device stranded.
            logger.warning("Hotspot failed to start — the connectivity watchdog will keep retrying")
            self._ap_retry_source = "boot"

    def _reconcile_configured_boot(self, config: DeviceConfig) -> None:
        """A configured device must boot with setup mode OFF.

        The state file lives in tmpfs on an appliance, but a crash or OTA
        restart on a laptop (temp-dir fallback) can leave "setup_mode: true"
        behind — which opened /api/reset-password and made the wizard think a
        hotspot was up. Clear it, then adopt an AP that survived the restart
        (watchdog kill mid-fallback) or report the existing connection.
        """
        if self._setup_is_active():
            logger.info("Device is configured (ssid=%s) but setup mode was left on — clearing it", config.wifi_ssid)
            self._setup.exit_setup_mode()
        self._hotspot_active = False
        self._fallback_active = False
        self._ap_retry_source = None
        self._manual_session_started_at = None
        status_fn = getattr(self._wifi, "hotspot_status", None)
        if callable(status_fn):
            try:
                ap_up = bool((status_fn() or {}).get("active"))
            except Exception:  # noqa: BLE001
                ap_up = False
            if ap_up:
                logger.info("Setup AP is still active from a previous run — adopting it as the fallback AP")
                self._hotspot_active = True
                self._fallback_active = True
                self._polls_in_fallback = 0
                self._setup.enter_setup_mode("fallback")
                self._start_captive_responder()
                self._emit("setup_ready")
                return
        connected = self._connected_ssid()
        if connected:
            logger.info("Device already configured and connected to '%s'", connected)
            self._emit("connected")
        else:
            logger.info("Device already configured (ssid=%s) — waiting for Wi-Fi", config.wifi_ssid)

    def start_manual_hotspot(self, device_id: str, source: str = "button") -> dict:
        """Raise the setup AP. ``source``: "button" (physical hold — the default,
        used by the button handler), "web" (/onboarding/start), "fallback"
        (watchdog), "factory_reset".

        Idempotent: if the setup AP is already broadcasting, we return its info
        WITHOUT restarting it. Restarting while a phone is connected drops the
        connection ("kicked out the moment I hit setup"), so a wizard/captive
        call to this must never tear the network down.
        """
        ssid = self._make_ssid(device_id)
        password = self._password()
        self._last_device_id = device_id or self._last_device_id
        reason = _SOURCE_REASON.get(source, "manual_trigger")
        operator = source in ("button", "web")
        info = {
            "ssid": ssid, "password": password, "open": not bool(password),
            "already_active": False, "ap_url": self._ap_url(),
        }
        # Never interleave with a running join/AP flip (re-entrant for the
        # thread that holds the lock, e.g. complete() re-raising the AP).
        if not self._lock.acquire(timeout=5.0):
            logger.warning("start_manual_hotspot(%s): a network transition is in progress — try again shortly", source)
            return {"ok": False, "error": "busy", **info}
        try:
            already_up = self._hotspot_active or self._wifi.is_hotspot_up(ssid, bool(password))
            if already_up:
                self._hotspot_active = True
                self._setup.enter_setup_mode(reason)
                self._start_captive_responder()
                if operator:
                    self._begin_operator_session()
                return {"ok": True, "already_active": True, **{k: v for k, v in info.items() if k != "already_active"}}
            ok = self._wifi.start_hotspot(ssid, password)
            self._hotspot_active = ok
            if ok:
                self._setup.enter_setup_mode(reason)
                self._ap_retry_source = None
                self._start_captive_responder()
                if operator:
                    self._begin_operator_session()
                self._emit("setup_ready")
            else:
                logger.warning("Setup AP '%s' did not come up (%s) — the watchdog will retry", ssid, source)
                self._ap_retry_source = source
            return {"ok": ok, **info}
        finally:
            self._lock.release()

    def _begin_operator_session(self) -> None:
        """A person is at the device: stop fallback retries and start the idle clock."""
        now = self._clock()
        self._manual_session_started_at = now
        self._last_activity_at = now
        self._fallback_active = False
        self._polls_in_fallback = 0

    def touch_activity(self) -> None:
        """Called on every /onboarding/* request: a phone is mid-wizard, so the
        fallback retry timer restarts and the idle timeout is pushed back."""
        self._last_activity_at = self._clock()
        self._polls_in_fallback = 0

    def complete(self, ssid: str, password: str) -> bool:
        """Called after the onboarding wizard finishes. Switches to client Wi-Fi.

        The Pi Zero 2 W has a single radio, so the AP and the client connection
        cannot coexist. Tear the hotspot (and captive responder) down FIRST,
        then join the client network — otherwise the connect attempt races the
        still-running AP and setup hangs. The HTTP handler schedules this as a
        background task so the response is already on the wire before the
        network flips out from under it.
        """
        with self._transition():
            return self._join_network(ssid, password)

    def switch_network(self, ssid: str, password: str, *, previous_ssid: str | None = None) -> bool:
        """Change Wi-Fi after setup (Settings → Network) with the full
        AP-teardown → join → fallback dance. On failure the previously saved
        network (if any, and different) is tried before the setup AP comes back,
        so a mistyped password never takes a working device offline."""
        with self._transition():
            return self._join_network(ssid, password, previous_ssid=previous_ssid)

    def _join_network(self, ssid: str, password: str, *, previous_ssid: str | None = None) -> bool:
        self._setup.exit_setup_mode()
        self._stop_captive_responder()
        if self._hotspot_active:
            # Hold the AP up briefly so the wizard's connecting screen can load
            # over the hotspot before the single radio switches over.
            time.sleep(self._complete_grace_seconds)
            self._wifi.stop_hotspot()
            self._hotspot_active = False
        self._manual_session_started_at = None
        self._emit("connecting")
        connected = False
        if ssid:
            try:
                connected = bool(self._wifi.connect_client(ssid, password))
            except Exception as exc:  # noqa: BLE001
                logger.warning("connect_client('%s') raised: %s", ssid, exc)
        if connected:
            logger.info("Joined Wi-Fi '%s'", ssid)
            self._fallback_active = False
            self._offline_polls = 0
            self._polls_in_fallback = 0
            self._missing_profile_logged = None
            self._emit("connected")
            return True
        self._emit("failed")
        if previous_ssid and previous_ssid != ssid:
            logger.warning("Could not join '%s' — returning to the saved network '%s'", ssid, previous_ssid)
            if self._try_saved(previous_ssid):
                self._fallback_active = False
                self._offline_polls = 0
                self._emit("connected")
                return False
        # NEVER leave the device on zero networks. A wrong password, an
        # out-of-range router, or a skipped Wi-Fi step used to strand the unit
        # here (hotspot gone, no client connection, "configured" in the DB) —
        # unreachable until a reflash. Re-raise the setup AP so the operator
        # can simply reconnect and try again.
        logger.warning("Wi-Fi join failed/skipped — re-raising the setup AP")
        self._fallback_active = True
        self.start_manual_hotspot(self._last_device_id or "PI-XXXX", source="fallback")
        return False

    # ------------------------------------------------------------------
    # Connectivity watchdog — the never-unreachable invariant
    # ------------------------------------------------------------------

    def ensure_reachable(self, db: Session) -> None:
        """Called from the sensor-poll loop (~every 30 s). Guarantees the device
        is never on zero networks:

          * an AP that failed to come up (boot / button) is retried every poll;
          * no Wi-Fi AND no hotspot for ~2 minutes → raise the setup AP
            (fallback mode) so the operator can always reach the device;
          * fallback AP with a saved Wi-Fi → retry the saved network ~every
            10 min (restarted by wizard activity; stopped if the NM profile is
            gone); on success drop the AP, else re-raise it;
          * an operator session (button / wizard) is left alone, but an idle
            one on a configured device returns to the saved Wi-Fi after
            ``_manual_idle_timeout``.

        Joins/rejoins run on a background thread; this returns promptly and
        never races a join already in progress. Best-effort: any error here
        must never break sensor polling.
        """
        if self.transition_in_progress():
            return

        # 1. An AP was requested but never came up: keep trying.
        if self._ap_retry_source and not self._hotspot_active:
            if self._connected_ssid():
                logger.info("Pending setup AP no longer needed — device is online")
                self._ap_retry_source = None
                return
            config = db.scalar(select(DeviceConfig).limit(1))
            device_id = (config.device_id if config and config.device_id else "") or self._last_device_id or "PI-XXXX"
            logger.info("Retrying the setup AP (%s)", self._ap_retry_source)
            self.start_manual_hotspot(device_id, source=self._ap_retry_source)
            return

        now = self._clock()

        # 2. Operator-driven setup session — don't fight it, but don't let an
        #    accidental button hold take a configured device offline forever.
        if self._setup_is_active() and not self._fallback_active:
            if self._hotspot_active and self._manual_session_started_at is not None:
                config = db.scalar(select(DeviceConfig).limit(1))
                saved = config.wifi_ssid if config and config.wifi_ssid else None
                if saved:
                    idle_since = max(self._manual_session_started_at, self._last_activity_at or self._manual_session_started_at)
                    if now - idle_since >= self._manual_idle_timeout:
                        device_id = (config.device_id or "") or self._last_device_id or "PI-XXXX"
                        logger.info(
                            "Setup session idle for %.0f s with Wi-Fi saved — leaving the setup AP and rejoining '%s'",
                            now - idle_since, saved,
                        )
                        self._start_transition(lambda: self._return_from_manual_session(saved, device_id))
            return

        # 3. Fallback AP is up: periodically retry the saved Wi-Fi.
        if self._hotspot_active:
            if not self._fallback_active:
                return
            config = db.scalar(select(DeviceConfig).limit(1))
            if not config or not config.wifi_ssid:
                return  # nothing saved to retry — stay reachable on the AP
            saved = config.wifi_ssid
            if self._missing_profile_logged == saved:
                return  # profile is gone; retrying would only flap the AP
            self._polls_in_fallback += 1
            if self._polls_in_fallback < self._fallback_retry_polls:
                return
            self._polls_in_fallback = 0
            if not self._saved_profile_exists(saved):
                logger.warning(
                    "Saved Wi-Fi '%s' has no NetworkManager profile any more — staying on the setup AP "
                    "(re-run the wizard to pick a network)", saved,
                )
                self._missing_profile_logged = saved
                return
            device_id = (config.device_id or "") or self._last_device_id or "PI-XXXX"
            self._start_transition(lambda: self._fallback_rejoin(saved, device_id))
            return

        # 4. No AP and not in setup: are we online?
        if self._connected_ssid():
            self._offline_polls = 0
            self._fallback_active = False
            return

        self._offline_polls += 1
        if self._offline_polls >= self._offline_polls_threshold:
            self._offline_polls = 0
            logger.warning("No Wi-Fi and no hotspot for ~2 min — raising fallback setup AP")
            self._raise_fallback_ap(db)

    def _fallback_rejoin(self, saved: str, device_id: str) -> None:
        """Worker: drop the fallback AP, try the saved network, else bring the AP back."""
        if not (self._hotspot_active and self._fallback_active):
            return  # state changed while we waited for the lock
        logger.info("Fallback AP: retrying saved Wi-Fi '%s'", saved)
        self._stop_captive_responder()
        self._wifi.stop_hotspot()
        self._hotspot_active = False
        self._emit("connecting")
        if self._try_saved(saved):
            logger.info("Rejoined '%s' — leaving fallback AP mode", saved)
            self._fallback_active = False
            self._offline_polls = 0
            self._setup.exit_setup_mode()
            self._emit("connected")
            return
        logger.info("Saved Wi-Fi '%s' still unreachable — setup AP comes back", saved)
        self._raise_fallback_ap(device_id=device_id)

    def _return_from_manual_session(self, saved: str, device_id: str) -> None:
        """Worker: an idle operator session ends — rejoin the saved Wi-Fi."""
        if not self._hotspot_active or self._fallback_active:
            return
        self._stop_captive_responder()
        self._wifi.stop_hotspot()
        self._hotspot_active = False
        self._manual_session_started_at = None
        self._setup.exit_setup_mode()
        self._emit("connecting")
        if self._try_saved(saved):
            logger.info("Back on '%s' after the setup session timed out", saved)
            self._fallback_active = False
            self._offline_polls = 0
            self._emit("connected")
            return
        logger.warning("Could not rejoin '%s' after the setup session — raising the fallback AP", saved)
        self._raise_fallback_ap(device_id=device_id)

    def _raise_fallback_ap(self, db: Session | None = None, *, device_id: str = "") -> None:
        if db is not None:
            config = db.scalar(select(DeviceConfig).limit(1))
            device_id = (config.device_id if config and config.device_id else "") or device_id
        device_id = device_id or self._last_device_id or "PI-XXXX"
        self._fallback_active = True
        self._polls_in_fallback = 0
        self.start_manual_hotspot(device_id, source="fallback")

    # ------------------------------------------------------------------
    # Captive portal responder (best-effort; only while the hotspot is up)
    # ------------------------------------------------------------------

    def _start_captive_responder(self) -> None:
        try:
            from .captive_portal import CaptivePortalResponder

            if self._captive is None:
                self._captive = CaptivePortalResponder(f"{self._ap_url()}/onboarding")
            self._captive.start()
        except Exception as exc:  # noqa: BLE001 — never let this break onboarding
            logger.warning("Could not start captive portal responder: %s", exc)

    def _stop_captive_responder(self) -> None:
        try:
            if self._captive is not None:
                self._captive.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("captive responder stop: %s", exc)

    # ------------------------------------------------------------------
    # Read-only state for the routes
    # ------------------------------------------------------------------

    def is_hotspot_active(self) -> bool:
        return self._hotspot_active

    def is_fallback_active(self) -> bool:
        return self._fallback_active

    def ap_ssid(self, device_id: str) -> str:
        return self._make_ssid(device_id)

    def candidate_urls(self, hostname: str | None = None) -> list[str]:
        return candidate_urls(hostname)

    def lan_ips(self) -> list[str]:
        return lan_ips()
