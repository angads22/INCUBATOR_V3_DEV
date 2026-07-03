"""Never-unreachable invariant: the device is always on Wi-Fi OR its setup AP.

Covers the stranding bug: setup completes → hotspot torn down → Wi-Fi join
fails (wrong password / out of range / skipped) → previously the device ended
up on ZERO networks with wifi_ssid saved, so it never raised the AP again.
"""

import os
import sqlite3
import tempfile

from app.services.onboarding_service import OnboardingService


class _FakeWiFi:
    """Records calls; behavior is configured per test."""

    def __init__(self, connect_ok=False, connected_ssid=None, rejoin_ok=False):
        self.connect_ok = connect_ok
        self.connected_ssid = connected_ssid
        self.rejoin_ok = rejoin_ok
        self.start_calls = 0
        self.stop_calls = 0

    def is_hotspot_up(self, ssid, secured):
        return False

    def start_hotspot(self, ssid, password):
        self.start_calls += 1
        return True

    def stop_hotspot(self):
        self.stop_calls += 1

    def connect_client(self, ssid, password):
        return self.connect_ok

    def get_connected_ssid(self):
        return self.connected_ssid

    def try_saved_connection(self, ssid, timeout=25.0):
        return self.rejoin_ok


class _FakeSetup:
    def __init__(self):
        self.in_setup = False

    def enter_setup_mode(self, *_a):
        self.in_setup = True

    def exit_setup_mode(self, *_a):
        self.in_setup = False

    def is_setup_mode(self):
        return self.in_setup


def _svc(wifi):
    svc = OnboardingService(
        wifi_service=wifi,
        setup_mode_service=_FakeSetup(),
        ap_ssid_prefix="Incubator",
        ap_password="",
        ap_ip="10.42.0.1",
        ap_password_resolver=lambda: "",
    )
    svc._complete_grace_seconds = 0.0   # no 12s sleep in tests
    svc._last_device_id = "PI-ABCD1234"
    return svc


def _db():
    from app.database import get_db
    return next(get_db())


def _config_row(db, ssid="HomeNet"):
    from app.models import DeviceConfig
    row = DeviceConfig(device_id="PI-ABCD1234", claimed=False, wifi_ssid=ssid)
    db.add(row)
    db.commit()
    return row


# ── complete(): failed/skipped join re-raises the AP ─────────────────────────

def test_failed_join_reraises_setup_ap():
    wifi = _FakeWiFi(connect_ok=False)
    svc = _svc(wifi)
    svc._hotspot_active = True          # wizard ran over the AP
    assert svc.complete("HomeNet", "wrongpass") is False
    assert svc._hotspot_active is True  # AP is BACK — device stays reachable
    assert wifi.start_calls >= 1


def test_skipped_wifi_keeps_setup_ap():
    wifi = _FakeWiFi(connect_ok=False)
    svc = _svc(wifi)
    svc._hotspot_active = True
    assert svc.complete("", "") is False
    assert svc._hotspot_active is True  # no join to make — keep the AP


def test_successful_join_leaves_ap_down():
    wifi = _FakeWiFi(connect_ok=True)
    svc = _svc(wifi)
    svc._hotspot_active = True
    assert svc.complete("HomeNet", "rightpass") is True
    assert svc._hotspot_active is False
    assert wifi.start_calls == 0


# ── ensure_reachable(): poll-loop watchdog ───────────────────────────────────

def test_watchdog_raises_ap_after_offline_threshold(client):
    wifi = _FakeWiFi(connected_ssid=None)
    svc = _svc(wifi)
    db = _db()
    try:
        _config_row(db)                          # configured, but Wi-Fi is down
        for _ in range(svc._offline_polls_threshold):
            svc.ensure_reachable(db)
        assert svc._hotspot_active is True       # fallback AP raised
        assert svc._fallback_active is True
        assert wifi.start_calls == 1
    finally:
        db.close()


def test_watchdog_quiet_while_connected(client):
    wifi = _FakeWiFi(connected_ssid="HomeNet")
    svc = _svc(wifi)
    db = _db()
    try:
        _config_row(db)
        for _ in range(10):
            svc.ensure_reachable(db)
        assert wifi.start_calls == 0             # never touched the radio
    finally:
        db.close()


def test_fallback_retries_saved_wifi_and_recovers(client):
    wifi = _FakeWiFi(connected_ssid=None, rejoin_ok=True)
    svc = _svc(wifi)
    db = _db()
    try:
        _config_row(db)
        svc._raise_fallback_ap(db)               # in fallback AP mode
        svc._polls_in_fallback = svc._fallback_retry_polls - 1
        svc.ensure_reachable(db)                 # triggers the rejoin attempt
        assert svc._hotspot_active is False      # back on the home Wi-Fi
        assert svc._fallback_active is False
    finally:
        db.close()


def test_fallback_reraises_ap_when_rejoin_fails(client):
    wifi = _FakeWiFi(connected_ssid=None, rejoin_ok=False)
    svc = _svc(wifi)
    db = _db()
    try:
        _config_row(db)
        svc._raise_fallback_ap(db)
        starts_before = wifi.start_calls
        svc._polls_in_fallback = svc._fallback_retry_polls - 1
        svc.ensure_reachable(db)
        assert svc._hotspot_active is True       # AP came straight back
        assert wifi.start_calls == starts_before + 1
    finally:
        db.close()


def test_watchdog_leaves_operator_setup_alone(client):
    wifi = _FakeWiFi(connected_ssid=None)
    svc = _svc(wifi)
    db = _db()
    try:
        _config_row(db)
        svc._setup.enter_setup_mode("manual")    # operator-initiated session
        for _ in range(10):
            svc.ensure_reachable(db)
        assert wifi.start_calls == 0
    finally:
        db.close()


# ── DB corruption quarantine ─────────────────────────────────────────────────

def test_corrupt_sqlite_is_quarantined():
    from app.database import ensure_sqlite_healthy

    d = tempfile.mkdtemp()
    path = os.path.join(d, "incubator.db")
    with open(path, "wb") as fh:
        fh.write(b"definitely not a sqlite file" * 100)
    assert ensure_sqlite_healthy(f"sqlite:///{path}") is False
    assert not os.path.exists(path)                       # moved aside
    assert any(".corrupt-" in f for f in os.listdir(d))   # kept for forensics


def test_healthy_sqlite_is_untouched():
    from app.database import ensure_sqlite_healthy

    d = tempfile.mkdtemp()
    path = os.path.join(d, "incubator.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    assert ensure_sqlite_healthy(f"sqlite:///{path}") is True
    assert os.path.exists(path)


def test_missing_db_is_fine():
    from app.database import ensure_sqlite_healthy

    assert ensure_sqlite_healthy("sqlite:////nonexistent/nowhere.db") is True
