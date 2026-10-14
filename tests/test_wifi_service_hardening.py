"""WiFiService on any box: honest without NetworkManager, safe with it.

* ``INCUBATOR_NMCLI_BIN`` picks the nmcli binary (tests point it nowhere).
* A cached ``nmcli general`` probe decides between the real thing, an explicit
  mock mode (GPIO_MOCK / INCUBATOR_WIFI_MOCK) and an honest failure.
* Every subprocess call has a timeout; joins are bounded.
* SSIDs may contain any printable character ("John's iPhone").
* The purge only removes OUR AP profiles; home networks named like hotspots stay.
* The device never mistakes its own AP for the home network.
"""

from __future__ import annotations

import dataclasses
import subprocess

import pytest

import app.services.wifi_service as ws
from app.services.wifi_service import WiFiService, valid_ssid


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """Fresh probe cache, no radio prep, virtual clock. Yields the real _prepare_radio."""
    ws.reset_nm_probe_cache()
    orig_prepare = WiFiService._prepare_radio
    monkeypatch.setattr(WiFiService, "_prepare_radio", lambda self: None)
    monkeypatch.setattr(ws, "time", _FakeClock())
    monkeypatch.delenv(ws.WIFI_MOCK_ENV, raising=False)
    yield orig_prepare
    ws.reset_nm_probe_cache()


class _Recorder:
    """subprocess.run stand-in: records (argv, kwargs); answers from tables."""

    def __init__(self, answers=None, rc_for=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.answers = answers or {}
        self.rc_for = rc_for or {}

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append((list(cmd), dict(kwargs)))
        joined = " ".join(cmd)
        out = ""
        for key, value in self.answers.items():
            if key in joined:
                out = value
                break
        rc = 0
        for key, value in self.rc_for.items():
            if key in joined:
                rc = value
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    @property
    def argv(self) -> list[list[str]]:
        return [c for c, _ in self.calls]


def _wire(monkeypatch, recorder: _Recorder) -> _Recorder:
    monkeypatch.setattr(ws.subprocess, "run", recorder)
    return recorder


def _real_device_settings(monkeypatch):
    """Pretend we are an appliance: GPIO_MOCK off and no Wi-Fi mock flag."""
    monkeypatch.setattr(ws, "settings", dataclasses.replace(ws.settings, gpio_mock=False))
    monkeypatch.delenv(ws.WIFI_MOCK_ENV, raising=False)


# ── SSID rule ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ssid", [
    "John's iPhone", "Smith Family (2.4GHz)", "NETGEAR&Co", "Barn+Office",
    "Wi-Fi 5G: Home", "Café-Réseau", "日本語ネット", "x", "é" * 16,  # 32 bytes
])
def test_ssid_rule_accepts_printable_names(ssid):
    assert valid_ssid(ssid) is True


@pytest.mark.parametrize("ssid", [
    "", "   ", "x" * 33, "é" * 17,            # empty / blank / 33 and 34 bytes
    "a\x00b", "bad\nssid", "tab\tbed", "\x7f", None, 42,
])
def test_ssid_rule_rejects_control_chars_and_bad_lengths(ssid):
    assert valid_ssid(ssid) is False


def test_connect_rejects_bad_ssid_before_any_nmcli(monkeypatch):
    rec = _wire(monkeypatch, _Recorder())
    assert WiFiService(country="US").connect_client("bad\nname", "pw12345678") is False
    assert rec.calls == []


# ── nmcli binary override + timeouts ─────────────────────────────────────────

def test_nmcli_bin_env_overrides_executable_but_keeps_argv0(monkeypatch):
    rec = _wire(monkeypatch, _Recorder())
    monkeypatch.setenv(ws.NMCLI_BIN_ENV, "/opt/nm/bin/nmcli")
    ws._nmcli("general", check=False)
    cmd, kwargs = rec.calls[-1]
    assert cmd[0] == "nmcli" and kwargs["executable"] == "/opt/nm/bin/nmcli"
    monkeypatch.delenv(ws.NMCLI_BIN_ENV)
    ws._nmcli("general", check=False)
    assert "executable" not in rec.calls[-1][1]


