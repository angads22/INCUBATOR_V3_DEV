"""OnboardingService: boot reconciliation, feedback events, the transition
lock, background rejoins, wizard-activity awareness and the return path from
an idle setup session — all with a fake clock and a scripted Wi-Fi service."""

from __future__ import annotations

import json
import subprocess
import threading

import pytest

from app.services import onboarding_service as onb
from app.services.onboarding_service import OnboardingService


class FakeWiFi:
    def __init__(self, *, start_ok=True, connect_ok=True, rejoin_ok=False, connected=None,
                 has_profile=True, ap_active=False):
        self.start_ok = start_ok
        self.connect_ok = connect_ok
        self.rejoin_ok = rejoin_ok
        self.connected = connected
        self.has_profile = has_profile
        self.ap_active = ap_active
        self.start_calls = 0
        self.stop_calls = 0
        self.rejoins: list[str] = []
        self.connects: list[str] = []
        self.block: threading.Event | None = None   # when set, try_saved_connection waits on it

    def is_hotspot_up(self, ssid, secured):
        return False

    def start_hotspot(self, ssid, password):
        self.start_calls += 1
        return self.start_ok

    def stop_hotspot(self):
        self.stop_calls += 1

    def connect_client(self, ssid, password):
        self.connects.append(ssid)
        if self.connect_ok:
            self.connected = ssid
        return self.connect_ok

    def try_saved_connection(self, ssid, timeout=25.0):
        self.rejoins.append(ssid)
        if self.block is not None:
            self.block.wait(5.0)
        if self.rejoin_ok:
            self.connected = ssid
        return self.rejoin_ok

    def get_connected_ssid(self):
        return self.connected

    def has_saved_profile(self, ssid):
        return self.has_profile

    def hotspot_status(self):
        return {"active": self.ap_active}


class FakeSetup:
    def __init__(self):
        self.in_setup = False
        self.reasons: list[str] = []

    def enter_setup_mode(self, reason):
        self.in_setup = True
        self.reasons.append(reason)

    def exit_setup_mode(self):
        self.in_setup = False

    def is_setup_mode(self):
        return self.in_setup


class Clock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _svc(wifi, events=None, *, auto=True):
    clock = Clock()
    svc = OnboardingService(
        wifi_service=wifi,
        setup_mode_service=FakeSetup(),
        ap_ssid_prefix="Incubator",
        ap_password="",
        ap_ip="10.42.0.1",
        auto_hotspot=auto,
        ap_password_resolver=lambda: "",
        on_event=(events.append if events is not None else None),
    )
    svc._complete_grace_seconds = 0.0
    svc._last_device_id = "PI-ABCD1234"
    svc._clock = clock
    return svc, clock


def _db():
    from app.database import get_db
    return next(get_db())


def _config_row(db, ssid="HomeNet"):
    from app.models import DeviceConfig
    row = DeviceConfig(device_id="PI-ABCD1234", claimed=False, wifi_ssid=ssid)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def db():
    session = _db()
    try:
        yield session
    finally:
        session.close()


# ── boot() ───────────────────────────────────────────────────────────────────

def test_boot_unconfigured_enters_setup_mode_only_once_ap_is_up(db):
    events: list[str] = []
    wifi = FakeWiFi(start_ok=True)
    svc, _ = _svc(wifi, events)
    svc.boot(db, "PI-ABCD1234")
    assert svc.is_hotspot_active() is True
    assert svc._setup.in_setup is True and svc._setup.reasons == ["auto_boot"]
    assert events == ["setup_ready"]


def test_boot_ap_failure_is_retried_by_the_watchdog(db):
    events: list[str] = []
    wifi = FakeWiFi(start_ok=False)
    svc, _ = _svc(wifi, events)
    svc.boot(db, "PI-ABCD1234")
    assert svc.is_hotspot_active() is False
    assert svc._setup.in_setup is False            # no AP → no setup mode
    assert events == []
    svc.ensure_reachable(db)                        # radio still not ready
    assert wifi.start_calls == 2 and svc.is_hotspot_active() is False
    wifi.start_ok = True                            # NM now manages the radio
    svc.ensure_reachable(db)
    assert wifi.start_calls == 3 and svc.is_hotspot_active() is True
    assert svc._setup.reasons == ["auto_boot"] and events == ["setup_ready"]
    svc.ensure_reachable(db)                        # stable afterwards
    assert wifi.start_calls == 3


