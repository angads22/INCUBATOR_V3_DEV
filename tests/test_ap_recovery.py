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

    # And the freshly-built AP is OPEN: created with our SSID and no WPA key.
    flat = [tok for c in calls for tok in c]
    add = next(c for c in calls if "add" in c)
    assert "Incubator-NEW9" in add
    assert "wpa-psk" not in flat
    assert "802-11-wireless-security.psk" not in flat


# ── OnboardingService: DB resolver wins over any stale static password ────────

class _RecordingWiFi:
    def __init__(self, already_up=False):
        self.started_with = None
        self.start_calls = 0
        self._already_up = already_up

    def is_hotspot_up(self, ssid, secured):
        return self._already_up

    def start_hotspot(self, ssid, password):
        self.start_calls += 1
        self.started_with = (ssid, password)
        return True


class _NoopSetup:
    def enter_setup_mode(self, *_a, **_k):
        pass

    def exit_setup_mode(self, *_a, **_k):
        pass


def _service(resolver, wifi=None):
    return OnboardingService(
        wifi_service=wifi or _RecordingWiFi(),
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


def test_manual_hotspot_does_not_restart_when_already_up():
    # The AP is already broadcasting → hitting "start" must NOT tear it down
    # (that's the "kicked out the moment I hit setup" bug).
    wifi = _RecordingWiFi(already_up=True)
    svc = _service(lambda: "", wifi=wifi)
    out = svc.start_manual_hotspot("PI-ABCD1234")
    assert out["ok"] is True and out["already_active"] is True
    assert wifi.start_calls == 0                     # never restarted


def test_manual_hotspot_starts_when_not_up():
    wifi = _RecordingWiFi(already_up=False)
    svc = _service(lambda: "", wifi=wifi)
    out = svc.start_manual_hotspot("PI-ABCD1234")
    assert out["already_active"] is False
    assert wifi.start_calls == 1


# ── start_hotspot idempotency + AP-mode purge (real WiFiService, mock nmcli) ──

def test_start_hotspot_early_returns_when_already_up(monkeypatch):
    """If our AP is already active with matching ssid+security, do not purge."""
    calls = []

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nmcli":
            calls.append(list(cmd))
            joined = " ".join(cmd)
            if "--active" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="incubator-hotspot:activated\n", stderr="")
            if "-g" in cmd and "802-11-wireless.ssid" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="Incubator-A3F2\n", stderr="")
            if "-g" in cmd and "802-11-wireless-security.key-mgmt" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")   # open
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    assert WiFiService(country="US").start_hotspot("Incubator-A3F2", "") is True
    # Idempotent path: no delete / add / up issued.
    verbs = {tok for c in calls for tok in c}
    assert "delete" not in verbs and "add" not in verbs


def test_purge_deletes_ap_mode_profile_by_mode(monkeypatch):
    """A stale AP-mode profile with an unrelated name is still purged."""
    calls = []

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nmcli":
            calls.append(list(cmd))
            if "--active" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")   # not up
            if cmd[1:4] == ["-t", "-f", "NAME,TYPE"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="RandomAP:802-11-wireless\n", stderr="")
            if "-g" in cmd and "802-11-wireless.mode" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="ap\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    assert WiFiService(country="US").start_hotspot("Incubator-NEW", "") is True
    deleted = {c[c.index("delete") + 1] for c in calls if "delete" in c}
    assert "RandomAP" in deleted                     # purged by mode, not name


def test_debug_network_endpoint(client):
    r = client.get("/api/debug/network")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "connections" in body


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
