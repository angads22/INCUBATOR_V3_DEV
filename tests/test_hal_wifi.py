"""Wi-Fi interface discovery and AP-capability parsing."""

import subprocess

from app.hal.wifi import parse_iw_modes, phy_name, supports_ap_mode, wifi_interface, wifi_interfaces

IW_WITH_AP = """Wiphy phy0
\twiphy index: 0
\tSupported interface modes:
\t\t * IBSS
\t\t * managed
\t\t * AP
\t\t * P2P-client
\t\t * P2P-GO
\t\t * P2P-device
\tsoftware interface modes (can always be added):
\t\t * AP/VLAN
\t\t * monitor
\tvalid interface combinations:
"""

IW_WITHOUT_AP = """Wiphy phy0
\tSupported interface modes:
\t\t * managed
\t\t * P2P-client
\tsoftware interface modes (can always be added):
\t\t * monitor
"""


def make_net(tmp_path):
    (tmp_path / "eth0").mkdir()
    (tmp_path / "lo").mkdir()
    (tmp_path / "wlan0" / "wireless").mkdir(parents=True)
    (tmp_path / "wlp2s0" / "phy80211").mkdir(parents=True)
    (tmp_path / "wlp2s0" / "phy80211" / "name").write_text("phy1\n")
    (tmp_path / "ap0" / "phy80211").mkdir(parents=True)
    return tmp_path


def test_wifi_interfaces(tmp_path):
    root = make_net(tmp_path)
    assert wifi_interfaces(str(root)) == ["wlan0", "wlp2s0", "ap0"]
    assert wifi_interfaces(str(tmp_path / "missing")) == []
    assert phy_name("wlp2s0", str(root)) == "phy1"
    assert phy_name("wlan0", str(root)) is None


def test_wifi_interface_env_detected_default(tmp_path, monkeypatch):
    root = make_net(tmp_path)
    assert wifi_interface(str(root), env={"WIFI_IFACE": " wlx00 "}) == "wlx00"
    assert wifi_interface(str(root), env={}) == "wlan0"
    (tmp_path / "wlan0" / "wireless").rmdir()
    (tmp_path / "wlan0").rmdir()
    assert wifi_interface(str(root), env={}) == "wlp2s0"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert wifi_interface(str(empty), env={}) == "wlan0"
    monkeypatch.setenv("WIFI_IFACE", "wlan9")
    assert wifi_interface(str(empty)) == "wlan9"
    monkeypatch.delenv("WIFI_IFACE")
    assert wifi_interface(str(empty)) == "wlan0"


def test_parse_iw_modes():
    assert parse_iw_modes(IW_WITH_AP) == ["IBSS", "managed", "AP", "P2P-client", "P2P-GO", "P2P-device"]
    assert "AP" not in parse_iw_modes(IW_WITHOUT_AP)
    assert parse_iw_modes("") == []


def test_supports_ap_mode(tmp_path):
    root = make_net(tmp_path)
    calls = []

    def run_ok(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=IW_WITH_AP, stderr="")

    assert supports_ap_mode("wlp2s0", str(root), run=run_ok) is True
    assert calls[-1] == ["iw", "phy", "phy1", "info"]
    # no phy name known -> iw list
    assert supports_ap_mode("wlan0", str(root), run=run_ok) is True
    assert calls[-1] == ["iw", "list"]

    def run_no_ap(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=IW_WITHOUT_AP, stderr="")

    assert supports_ap_mode("wlp2s0", str(root), run=run_no_ap) is False

    def run_missing(cmd, **kw):
        raise FileNotFoundError("iw")

    assert supports_ap_mode("wlp2s0", str(root), run=run_missing) is None

    def run_fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nl80211 not found")

    assert supports_ap_mode("wlp2s0", str(root), run=run_fail) is None

    def run_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    assert supports_ap_mode("wlp2s0", str(root), run=run_timeout) is None

    def run_unparseable(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="Wiphy phy1\n", stderr="")

    assert supports_ap_mode("wlp2s0", str(root), run=run_unparseable) is None


def test_supports_ap_mode_defaults_to_detected_iface(tmp_path):
    root = make_net(tmp_path)
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=IW_WITH_AP, stderr="")

    assert supports_ap_mode(None, str(root), run=run) is True
    assert seen[-1] == ["iw", "list"]  # wlan0 has no phy80211/name