def test_every_nmcli_call_carries_a_timeout(monkeypatch):
    rec = _wire(monkeypatch, _Recorder())
    assert WiFiService(country="US").start_hotspot("Incubator-AB12", "") is True
    assert rec.calls, "expected nmcli calls"
    for cmd, kwargs in rec.calls:
        assert kwargs.get("timeout", 0) > 0, f"no timeout on {cmd}"
    probe = next(k for c, k in rec.calls if c[1:5] == ["-t", "-f", "RUNNING", "general"])
    assert probe["timeout"] == ws.NM_PROBE_TIMEOUT
    up = next(k for c, k in rec.calls if c[1:3] == ["connection", "up"])
    assert up["timeout"] == ws.NMCLI_CONNECT_TIMEOUT


def test_rfkill_and_iw_calls_have_timeouts(monkeypatch, _fresh):
    rec = _wire(monkeypatch, _Recorder())
    svc = WiFiService(country="US")
    _fresh(svc)   # the real _prepare_radio
    tools = [(c, k) for c, k in rec.calls if c[0] in ("rfkill", "iw")]
    assert len(tools) == 2
    assert all(k.get("timeout", 0) > 0 for _, k in tools)


# ── NetworkManager availability: mock vs honest failure ──────────────────────

def test_missing_nmcli_in_mock_mode_simulates_a_coherent_network(monkeypatch):
    monkeypatch.setenv(ws.NMCLI_BIN_ENV, "/nonexistent/nmcli")   # real subprocess → FileNotFoundError
    assert ws.nm_available() is False
    svc = WiFiService(country="US")
    assert svc.start_hotspot("Incubator-AB12", "") is True
    assert svc.is_hotspot_up("Incubator-AB12", False) is True
    assert svc.hotspot_status()["active"] is True
    assert svc.get_connected_ssid() is None            # hosting the AP ≠ online
    assert svc.connect_client("HomeNet", "pw12345678") is True
    assert svc.get_connected_ssid() == "HomeNet"
    assert svc.hotspot_status()["active"] is False     # single radio: AP gone
    assert svc.has_saved_profile("HomeNet") is True
    assert svc.forget_all_networks() == 0
    assert svc.get_connected_ssid() is None


def test_missing_nmcli_on_a_real_device_fails_honestly(monkeypatch, caplog):
    _real_device_settings(monkeypatch)
    monkeypatch.setenv(ws.NMCLI_BIN_ENV, "/nonexistent/nmcli")
    svc = WiFiService(country="US")
    with caplog.at_level("ERROR"):
        assert svc.start_hotspot("Incubator-AB12", "") is False
        assert svc.connect_client("HomeNet", "pw12345678") is False
        assert svc.try_saved_connection("HomeNet") is False
        assert svc.get_connected_ssid() is None
        assert svc.is_hotspot_up("Incubator-AB12", False) is False
        assert svc.forget_all_networks() == 0
        assert svc.network_debug()["nmcli"] is False
    assert "NetworkManager is not available" in caplog.text


def test_wifi_mock_env_enables_simulation_without_gpio_mock(monkeypatch):
    _real_device_settings(monkeypatch)
    monkeypatch.setenv(ws.WIFI_MOCK_ENV, "true")
    monkeypatch.setenv(ws.NMCLI_BIN_ENV, "/nonexistent/nmcli")
    assert WiFiService(country="US").start_hotspot("Incubator-AB12", "") is True


def test_nm_probe_is_cached_and_reads_rc(monkeypatch):
    rec = _wire(monkeypatch, _Recorder())
    assert ws.nm_available() is True
    assert ws.nm_available() is True
    assert ws.nm_available() is True
    probes = [c for c in rec.argv if c[1:5] == ["-t", "-f", "RUNNING", "general"]]
    assert len(probes) == 1                      # cached
    ws.reset_nm_probe_cache()
    _wire(monkeypatch, _Recorder(rc_for={"RUNNING general": 8}))   # "NetworkManager is not running"
    assert ws.nm_available() is False


def test_nm_probe_timeout_counts_as_unavailable(monkeypatch):
    def hang(cmd, *a, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))

    monkeypatch.setattr(ws.subprocess, "run", hang)
    assert ws.nm_available() is False


