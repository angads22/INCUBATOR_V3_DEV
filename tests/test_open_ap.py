"""Setup AP is open by default: a blank password yields an unsecured hotspot."""

from app.services import wifi_service as wifi_mod
from app.services.wifi_service import WiFiService


def _capture_nmcli(monkeypatch):
    """Record every nmcli argv; make the hotspot call 'succeed'."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        import subprocess

        if cmd and cmd[0] == "nmcli":
            calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(wifi_mod.subprocess, "run", fake_run)
    return calls


def _hotspot_argv(calls):
    for c in calls:
        if "hotspot" in c:
            return c
    raise AssertionError(f"no hotspot call captured in {calls!r}")


def test_blank_password_makes_open_ap(monkeypatch):
    calls = _capture_nmcli(monkeypatch)
    assert WiFiService(country="US").start_hotspot("Incubator-A3F2", "") is True
    argv = _hotspot_argv(calls)
    assert "password" not in argv          # open network — no WPA key
    assert "Incubator-A3F2" in argv


def test_nonblank_password_still_secures_ap(monkeypatch):
    calls = _capture_nmcli(monkeypatch)
    assert WiFiService(country="US").start_hotspot("Incubator-A3F2", "secret12") is True
    argv = _hotspot_argv(calls)
    assert "password" in argv
    assert "secret12" in argv
