"""Single-radio onboarding: the network list is pre-scanned before the AP.

While the Pi Zero 2 W hosts its setup hotspot, its one radio can't scan, so a
live scan returns nothing. We capture nearby networks just before the AP comes
up and serve that cache to the wizard.
"""

import subprocess

from app.services import wifi_service as wifi_mod
from app.services.wifi_service import WiFiService


def _fake_scan(monkeypatch, first_output):
    """nmcli `dev wifi list` returns `first_output` once, then empty (AP up)."""
    state = {"n": 0}

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nmcli" and "list" in cmd:
            state["n"] += 1
            out = first_output if state["n"] == 1 else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)


def test_prescan_cache_serves_networks_when_radio_busy(monkeypatch):
    _fake_scan(monkeypatch, "HomeWifi:80:WPA2\nBarnOffice:60:WPA2\nIncubator-A3F2:99:\n")
    svc = WiFiService(country="US")

    svc.prescan()                                   # radio free → caches the real list
    names = [n.ssid for n in svc.scan_networks()]   # AP up (empty live) → cache

    assert "HomeWifi" in names and "BarnOffice" in names
    # Our own hotspot must never be offered as a network to join.
    assert not any(s.startswith("Incubator") for s in names)


def test_cached_read_used_when_no_prescan(monkeypatch):
    # With no prescan cache, scan_networks reads NM's cache (still no forced
    # rescan) rather than the mock fallback.
    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nmcli" and "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="LiveNet:70:WPA2\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    svc = WiFiService(country="US")
    names = [n.ssid for n in svc.scan_networks()]
    assert names == ["LiveNet"]


def _capture(monkeypatch, output=""):
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nmcli":
            calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    return calls


def _rescan_arg(cmd):
    return cmd[cmd.index("--rescan") + 1] if "--rescan" in cmd else None


def test_scan_networks_never_forces_rescan(monkeypatch):
    # A forced rescan while the AP is up drops the hotspot — must never happen
    # during the wizard.
    calls = _capture(monkeypatch)
    WiFiService(country="US").scan_networks()
    for c in (c for c in calls if "list" in c):
        assert _rescan_arg(c) != "yes"


def test_prescan_forces_fresh_rescan(monkeypatch):
    # The pre-AP scan (radio free) is the ONE place a fresh rescan is allowed.
    calls = _capture(monkeypatch, output="X:50:WPA2\n")
    WiFiService(country="US").prescan()
    list_calls = [c for c in calls if "list" in c]
    assert list_calls and any(_rescan_arg(c) == "yes" for c in list_calls)
