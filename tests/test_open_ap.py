"""Setup AP is open by default: a blank password yields a genuinely open AP.

The AP connection is built explicitly (connection add → modify → up) rather than
via `nmcli device wifi hotspot`, because that shortcut auto-generates a random
WPA key when given no password — i.e. it is never actually open.
"""

import subprocess

from app.services import wifi_service as wifi_mod
from app.services.wifi_service import WiFiService


def _capture_nmcli(monkeypatch):
    """Record every nmcli argv; make each call 'succeed'."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "nmcli":
            calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    return calls


def _flat(calls):
    return [tok for c in calls for tok in c]


def test_blank_password_makes_open_ap(monkeypatch):
    calls = _capture_nmcli(monkeypatch)
    assert WiFiService(country="US").start_hotspot("Incubator-A3F2", "") is True
    flat = _flat(calls)
    # The connection is created with our SSID...
    add = next(c for c in calls if "add" in c)
    assert "Incubator-A3F2" in add
    # ...and NO security is ever attached → genuinely open.
    assert "wpa-psk" not in flat
    assert "802-11-wireless-security.psk" not in flat


def test_nonblank_password_still_secures_ap(monkeypatch):
    calls = _capture_nmcli(monkeypatch)
    assert WiFiService(country="US").start_hotspot("Incubator-A3F2", "secret12") is True
    flat = _flat(calls)
    assert "wpa-psk" in flat
    assert "secret12" in flat
