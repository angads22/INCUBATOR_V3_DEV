"""The post-setup Wi-Fi join must survive a stale scan cache.

connect_client runs seconds after the setup AP is torn down; the single radio
has not scanned since before the AP came up, so ``nmcli device wifi connect``
often fails with "No network with SSID ...". The fix: rescan+wait first, and
when the direct connect still fails, build an explicit profile and activate it
(profile activation probes for the network and needs no scan cache).
"""

import subprocess

import pytest

import app.services.wifi_service as ws


class _FakeNmcli:
    """Scripted nmcli: records every invocation, answers from a playbook."""

    def __init__(self, visible_ssids=(), device_connect_rc=0, profile_up_rc=0,
                 connected_after=None):
        self.calls = []
        self.visible_ssids = list(visible_ssids)
        self.device_connect_rc = device_connect_rc
        self.profile_up_rc = profile_up_rc
        # SSID that "device wifi"/ACTIVE reports once a join succeeded.
        self.connected = None
        self.connected_after = connected_after

    def __call__(self, *args, check=True):
        self.calls.append(args)
        joined = " ".join(args)
        rc, out = 0, ""
        if "dev wifi list" in joined or "device wifi list" in joined:
            out = "\n".join(f"{s}:70:WPA2" for s in self.visible_ssids)
        elif args[:3] == ("device", "wifi", "connect"):
            rc = self.device_connect_rc
            if rc == 0:
                self.connected = args[3]
        elif args[:2] == ("connection", "up"):
            rc = self.profile_up_rc
            if rc == 0:
                self.connected = self.connected_after or args[2]
        elif "-t -f ACTIVE,SSID dev wifi" in joined:
            out = f"yes:{self.connected}" if self.connected else "no:"
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")


class _FakeClock:
    """time.time()/sleep() stand-in — sleeps advance the clock instantly."""

    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Virtual clock (no real waiting); radio prep is a no-op."""
    clock = _FakeClock()
    monkeypatch.setattr(ws, "time", clock)
    monkeypatch.setattr(ws.WiFiService, "_prepare_radio", lambda self: None)


def _wire(monkeypatch, fake):
    monkeypatch.setattr(ws, "_nmcli", fake)
    return ws.WiFiService(country="US")


def test_direct_connect_succeeds_after_rescan(monkeypatch):
    fake = _FakeNmcli(visible_ssids=["HomeNet"], device_connect_rc=0)
    svc = _wire(monkeypatch, fake)
    assert svc.connect_client("HomeNet", "hunter2pass") is True
    # It rescanned (radio just freed from AP duty) before trying to join.
    assert ("device", "wifi", "rescan") in fake.calls
    assert any(c[:3] == ("device", "wifi", "connect") for c in fake.calls)


def test_falls_back_to_explicit_profile_when_direct_connect_fails(monkeypatch):
    # SSID never shows in the scan list and the direct connect is refused —
    # the exact stale-cache failure. The profile path must still join.
    fake = _FakeNmcli(visible_ssids=[], device_connect_rc=10, profile_up_rc=0,
                      connected_after="HomeNet")
    svc = _wire(monkeypatch, fake)
    assert svc.connect_client("HomeNet", "hunter2pass") is True
    adds = [c for c in fake.calls if c[:2] == ("connection", "add")]
    assert adds and "autoconnect" in adds[0]
    # hidden=yes forces active probing so the join needs no scan cache;
    # the password rode along as wpa-psk.
    mods = [c for c in fake.calls if c[:2] == ("connection", "modify")]
    flat = " ".join(" ".join(c) for c in mods)
    assert "802-11-wireless.hidden yes" in flat
    assert "wpa-psk" in flat


def test_total_failure_cleans_up_profile_and_returns_false(monkeypatch):
    fake = _FakeNmcli(visible_ssids=[], device_connect_rc=10, profile_up_rc=4)
    svc = _wire(monkeypatch, fake)
    assert svc.connect_client("HomeNet", "wrongpass12") is False
    # The dead profile was deleted so it can't fight the fallback AP later.
    assert ("connection", "delete", "HomeNet") in fake.calls


def test_open_network_profile_has_no_wifi_sec(monkeypatch):
    fake = _FakeNmcli(visible_ssids=[], device_connect_rc=10, profile_up_rc=0,
                      connected_after="OpenBarn")
    svc = _wire(monkeypatch, fake)
    assert svc.connect_client("OpenBarn", "") is True
    mods = [c for c in fake.calls if c[:2] == ("connection", "modify")]
    flat = " ".join(" ".join(c) for c in mods)
    assert "wpa-psk" not in flat


def test_bad_ssid_rejected_without_touching_nmcli(monkeypatch):
    fake = _FakeNmcli()
    svc = _wire(monkeypatch, fake)
    assert svc.connect_client("bad;`ssid`" + "x" * 40, "pw") is False
    assert fake.calls == []


# ── Captive launcher: themed + actionable ────────────────────────────────────

def test_launcher_page_is_life_loop_themed():
    from app.services.captive_portal import _landing_page

    html = _landing_page("http://10.42.0.1:8000/onboarding").decode()
    assert "#efe7d6" in html and "#c2702a" in html   # cream paper + clay accent
    assert "Open setup in my browser" in html
    assert "10.42.0.1:8000/onboarding" in html
    assert "Use Without Internet" in html            # iPhone escape hatch


def test_finish_screen_gives_numbered_steps(client):
    html = client.get("/onboarding").text
    assert "Reconnect your phone to your home Wi-Fi" in html
    assert "Open your browser" in html
    assert "Type this address" in html
    assert "finishCopyBtn" in html