@pytest.mark.parametrize("auto", [True, False])
def test_boot_configured_clears_stale_setup_mode_and_emits_connected(db, auto):
    _config_row(db, "HomeNet")
    events: list[str] = []
    wifi = FakeWiFi(connected="HomeNet")
    svc, _ = _svc(wifi, events, auto=auto)
    svc._setup.enter_setup_mode("manual_trigger")   # stale from a crash / OTA restart
    svc._hotspot_active = True
    svc._fallback_active = True
    svc.boot(db, "PI-ABCD1234")
    assert svc._setup.in_setup is False
    assert svc.is_hotspot_active() is False and svc.is_fallback_active() is False
    assert events == ["connected"]
    assert wifi.start_calls == 0


def test_boot_configured_adopts_an_ap_that_survived_a_restart(db):
    _config_row(db, "HomeNet")
    events: list[str] = []
    wifi = FakeWiFi(connected=None, ap_active=True)
    svc, _ = _svc(wifi, events)
    svc.boot(db, "PI-ABCD1234")
    assert svc.is_hotspot_active() is True and svc.is_fallback_active() is True
    assert svc._setup.reasons == ["fallback"] and events == ["setup_ready"]


# ── events around complete() / switch_network() ──────────────────────────────

def test_complete_emits_connecting_then_connected():
    events: list[str] = []
    svc, _ = _svc(FakeWiFi(connect_ok=True), events)
    svc._hotspot_active = True
    assert svc.complete("HomeNet", "pw") is True
    assert events == ["connecting", "connected"]
    assert svc.is_fallback_active() is False


def test_failed_complete_emits_failed_then_setup_ready_for_the_returning_ap():
    events: list[str] = []
    wifi = FakeWiFi(connect_ok=False)
    svc, _ = _svc(wifi, events)
    svc._hotspot_active = True
    assert svc.complete("HomeNet", "wrong") is False
    assert events == ["connecting", "failed", "setup_ready"]
    assert svc.is_hotspot_active() and svc.is_fallback_active()
    assert svc._setup.reasons[-1] == "fallback"


def test_on_event_errors_never_break_the_flow():
    def boom(name):
        raise RuntimeError(name)

    svc = OnboardingService(FakeWiFi(), FakeSetup(), "Incubator", "", "10.42.0.1", on_event=boom)
    svc._complete_grace_seconds = 0.0
    assert svc.complete("HomeNet", "pw") is True


def test_switch_network_returns_to_previous_network_instead_of_raising_ap():
    events: list[str] = []
    wifi = FakeWiFi(connect_ok=False, rejoin_ok=True, connected="HomeNet")
    svc, _ = _svc(wifi, events)
    assert svc.switch_network("NewNet", "typo", previous_ssid="HomeNet") is False
    assert wifi.connects == ["NewNet"] and wifi.rejoins == ["HomeNet"]
    assert wifi.start_calls == 0 and svc.is_hotspot_active() is False
    assert events == ["connecting", "failed", "connected"]


def test_switch_network_raises_ap_when_previous_network_is_gone_too():
    wifi = FakeWiFi(connect_ok=False, rejoin_ok=False, connected=None)
    svc, _ = _svc(wifi)
    assert svc.switch_network("NewNet", "typo", previous_ssid="HomeNet") is False
    assert wifi.rejoins == ["HomeNet"] and wifi.start_calls == 1
    assert svc.is_hotspot_active() and svc.is_fallback_active()


def test_switch_network_from_the_ap_tears_it_down_first():
    wifi = FakeWiFi(connect_ok=True)
    svc, _ = _svc(wifi)
    svc._hotspot_active = True
    svc._setup.enter_setup_mode("button_hold")
    assert svc.switch_network("HomeNet", "pw") is True
    assert wifi.stop_calls == 1 and svc.is_hotspot_active() is False
    assert svc._setup.in_setup is False


# ── transition lock / background work ────────────────────────────────────────

