"""``python -m app.hal`` prints JSON and never raises; app.hal imports no hardware libs."""

import json
import os
import subprocess
import sys

import pytest

import app.hal.__main__ as cli

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_hal_import_does_not_pull_hardware_libraries():
    code = (
        "import sys, app.hal, app.hal.gpio, app.hal.sensors, app.hal.led, app.hal.wifi, app.hal.board;"
        "bad=[m for m in sys.modules if m.split('.')[0] in ('gpiod','smbus2','RPi','adafruit_dht','board','picamera2')];"
        "print(json.dumps(bad))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import json;" + code],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == []


def test_cli_prints_json_report():
    proc = subprocess.run(
        [sys.executable, "-m", "app.hal"],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": REPO_ROOT, "GPIO_MOCK": "true"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert set(report) >= {"board", "gpio", "sensor", "led", "wifi", "python"}
    assert "model" in report["board"] and "family" in report["board"]
    assert "backend" in report["gpio"] and "chips" in report["gpio"] and "available" in report["gpio"]
    assert report["gpio"]["backend"] in ("gpiod", "rpi", "sysfs", "mock")
    assert "sensor" in report["sensor"]
    assert "led" in report["led"] and "sysfs_leds" in report["led"]
    assert "interfaces" in report["wifi"] and "interface" in report["wifi"] and "supports_ap_mode" in report["wifi"]


def test_build_report_survives_failures(monkeypatch):
    import app.hal.board as board_mod
    import app.hal.gpio as gpio_mod
    import app.hal.sensors as sensors_mod

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(board_mod, "detect_board", boom)
    monkeypatch.setattr(gpio_mod, "get_gpio_backend", boom)
    monkeypatch.setattr(sensors_mod, "get_sensor", boom)
    report = cli.build_report()
    assert report["board"] == {"error": "RuntimeError: kaboom"}
    assert report["gpio"]["backend"] is None and "kaboom" in report["gpio"]["error"]
    assert report["sensor"] == {"error": "RuntimeError: kaboom"}
    assert "led" in report and "wifi" in report


def test_build_report_with_custom_settings_and_mock_led(tmp_path, monkeypatch):
    import app.hal.led as led_mod

    leds = tmp_path / "leds"
    (leds / "ACT").mkdir(parents=True)
    (leds / "ACT" / "trigger").write_text("[mmc0] none\n")
    (leds / "ACT" / "brightness").write_text("0\n")
    (leds / "ACT" / "max_brightness").write_text("1\n")
    monkeypatch.setattr(led_mod, "SYSFS_LEDS", str(leds))

    class S:
        gpio_backend = "mock"
        gpio_chip = ""
        sensor_backend = "mock"
        sensor_i2c_bus = 1
        sensor_i2c_addr = 0
        sensor_iio_device = ""
        gpio_dht_pin = "4"
        status_led = "auto"

    report = cli.build_report(S())
    assert report["gpio"]["backend"] == "mock"
    assert report["sensor"]["sensor"] == "mock" and report["sensor"]["reading"]["ok"] is True
    assert report["led"]["led"]["name"] == "ACT"
    assert (leds / "ACT" / "trigger").read_text() == "mmc0"  # restored after the probe


def test_main_returns_zero(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["python"].startswith(str(sys.version_info[0]))


@pytest.mark.parametrize("name", ["detect_board", "BoardInfo", "get_gpio_backend", "parse_pin", "get_sensor", "StatusLed", "wifi_interface", "wifi_interfaces", "supports_ap_mode"])
def test_public_reexports(name):
    import app.hal

    assert hasattr(app.hal, name)
