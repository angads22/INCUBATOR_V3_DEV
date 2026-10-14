"""get_sensor(): backend names, auto order, DHT fallback, mock/none."""

import logging
import sys

import pytest

from app.hal.sensors import MockSensor, get_sensor, i2c
from app.hal.sensors.dht import DhtSensor, bcm_from_pin
from app.hal.sensors.i2c import AHT20Sensor, BME280Sensor, SHT3xSensor
from tests.test_hal_fakes import FakeAHT20, FakeBME280, FakeBus, FakeSHT3x, make_adafruit


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(i2c, "_sleep", lambda s: None)


@pytest.fixture
def no_iio_no_dht(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "adafruit_dht", None)
    monkeypatch.setitem(sys.modules, "board", None)
    iio = tmp_path / "iio"
    iio.mkdir()
    return str(iio)


def test_none_and_mock():
    assert get_sensor("none") is None
    s = get_sensor("mock")
    assert isinstance(s, MockSensor)
    r = s.read()
    assert r["ok"] and r["mock"] is True and r["sensor"] == "mock"
    assert 37.0 < r["temperature_c"] < 37.8 and 54 < r["humidity_pct"] < 56
    s.close()


def test_explicit_i2c_backend_with_bus_factory(no_iio_no_dht):
    bus = FakeBus({0x44: FakeSHT3x(), 0x76: FakeBME280()})
    s = get_sensor("sht3x", bus_factory=lambda n: bus)
    assert isinstance(s, SHT3xSensor) and s.addr == 0x44
    s = get_sensor("bme280", i2c_addr=0x76, bus_factory=lambda n: bus)
    assert isinstance(s, BME280Sensor)
    # forced address where the wanted chip is not
    assert get_sensor("bme280", i2c_addr=0x77, bus_factory=lambda n: bus) is None
    assert get_sensor("aht20", bus_factory=lambda n: bus) is None


def test_auto_probes_i2c_in_order(no_iio_no_dht):
    bus = FakeBus({0x38: FakeAHT20(), 0x76: FakeBME280()})
    opened = []

    def factory(n):
        opened.append(n)
        return bus

    s = get_sensor("auto", i2c_bus=3, iio_root=no_iio_no_dht, bus_factory=factory)
    assert isinstance(s, AHT20Sensor) and opened == [3]
    s = get_sensor("auto", i2c_addr=0x76, iio_root=no_iio_no_dht, bus_factory=factory)
    assert isinstance(s, BME280Sensor)


def test_auto_closes_bus_when_nothing_found(no_iio_no_dht):
    bus = FakeBus({})
    assert get_sensor("auto", iio_root=no_iio_no_dht, bus_factory=lambda n: bus) is None
    assert bus.closed is True


def test_auto_skips_i2c_without_device_node(no_iio_no_dht, tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    assert get_sensor("auto", iio_root=no_iio_no_dht, dev_dir=str(dev)) is None
    assert get_sensor("sht3x", dev_dir=str(dev)) is None


def test_i2c_device_node_without_smbus2_is_skipped(no_iio_no_dht, tmp_path, monkeypatch, caplog):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "i2c-1").touch()
    monkeypatch.setitem(sys.modules, "smbus2", None)
    with caplog.at_level(logging.WARNING, logger="app.hal.sensors"):
        assert get_sensor("auto", iio_root=no_iio_no_dht, dev_dir=str(dev)) is None
    assert "smbus2" in caplog.text


def test_bus_factory_error_is_swallowed(no_iio_no_dht):
    def boom(n):
        raise OSError("no bus")

    assert get_sensor("sht3x", bus_factory=boom) is None


def test_unknown_backend_falls_back_to_auto(no_iio_no_dht, tmp_path, caplog):
    dev = tmp_path / "dev"
    dev.mkdir()
    with caplog.at_level(logging.WARNING, logger="app.hal.sensors"):
        assert get_sensor("bogus", iio_root=no_iio_no_dht, dev_dir=str(dev)) is None
    assert "unknown SENSOR_BACKEND" in caplog.text


# ---------------------------------------------------------------------------
# DHT (fake adafruit_dht + board)
# ---------------------------------------------------------------------------


def test_dht_via_fake_adafruit(monkeypatch, tmp_path):
    adafruit, board = make_adafruit(37.6, 54.0)
    monkeypatch.setitem(sys.modules, "adafruit_dht", adafruit)
    monkeypatch.setitem(sys.modules, "board", board)
    s = get_sensor("dht", dht_pin="4")
    assert isinstance(s, DhtSensor) and s.name == "dht22" and s.pin == 4
    inst = adafruit.instances[-1]
    assert inst.pin == "D4" and inst.use_pulseio is False
    r = s.read()
    assert r["ok"] and r["temperature_c"] == 37.6 and r["humidity_pct"] == 54.0 and r["sensor"] == "dht22"
    s.close()
    assert inst.exited is True
    # auto falls through to DHT when IIO and I2C find nothing
    iio = tmp_path / "iio"
    iio.mkdir()
    dev = tmp_path / "dev"
    dev.mkdir()
    s2 = get_sensor("auto", iio_root=str(iio), dev_dir=str(dev), dht_pin="GPIO4")
    assert isinstance(s2, DhtSensor)
    assert get_sensor("auto", iio_root=str(iio), dev_dir=str(dev), dht_pin=None) is None


def test_dht_runtime_errors_are_not_ok(monkeypatch):
    adafruit, board = make_adafruit(error=RuntimeError("Checksum did not validate. Try again."))
    monkeypatch.setitem(sys.modules, "adafruit_dht", adafruit)
    monkeypatch.setitem(sys.modules, "board", board)
    s = get_sensor("dht", dht_pin=4)
    r = s.read()
    assert r["ok"] is False and "Checksum" in r["error"]


def test_dht_bad_pin_and_missing_library(monkeypatch):
    adafruit, board = make_adafruit()
    monkeypatch.setitem(sys.modules, "adafruit_dht", adafruit)
    monkeypatch.setitem(sys.modules, "board", board)
    assert get_sensor("dht", dht_pin="PA12") is None
    assert get_sensor("dht", dht_pin="") is None
    assert bcm_from_pin("gpiochip0:4") == 4 and bcm_from_pin("GPIO17") == 17 and bcm_from_pin(5) == 5
    monkeypatch.setitem(sys.modules, "adafruit_dht", None)
    assert get_sensor("dht", dht_pin="4") is None
