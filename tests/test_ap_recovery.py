"""Regression: setup AP stays OPEN and never piles up duplicate locked SSIDs.

Covers the two on-device failures reported after flashing:
  * the AP came up locked (a stale random password from /etc/incubator.env);
  * several "Incubator-XXXX" networks showed up at once (stale NM AP profiles).
"""

import subprocess

from app.services import wifi_service as wifi_mod
from app.services.wifi_service import WiFiService
from app.services.onboarding_service import OnboardingService


# ── WiFiService: purge stale AP profiles, bring up one open AP ────────────────

def _fake_nmcli(monkeypatch, existing_connections: str):
    """Patch subprocess.run so `connection show` returns canned profiles and
    every other nmcli call just succeeds; record the argv of each call."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "nmcli":
            calls.append(list(cmd))
            if "show" in cmd and "connection" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=existing_connections, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    return calls


def test_stale_ap_profiles_are_purged_then_open_ap_started(monkeypatch):
    existing = "\n".join([
        "incubator-hotspot:802-11-wireless",
        "Hotspot:802-11-wireless",
        "Incubator-1A2B:802-11-wireless",
        "HomeWifi:802-11-wireless",
        "Wired connection 1:802-3-ethernet",
    ])
    calls = _fake_nmcli(monkeypatch, existing)

    assert WiFiService(country="US").start_hotspot("Incubator-NEW9", "") is True

    deleted = {c[c.index("delete") + 1] for c in calls if "delete" in c}
    # All three AP/hotspot profiles are removed...
    assert {"incubator-hotspot", "Hotspot", "Incubator-1A2B"} <= deleted
    # ...but an unrelated client network is left alone.
    assert "HomeWifi" not in deleted
    assert "Wired connection 1" not in deleted

    # And the new hotspot is OPEN (no password argument to nmcli).
    hotspot = next(c for c in calls if "hotspot" in c)
    assert "password" not in hotspot
    assert "Incubator-NEW9" in hotspot


# ── OnboardingService: DB resolver wins over any stale static password ────────

class _RecordingWiFi:
    def __init__(self):
        self.started_with = None

    def start_hotspot(self, ssid, password):
        self.started_with = (ssid, password)
        return True


class _NoopSetup:
    def enter_setup_mode(self, *_a, **_k):
        pass

    def exit_setup_mode(self, *_a, **_k):
        pass


def _service(resolver):
    return OnboardingService(
        wifi_service=_RecordingWiFi(),
        setup_mode_service=_NoopSetup(),
        ap_ssid_prefix="Incubator",
        ap_password="stale-baked-key",   # what an old env would have provided
        ap_ip="10.42.0.1",
        ap_password_resolver=resolver,
    )


def test_resolver_open_overrides_stale_static_password():
    svc = _service(lambda: "")
    out = svc.start_manual_hotspot("PI-ABCD1234")
    assert out["open"] is True
    assert svc._wifi.started_with[1] == ""          # open, not the stale key


def test_resolver_user_password_is_used():
    svc = _service(lambda: "barnpass1")
    out = svc.start_manual_hotspot("PI-ABCD1234")
    assert out["open"] is False
    assert svc._wifi.started_with[1] == "barnpass1"


# ── Post-setup AP password endpoint + DB-authoritative resolution ─────────────

def test_effective_ap_password_defaults_open(client):
    from app.database import get_db
    from app.settings_store import effective_ap_password

    db = next(get_db())
    try:
        assert effective_ap_password(db) == ""
    finally:
        db.close()


def test_set_ap_password_endpoint_roundtrip(client):
    from app.database import get_db
    from app.settings_store import effective_ap_password

    assert client.post("/api/settings/ap-password", json={"password": "barnpass1"}).status_code == 200
    db = next(get_db())
    try:
        assert effective_ap_password(db) == "barnpass1"
    finally:
        db.close()
    # Clearing it returns the network to open.
    assert client.post("/api/settings/ap-password", json={"password": ""}).json()["open"] is True


def test_set_ap_password_rejects_too_short(client):
    assert client.post("/api/settings/ap-password", json={"password": "abc"}).status_code == 400
