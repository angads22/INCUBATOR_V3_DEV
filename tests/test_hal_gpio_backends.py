"""sysfs (tmp root), mock, RPi.GPIO (fake module) backends and the auto-selection order."""

import logging
import sys

import pytest

from app.hal.gpio import GpioBackendUnavailable, MockGpioBackend, available_backends, get_gpio_backend, parse_pin
from app.hal.gpio.rpi import RpiGpioBackend
from app.hal.gpio.sysfs import SysfsGpioBackend
from tests.test_hal_fakes import make_gpiod_v2, make_rpi_gpio


# ---------------------------------------------------------------------------
# sysfs
# ---------------------------------------------------------------------------


def make_sysfs(tmp_path, *, base=512, chip_index=0, link_chardev=True, gpios=()):
    root = tmp_path / "gpio"
    root.mkdir()
    (root / "export").write_text("")
    (root / "unexport").write_text("")
    chip = root / f"gpiochip{base}"
    chip.mkdir()
    (chip / "base").write_text(f"{base}\n")
    (chip / "label").write_text("pinctrl-bcm2835\n")
    (chip / "ngpio").write_text("58\n")
    if link_chardev:
        (chip / "device" / f"gpiochip{chip_index}").mkdir(parents=True)
    for n in gpios:
        d = root / f"gpio{n}"
        d.mkdir()
        (d / "direction").write_text("in\n")
        (d / "value").write_text("0\n")
    return root


def test_sysfs_is_available_only_with_writable_export(tmp_path):
    from app.hal.gpio import sysfs

    assert sysfs.is_available(str(tmp_path / "nope")) is False
    root = make_sysfs(tmp_path)
    assert sysfs.is_available(str(root)) is True
    with pytest.raises(GpioBackendUnavailable, match="sysfs"):
        get_gpio_backend("sysfs", sysfs_root=str(tmp_path / "nope"))


def test_sysfs_output_uses_chip_base_plus_offset(tmp_path):
    root = make_sysfs(tmp_path, base=512, gpios=(529,))
    backend = get_gpio_backend("sysfs", sysfs_root=str(root))
    assert isinstance(backend, SysfsGpioBackend) and backend.name == "sysfs"

    line = backend.request_output(parse_pin("17"), initial=False)
    assert (root / "export").read_text() == "529"
    assert (root / "gpio529" / "direction").read_text() == "low"
    line.set(True)
    assert (root / "gpio529" / "value").read_text() == "1"
    assert line.get() is True
    line.set(False)
    assert (root / "gpio529" / "value").read_text() == "0"
    assert line.get() is False
    line.release()
    assert (root / "unexport").read_text() == "529"


def test_sysfs_initial_high_and_name_pin(tmp_path):
    root = make_sysfs(tmp_path, base=512, gpios=(539,))
    backend = SysfsGpioBackend(root=str(root))
    backend.request_output(parse_pin("GPIO27"), initial=True)
    assert (root / "gpio539" / "direction").read_text() == "high"
    with pytest.raises(ValueError):
        backend.request_output(parse_pin("PA12"), initial=True)


def test_sysfs_input(tmp_path, caplog):
    root = make_sysfs(tmp_path, base=512, gpios=(530,))
    backend = SysfsGpioBackend(root=str(root))
    with caplog.at_level(logging.DEBUG, logger="app.hal.gpio.sysfs"):
        line = backend.request_input(parse_pin("18"), pull="up")
    assert (root / "gpio530" / "direction").read_text() == "in"
    assert line.get() is False
    (root / "gpio530" / "value").write_text("1\n")
    assert line.get() is True
    assert "pull-up" in caplog.text  # sysfs cannot set bias and says so


def test_sysfs_base_fallback_without_chardev_link(tmp_path):
    root = make_sysfs(tmp_path, base=0, link_chardev=False, gpios=(17,))
    backend = SysfsGpioBackend(root=str(root))
    assert backend.chip_base("gpiochip0") == 0
    backend.request_output(parse_pin("17"), initial=False)
    assert (root / "export").read_text() == "17"
    assert backend.chips()[0]["base"] == 0 and backend.chips()[0]["num_lines"] == 58


def test_sysfs_already_exported_line_is_not_unexported(tmp_path):
    root = make_sysfs(tmp_path, base=512, gpios=(529,))
    backend = SysfsGpioBackend(root=str(root))
    # Simulate the kernel's EBUSY on export (line already exported by someone
    # else): the write fails, the pre-existing gpio529 is still driven, but it
    # is not unexported on release.
    (root / "export").unlink()
    (root / "export").mkdir()
    line = backend.request_output(parse_pin("17"), initial=False)
    assert (root / "gpio529" / "direction").read_text() == "low"
    line.release()
    assert (root / "unexport").read_text() == ""


def test_sysfs_missing_attributes_raise_runtime_error(tmp_path):
    root = make_sysfs(tmp_path, base=512)  # no gpio529 dir will appear (no kernel)
    backend = SysfsGpioBackend(root=str(root), settle_seconds=0.0)
    with pytest.raises(RuntimeError):
        backend.request_output(parse_pin("17"), initial=False)