# ── Interface + AP address ───────────────────────────────────────────────────

def test_interface_comes_from_wifi_iface_env(monkeypatch):
    monkeypatch.setenv("WIFI_IFACE", "wlp2s0")
    rec = _wire(monkeypatch, _Recorder(answers={"dev wifi list": "HomeNet:70:WPA2\n", "ACTIVE,SSID dev wifi": "yes:HomeNet\n"}))
    svc = WiFiService(country="US")
    assert svc.iface == "wlp2s0"
    svc.start_hotspot("Incubator-AB12", "")
    add = next(c for c in rec.argv if c[1:3] == ["connection", "add"])
    assert add[add.index("ifname") + 1] == "wlp2s0"
    svc.connect_client("HomeNet", "")
    connect = next(c for c in rec.argv if c[1:4] == ["device", "wifi", "connect"])
    assert connect[connect.index("ifname") + 1] == "wlp2s0"
    assert "wlan0" not in {tok for c in rec.argv for tok in c}


def test_default_interface_is_wlan0_without_wireless_sysfs(monkeypatch):
    monkeypatch.delenv("WIFI_IFACE", raising=False)
    monkeypatch.setattr(ws, "wifi_interface", lambda *a, **k: "wlan0")
    assert WiFiService(country="US").iface == "wlan0"


def test_hotspot_pins_ap_address_and_disables_ipv6(monkeypatch):
    rec = _wire(monkeypatch, _Recorder())
    assert WiFiService(country="US", ap_ip="10.42.0.1").start_hotspot("Incubator-AB12", "") is True
    modify = next(c for c in rec.argv if c[1:3] == ["connection", "modify"])
    assert modify[modify.index("ipv4.addresses") + 1] == "10.42.0.1/24"
    assert modify[modify.index("ipv4.method") + 1] == "shared"
    assert modify[modify.index("ipv6.method") + 1] == "disabled"


# ── Purge rule ───────────────────────────────────────────────────────────────