def test_watchdog_stays_out_while_a_transition_is_running(db):
    _config_row(db)
    wifi = FakeWiFi(connected=None)
    svc, _ = _svc(wifi)
    svc._transitions_active = 1                     # complete() running elsewhere
    for _ in range(10):
        svc.ensure_reachable(db)
    assert svc._offline_polls == 0 and wifi.start_calls == 0


def test_fallback_rejoin_runs_in_background_and_holds_the_lock(db):
    _config_row(db)
    events: list[str] = []
    wifi = FakeWiFi(connected=None, rejoin_ok=True)
    wifi.block = threading.Event()                  # make the rejoin slow
    svc, _ = _svc(wifi, events)
    svc._transition_join_timeout = 0.05
    svc._raise_fallback_ap(db)
    svc._polls_in_fallback = svc._fallback_retry_polls - 1
    svc.ensure_reachable(db)                        # returns promptly…
    assert svc.transition_in_progress() is True     # …while the rejoin runs
    assert svc._lock.acquire(blocking=False) is False   # worker owns the transition lock
    starts, stops = wifi.start_calls, wifi.stop_calls
    for _ in range(5):
        svc.ensure_reachable(db)                    # no second start/stop interleaved
    assert (wifi.start_calls, wifi.stop_calls) == (starts, stops)
    busy = svc.start_manual_hotspot("PI-ABCD1234", source="web") if False else None  # noqa: F841
    wifi.block.set()
    svc._transition_thread.join(5.0)
    assert svc.is_hotspot_active() is False and svc.is_fallback_active() is False
    assert svc._setup.in_setup is False
    assert events[-1] == "connected"
    assert svc._lock.acquire(blocking=False) is True
    svc._lock.release()


def test_fallback_retry_failure_skips_the_failed_buzz_but_brings_ap_back(db):
    _config_row(db)
    events: list[str] = []
    wifi = FakeWiFi(connected=None, rejoin_ok=False)
    svc, _ = _svc(wifi, events)
    svc._raise_fallback_ap(db)
    events.clear()
    svc._polls_in_fallback = svc._fallback_retry_polls - 1
    svc.ensure_reachable(db)
    assert wifi.rejoins == ["HomeNet"]
    assert svc.is_hotspot_active() and svc.is_fallback_active()
    assert "failed" not in events and events[-1] == "setup_ready"


def test_wizard_activity_resets_the_fallback_retry_timer(db):
    _config_row(db)
    wifi = FakeWiFi(connected=None, rejoin_ok=True)
    svc, _ = _svc(wifi)
    svc._raise_fallback_ap(db)
    svc._polls_in_fallback = svc._fallback_retry_polls - 1
    svc.touch_activity()                            # a phone is mid-wizard
    svc.ensure_reachable(db)
    assert wifi.stop_calls == 0 and svc.is_hotspot_active() is True
    assert svc._polls_in_fallback == 1


def test_missing_saved_profile_stops_retries_and_logs_once(db, caplog):
    _config_row(db, "HomeNet")
    wifi = FakeWiFi(connected=None, rejoin_ok=True, has_profile=False)
    svc, _ = _svc(wifi)
    svc._raise_fallback_ap(db)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            svc._polls_in_fallback = svc._fallback_retry_polls - 1
            svc.ensure_reachable(db)
    assert wifi.stop_calls == 0 and wifi.rejoins == []
    assert svc.is_hotspot_active() is True
    assert caplog.text.count("has no NetworkManager profile") == 1


# ── idle operator session returns home ───────────────────────────────────────

def test_idle_button_session_on_configured_device_rejoins_saved_wifi(db):
    _config_row(db, "HomeNet")
    events: list[str] = []
    wifi = FakeWiFi(connected=None, rejoin_ok=True)
    svc, clock = _svc(wifi, events)
    out = svc.start_manual_hotspot("PI-ABCD1234")   # physical button (default source)
    assert out["ok"] and svc._setup.reasons == ["button_hold"]
    events.clear()
    svc.ensure_reachable(db)
    clock.advance(599)
    svc.ensure_reachable(db)
    assert svc.is_hotspot_active() is True          # still within the window
    svc.touch_activity()                            # phone loaded the wizard
    clock.advance(599)
    svc.ensure_reachable(db)
    assert svc.is_hotspot_active() is True          # activity pushed the deadline
    clock.advance(2)
    svc.ensure_reachable(db)
    assert wifi.rejoins == ["HomeNet"]
    assert svc.is_hotspot_active() is False and svc._setup.in_setup is False
    assert events == ["connecting", "connected"]


