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


def test_live_scan_wins_when_available(monkeypatch):
    # If a live scan DOES return results, use them (don't get stuck on cache).
    def fake_run(cmd, *a, **k):
        if cmd and cmd[0] == "nmcli" and "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="LiveNet:70:WPA2\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    svc = WiFiService(country="US")
    names = [n.ssid for n in svc.scan_networks()]
    assert names == ["LiveNet"]