def test_purge_removes_only_our_ap_profiles(monkeypatch):
    profiles = {
        "Johns Hotspot": "infrastructure",
        "IncubatorHome": "infrastructure",
        "Hotspot": "infrastructure",          # NM's default hotspot name → ours
        "Incubator-1A2B": "infrastructure",   # <prefix>-XXXX → ours
        "incubator-hotspot": "ap",
        "MyAP": "ap",                         # unrelated name, AP mode → ours
        "Barn-Office": "infrastructure",      # <word>-<6 chars> → not the XXXX shape
    }
    listing = "\n".join(f"{n}:802-11-wireless" for n in profiles) + "\nWired connection 1:802-3-ethernet\n"
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        if cmd[1:4] == ["-t", "-f", "NAME,TYPE"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
        if cmd[1] == "-g" and cmd[2] == "802-11-wireless.mode":
            return subprocess.CompletedProcess(cmd, 0, stdout=profiles.get(cmd[-1], "") + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    assert WiFiService(country="US").start_hotspot("Incubator-NEW9", "") is True
    deleted = {c[c.index("delete") + 1] for c in calls if "delete" in c}
    assert deleted == {"Hotspot", "Incubator-1A2B", "incubator-hotspot", "MyAP"}


# ── Own AP is never "the connected network" ──────────────────────────────────

def test_connected_ssid_reports_home_network(monkeypatch):
    _wire(monkeypatch, _Recorder(answers={"ACTIVE,SSID dev wifi": "no:Neighbour\nyes:HomeNet\n"}))
    assert WiFiService(country="US").get_connected_ssid() == "HomeNet"


def test_connected_ssid_excludes_own_ap_by_name_shape(monkeypatch):
    _wire(monkeypatch, _Recorder(answers={"ACTIVE,SSID dev wifi": "yes:Incubator-A3F2\n"}))
    assert WiFiService(country="US").get_connected_ssid() is None


def test_connected_ssid_excludes_the_ssid_we_broadcast(monkeypatch):
    rec = _wire(monkeypatch, _Recorder(answers={"ACTIVE,SSID dev wifi": "yes:Coop-Setup\n"}))
    svc = WiFiService(country="US")
    assert svc.get_connected_ssid() == "Coop-Setup"
    svc._hotspot_ssid = "Coop-Setup"           # we are hosting it
    assert svc.get_connected_ssid() is None
    assert rec.calls


def test_connected_ssid_excludes_active_hotspot_connection(monkeypatch):
    _wire(monkeypatch, _Recorder(answers={
        "ACTIVE,SSID dev wifi": "yes:CustomSetupName\n",
        "connection show --active": "incubator-hotspot:activated\n",
    }))
    assert WiFiService(country="US").get_connected_ssid() is None


def test_scan_hides_only_our_ap_shape(monkeypatch):
    _wire(monkeypatch, _Recorder(answers={"dev wifi list": "Incubator-A3F2:90:\nIncubatorHome:50:WPA2\nJohns Hotspot:40:WPA2\n"}))
    names = [n.ssid for n in WiFiService(country="US").scan_networks()]
    assert names == ["IncubatorHome", "Johns Hotspot"]


# ── Client profiles register the hostname ────────────────────────────────────

def test_direct_connect_tunes_profile_for_mdns_and_dhcp_hostname(monkeypatch):
    rec = _wire(monkeypatch, _Recorder(answers={"dev wifi list": "HomeNet:70:WPA2\n", "ACTIVE,SSID dev wifi": "yes:HomeNet\n"}))
    assert WiFiService(country="US").connect_client("HomeNet", "pw12345678") is True
    mods = [c for c in rec.argv if c[1:3] == ["connection", "modify"] and c[3] == "HomeNet"]
    flat = " ".join(" ".join(c) for c in mods)
    assert "connection.mdns 2" in flat and "ipv4.dhcp-send-hostname yes" in flat


def test_profile_fallback_tunes_profile_too(monkeypatch):
    rec = _wire(monkeypatch, _Recorder(
        answers={"ACTIVE,SSID dev wifi": "yes:HomeNet\n"},
        rc_for={"device wifi connect": 10},
    ))
    assert WiFiService(country="US").connect_client("HomeNet", "pw12345678") is True
    mods = [c for c in rec.argv if c[1:3] == ["connection", "modify"] and c[3] == "HomeNet"]
    flat = " ".join(" ".join(c) for c in mods)
    assert "802-11-wireless.hidden yes" in flat
    assert "connection.mdns 2" in flat and "ipv4.dhcp-send-hostname yes" in flat
    assert "wpa-psk" in flat


# ── forget_all_networks / has_saved_profile / has_ap_support ─────────────────

def test_forget_all_networks_deletes_client_profiles_but_keeps_the_ap(monkeypatch):
    rec = _wire(monkeypatch, _Recorder(answers={
        "NAME,TYPE connection show": "HomeNet:802-11-wireless\nincubator-hotspot:802-11-wireless\nWired connection 1:802-3-ethernet\nBarn:802-11-wireless\n",
    }))
    assert WiFiService(country="US").forget_all_networks() == 2
    deleted = [c[c.index("delete") + 1] for c in rec.argv if "delete" in c]
    assert sorted(deleted) == ["Barn", "HomeNet"]


def test_has_saved_profile_uses_nmcli_rc(monkeypatch):
    _wire(monkeypatch, _Recorder(rc_for={"connection show Gone": 10}))
    svc = WiFiService(country="US")
    assert svc.has_saved_profile("HomeNet") is True
    assert svc.has_saved_profile("Gone") is False
    assert svc.has_saved_profile("bad\x00") is False


def test_has_ap_support_delegates_to_hal(monkeypatch):
    monkeypatch.setattr(ws, "supports_ap_mode", lambda iface: None)
    assert WiFiService(country="US").has_ap_support() is None
    monkeypatch.setattr(ws, "supports_ap_mode", lambda iface: True)
    assert WiFiService(country="US").has_ap_support() is True


def test_network_debug_reports_availability(monkeypatch):
    _wire(monkeypatch, _Recorder())
    info = WiFiService(country="US").network_debug()
    assert info["nmcli"] is True and info["nm_available"] is True and "iface" in info