def test_idle_session_without_saved_wifi_keeps_the_ap(db):
    _config_row(db, None)
    wifi = FakeWiFi(connected=None, rejoin_ok=True)
    svc, clock = _svc(wifi)
    svc.start_manual_hotspot("PI-ABCD1234")
    clock.advance(100_000)
    svc.ensure_reachable(db)
    assert svc.is_hotspot_active() is True and wifi.rejoins == []


def test_idle_session_rejoin_failure_falls_back_to_the_ap(db):
    _config_row(db, "HomeNet")
    wifi = FakeWiFi(connected=None, rejoin_ok=False)
    svc, clock = _svc(wifi)
    svc.start_manual_hotspot("PI-ABCD1234", source="web")
    assert svc._setup.reasons == ["manual_trigger"]
    clock.advance(601)
    svc.ensure_reachable(db)
    assert wifi.rejoins == ["HomeNet"]
    assert svc.is_hotspot_active() and svc.is_fallback_active()
    assert svc._setup.reasons[-1] == "fallback"


def test_operator_pressing_button_during_fallback_takes_over_the_session(db):
    _config_row(db, "HomeNet")
    wifi = FakeWiFi(connected=None, rejoin_ok=True)
    svc, _ = _svc(wifi)
    svc._raise_fallback_ap(db)
    svc._polls_in_fallback = svc._fallback_retry_polls - 1
    out = svc.start_manual_hotspot("PI-ABCD1234")
    assert out["already_active"] is True and wifi.start_calls == 1
    assert svc.is_fallback_active() is False and svc._polls_in_fallback == 0
    svc.ensure_reachable(db)
    assert wifi.stop_calls == 0                     # no fallback retry under the operator


def test_button_ap_failure_is_retried_until_it_comes_up(db):
    _config_row(db, None)
    wifi = FakeWiFi(start_ok=False, connected=None)
    svc, _ = _svc(wifi)
    out = svc.start_manual_hotspot("PI-ABCD1234")
    assert out["ok"] is False and svc._setup.in_setup is False
    svc.ensure_reachable(db)
    assert wifi.start_calls == 2
    wifi.start_ok = True
    svc.ensure_reachable(db)
    assert svc.is_hotspot_active() and svc._setup.reasons == ["button_hold"]


# ── address helpers ──────────────────────────────────────────────────────────

def test_short_hostname_and_candidate_urls(monkeypatch):
    monkeypatch.setattr(onb.socket, "gethostname", lambda: "rock.farm.lan")
    assert onb.short_hostname() == "rock"
    assert onb.candidate_urls() == ["http://rock.local:8000", "http://rock:8000", "http://rock.lan:8000"]
    assert onb.candidate_urls("incubator-ab12") == [
        "http://incubator-ab12.local:8000", "http://incubator-ab12:8000", "http://incubator-ab12.lan:8000",
    ]
    monkeypatch.setattr(onb.socket, "gethostname", lambda: "")
    assert onb.short_hostname() == "incubator"


def test_lan_ips_parses_iproute_json_and_skips_loopback(monkeypatch):
    payload = [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
        {"ifname": "wlan0", "addr_info": [
            {"family": "inet", "local": "192.168.1.23", "scope": "global"},
            {"family": "inet6", "local": "fe80::1", "scope": "link"},
        ]},
        {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "10.0.0.5", "scope": "global"}]},
    ]

    def fake_run(cmd, *a, **k):
        assert cmd[:2] == ["ip", "-j"] and k.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(onb.subprocess, "run", fake_run)
    onb._lan_ips_cache["ts"] = 0.0
    assert onb.lan_ips() == ["192.168.1.23", "10.0.0.5"]
    # Cached: a broken `ip` right after still serves the same answer.
    monkeypatch.setattr(onb.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert onb.lan_ips() == ["192.168.1.23", "10.0.0.5"]
    onb._lan_ips_cache["ts"] = 0.0
    ips = onb.lan_ips()                             # fallback path never raises
    assert isinstance(ips, list) and not any(ip.startswith("127.") for ip in ips)
    onb._lan_ips_cache["ts"] = 0.0