def test_sysfs_explicit_chip_and_close(tmp_path):
    root = make_sysfs(tmp_path, base=512, chip_index=4, gpios=(516, 517))
    backend = SysfsGpioBackend(root=str(root))
    a = backend.request_output(parse_pin("gpiochip4:4"), initial=False)
    b = backend.request_output(parse_pin("/dev/gpiochip4:5"), initial=False)
    assert a.number == 516 and b.number == 517
    backend.close()
    assert (root / "unexport").read_text() == "517"  # last write wins; both released
    assert backend._lines == []


# ---------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------


def test_mock_backend_lines():
    backend = get_gpio_backend("mock")
    assert isinstance(backend, MockGpioBackend) and backend.name == "mock"
    out = backend.request_output(parse_pin("17"), initial=True)
    assert out.get() is True
    out.set(False)
    assert out.get() is False
    inp = backend.request_input(parse_pin("18"), pull="up")
    assert inp.get() is True  # pulled up = idle HIGH
    backend.set_input_value("18", False)
    assert inp.get() is False
    down = backend.request_input(parse_pin("GPIO19"), pull="down")
    assert down.get() is False
    assert set(backend.lines) == {"17", "18", "GPIO19"}
    out.release()
    assert "17" not in backend.lines
    with pytest.raises(KeyError):
        backend.set_input_value("17", True)
    backend.close()
    assert backend.lines == {}


# ---------------------------------------------------------------------------
# RPi.GPIO
# ---------------------------------------------------------------------------


def test_rpi_backend_call_shapes(monkeypatch, tmp_path):
    pkg, gpio = make_rpi_gpio()
    monkeypatch.setitem(sys.modules, "RPi", pkg)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    empty_dev = tmp_path / "dev"
    empty_dev.mkdir()
    backend = get_gpio_backend("auto", dev_dir=str(empty_dev), sysfs_root=str(tmp_path / "nosysfs"))
    assert isinstance(backend, RpiGpioBackend) and backend.name == "rpi"
    assert ("setmode", gpio.BCM) in gpio.calls and ("setwarnings", False) in gpio.calls

    heater = backend.request_output(parse_pin("17"), initial=True)
    assert ("setup", 17, gpio.OUT, gpio.HIGH, None) in gpio.calls
    heater.set(False)
    assert gpio.calls[-1] == ("output", 17, gpio.LOW)
    assert heater.get() is False
    heater.set(True)
    assert gpio.calls[-1] == ("output", 17, gpio.HIGH)
    assert heater.get() is True

    button = backend.request_input(parse_pin("GPIO18"), pull="up")
    assert ("setup", 18, gpio.IN, None, gpio.PUD_UP) in gpio.calls
    assert button.get() is True
    gpio.levels[18] = gpio.LOW
    assert button.get() is False

    with pytest.raises(ValueError):
        backend.request_output(parse_pin("PA12"), initial=False)

    heater.release()
    assert ("cleanup", 17) in gpio.calls
    backend.close()
    assert ("cleanup", 18) in gpio.calls


def test_explicit_rpi_without_module_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "RPi", None)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", None)
    with pytest.raises(GpioBackendUnavailable, match="RPi.GPIO"):
        get_gpio_backend("rpi")


# ---------------------------------------------------------------------------
# auto ordering
# ---------------------------------------------------------------------------


@pytest.fixture
def no_hw_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "gpiod", None)
    monkeypatch.setitem(sys.modules, "gpiod.line", None)
    monkeypatch.setitem(sys.modules, "RPi", None)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", None)


def test_auto_order_gpiod_first(monkeypatch, tmp_path, no_hw_modules):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "gpiochip0").touch()
    fake = make_gpiod_v2({str(dev / "gpiochip0"): {17: "GPIO17"}})
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    pkg, gpio = make_rpi_gpio()
    monkeypatch.setitem(sys.modules, "RPi", pkg)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    root = make_sysfs(tmp_path)
    assert get_gpio_backend("auto", dev_dir=str(dev), sysfs_root=str(root)).name == "gpiod"
    assert available_backends(dev_dir=str(dev), sysfs_root=str(root)) == {"gpiod": True, "rpi": True, "sysfs": True, "mock": True}


def test_auto_order_rpi_when_no_gpiod_module(monkeypatch, tmp_path, no_hw_modules):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "gpiochip0").touch()  # chips exist, but no python gpiod
    pkg, gpio = make_rpi_gpio()
    monkeypatch.setitem(sys.modules, "RPi", pkg)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    root = make_sysfs(tmp_path)
    assert get_gpio_backend("auto", dev_dir=str(dev), sysfs_root=str(root)).name == "rpi"


def test_auto_order_sysfs_then_mock(monkeypatch, tmp_path, no_hw_modules, caplog):
    dev = tmp_path / "dev"
    dev.mkdir()
    root = make_sysfs(tmp_path)
    assert get_gpio_backend("auto", dev_dir=str(dev), sysfs_root=str(root)).name == "sysfs"
    with caplog.at_level(logging.INFO, logger="app.hal.gpio"):
        backend = get_gpio_backend("auto", dev_dir=str(dev), sysfs_root=str(tmp_path / "nosysfs"))
    assert backend.name == "mock"
    assert "mock" in caplog.text.lower()


def test_unknown_backend_name_raises():
    with pytest.raises(GpioBackendUnavailable, match="unknown"):
        get_gpio_backend("wiringpi")
