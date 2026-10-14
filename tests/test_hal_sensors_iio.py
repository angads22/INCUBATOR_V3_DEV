"""Linux IIO sensor backend against a fake sysfs tree."""

import pytest

from app.hal.sensors import get_sensor
from app.hal.sensors.iio import IioSensor, find_iio_device, list_iio_devices, read_channel, resolve_iio_device


def make_device(root, index, name, files):
    dev = root / f"iio:device{index}"
    dev.mkdir(parents=True)
    (dev / "name").write_text(f"{name}\n")
    for fname, value in files.items():
        (dev / fname).write_text(f"{value}\n")
    return dev


def test_dht11_driver_milli_units(tmp_path):
    dev = make_device(tmp_path, 0, "dht11", {"in_temp_input": 37500, "in_humidityrelative_input": 55000})
    sensor = IioSensor(str(dev))
    assert sensor.name == "iio:dht11"
    r = sensor.read()
    assert r["ok"] is True and r["mock"] is False and r["sensor"] == "iio:dht11"
    assert r["temperature_c"] == 37.5 and r["humidity_pct"] == 55.0


def test_input_in_plain_units_is_kept(tmp_path):
    dev = make_device(tmp_path, 0, "fake", {"in_temp_input": "37.2", "in_humidityrelative_input": "48.5"})
    r = IioSensor(str(dev)).read()
    assert (r["temperature_c"], r["humidity_pct"]) == (37.2, 48.5)


def test_raw_scale_offset(tmp_path):
    dev = make_device(
        tmp_path,
        0,
        "sht3x",
        {
            "in_temp_raw": 1234,
            "in_temp_offset": -234,
            "in_temp_scale": "0.01",  # (1234 - 234) * 0.01 = 10.0
            "in_humidityrelative_raw": 2500,
            "in_humidityrelative_scale": 10,  # 25000 -> milli-units -> 25.0
        },
    )
    r = IioSensor(str(dev)).read()
    assert r["ok"] and r["temperature_c"] == 10.0 and r["humidity_pct"] == 25.0


def test_raw_without_scale_uses_milli_heuristic(tmp_path):
    dev = make_device(tmp_path, 0, "x", {"in_temp_raw": 37400, "in_humidityrelative_raw": 60})
    r = IioSensor(str(dev)).read()
    assert (r["temperature_c"], r["humidity_pct"]) == (37.4, 60.0)


def test_indexed_channel_uses_type_scale(tmp_path):
    dev = make_device(tmp_path, 0, "x", {"in_temp0_raw": 2000, "in_temp_scale": "0.01", "in_humidityrelative0_input": 50000})
    assert read_channel(str(dev), str(dev / "in_temp0_raw")) == 20.0
    r = IioSensor(str(dev)).read()
    assert r["ok"] and r["temperature_c"] == 20.0 and r["humidity_pct"] == 50.0


def test_unreadable_value_reports_not_ok(tmp_path):
    dev = make_device(tmp_path, 0, "dht11", {"in_temp_input": 37500, "in_humidityrelative_input": 55000})
    sensor = IioSensor(str(dev))
    (dev / "in_temp_input").unlink()  # EIO from the driver shows up as a failed read
    r = sensor.read()
    assert r["ok"] is False and "error" in r and r["temperature_c"] is None
    (dev / "in_temp_input").write_text("garbage\n")
    assert sensor.read()["ok"] is False


def test_humidity_failure_is_not_ok_but_keeps_temperature(tmp_path):
    dev = make_device(tmp_path, 0, "dht11", {"in_temp_input": 37500, "in_humidityrelative_input": 55000})
    sensor = IioSensor(str(dev))
    (dev / "in_humidityrelative_input").unlink()
    r = sensor.read()
    assert r["ok"] is False and r["temperature_c"] == 37.5 and r["humidity_pct"] is None


def test_device_without_temperature_rejected(tmp_path):
    dev = make_device(tmp_path, 0, "light", {"in_illuminance_input": 10})
    with pytest.raises(RuntimeError):
        IioSensor(str(dev))


def test_discovery_prefers_device_with_both_channels(tmp_path):
    make_device(tmp_path, 0, "cpu_thermal", {"in_temp_input": 45000})
    both = make_device(tmp_path, 1, "dht11", {"in_temp_input": 37500, "in_humidityrelative_input": 55000})
    make_device(tmp_path, 10, "other", {"in_temp_input": 1, "in_humidityrelative_input": 2})
    assert [p.rsplit("/", 1)[1] for p in list_iio_devices(str(tmp_path))] == ["iio:device0", "iio:device1", "iio:device10"]
    assert find_iio_device(str(tmp_path)) == str(both)
    assert find_iio_device(str(tmp_path), require_humidity=False) == str(tmp_path / "iio:device0")
    assert find_iio_device(str(tmp_path / "missing")) is None


def test_resolve_iio_device_forms(tmp_path):
    dev = make_device(tmp_path, 3, "dht11", {"in_temp_input": 1})
    assert resolve_iio_device("iio:device3", str(tmp_path)) == str(dev)
    assert resolve_iio_device("3", str(tmp_path)) == str(dev)
    assert resolve_iio_device("dht11", str(tmp_path)) == str(dev)
    assert resolve_iio_device("DHT11", str(tmp_path)) == str(dev)
    assert resolve_iio_device(str(dev), str(tmp_path)) == str(dev)
    assert resolve_iio_device("iio:device9", str(tmp_path)) is None
    assert resolve_iio_device("", str(tmp_path)) is None


def test_get_sensor_iio_auto_and_forced(tmp_path):
    make_device(tmp_path, 0, "cpu_thermal", {"in_temp_input": 45000})
    make_device(tmp_path, 1, "dht11", {"in_temp_input": 37500, "in_humidityrelative_input": 55000})
    empty_dev = tmp_path / "dev"
    empty_dev.mkdir()
    s = get_sensor("auto", iio_root=str(tmp_path), dev_dir=str(empty_dev))
    assert s is not None and s.name == "iio:dht11"
    forced = get_sensor("iio", iio_device="iio:device0", iio_root=str(tmp_path))
    assert forced is not None and forced.name == "iio:cpu_thermal"
    r = forced.read()
    assert r["ok"] and r["temperature_c"] == 45.0 and r["humidity_pct"] is None
    assert get_sensor("iio", iio_device="iio:device7", iio_root=str(tmp_path)) is None
    assert get_sensor("iio", iio_root=str(tmp_path / "none")) is None
