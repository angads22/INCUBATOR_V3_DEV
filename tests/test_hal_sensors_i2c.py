"""User-space I2C drivers against fake devices replaying datasheet bytes."""

import sys

import pytest

from app.hal.sensors import i2c
from app.hal.sensors.i2c import (
    AHT20Sensor,
    BME280Sensor,
    HTU21DSensor,
    SHT3xSensor,
    SMBus2Bus,
    crc8,
    probe_i2c,
)
from tests.test_hal_fakes import (
    BME280_EXAMPLE_ADC_H,
    BME280_HUM_CAL,
    BMP280_EXAMPLE_CAL,
    FakeAHT20,
    FakeBME280,
    FakeBus,
    FakeHTU21D,
    FakeINA219,
    FakeSHT3x,
    make_smbus2,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(i2c, "_sleep", lambda s: None)


# ---------------------------------------------------------------------------
# CRC
# ---------------------------------------------------------------------------


def test_crc8_datasheet_vectors():
    # Sensirion SHT3x datasheet: CRC(0xBEEF) = 0x92 (poly 0x31, init 0xFF)
    assert crc8(b"\xbe\xef") == 0x92
    assert crc8(b"\xbe\xef", poly=0x31, init=0xFF) == 0x92
    # HTU21D datasheet examples (poly 0x31, init 0x00): 0xDC -> 0x79, 0x683A -> 0x7C, 0x4E85 -> 0x6B
    assert HTU21DSensor.crc(b"\xdc") == 0x79
    assert HTU21DSensor.crc(b"\x68\x3a") == 0x7C
    assert HTU21DSensor.crc(b"\x4e\x85") == 0x6B
    assert crc8(b"") == 0xFF


# ---------------------------------------------------------------------------
# SHT3x
# ---------------------------------------------------------------------------


def test_sht3x_read_and_command_shape():
    dev = FakeSHT3x(raw_t=0x6666, raw_h=0x8000)
    bus = FakeBus({0x44: dev})
    sensor = SHT3xSensor(bus, 0x44)
    r = sensor.read()
    assert dev.commands[-1] == b"\x24\x00"  # single shot, high repeatability, no clock stretch
    assert ("r", 0x44, 6) in bus.log
    assert r["ok"] and r["sensor"] == "sht3x" and r["mock"] is False
    assert r["temperature_c"] == pytest.approx(-45 + 175 * 0x6666 / 65535, abs=0.01)
    assert r["humidity_pct"] == pytest.approx(100 * 0x8000 / 65535, abs=0.01)


def test_sht3x_crc_mismatch_is_not_ok():
    bus = FakeBus({0x44: FakeSHT3x(corrupt_crc=True)})
    r = SHT3xSensor(bus, 0x44).read()
    assert r["ok"] is False and r["error"] == "crc mismatch"
    assert r["temperature_c"] is None


def test_sht3x_probe_uses_status_register():
    dev = FakeSHT3x()
    bus = FakeBus({0x45: dev})
    assert SHT3xSensor.probe(bus, 0x45) is True
    assert dev.commands[-1] == b"\xf3\x2d"
    assert SHT3xSensor.probe(bus, 0x44) is False  # nothing there


def test_sht3x_bus_error_never_raises():
    class Dead(FakeSHT3x):
        def read(self, length):
            raise OSError(121, "Remote I/O error")

    r = SHT3xSensor(FakeBus({0x44: Dead()}), 0x44).read()
    assert r["ok"] is False and "Remote I/O" in r["error"]


# ---------------------------------------------------------------------------
# AHT20
# ---------------------------------------------------------------------------


def test_aht20_read_decodes_20_bit_fields():
    dev = FakeAHT20(raw_t=0x70000, raw_h=0x80000)  # 37.5 degC, 50 %
    bus = FakeBus({0x38: dev})
    r = AHT20Sensor(bus, 0x38).read()
    assert dev.commands[-1] == b"\xac\x33\x00"
    assert b"\xbe\x08\x00" not in dev.commands  # already calibrated -> no init
    assert r["ok"] and r["sensor"] == "aht20"
    assert r["temperature_c"] == 37.5 and r["humidity_pct"] == 50.0


def test_aht20_initialises_uncalibrated_sensor():
    dev = FakeAHT20(calibrated=False)
    bus = FakeBus({0x38: dev})
    assert AHT20Sensor.probe(bus, 0x38) is True
    assert dev.commands[0] == b"\xbe\x08\x00"
    r = AHT20Sensor(bus, 0x38).read()
    assert r["ok"]


def test_aht20_crc_checked_unless_disabled():
    dev = FakeAHT20(with_crc=False)
    bus = FakeBus({0x38: dev})
    assert AHT20Sensor(bus, 0x38).read()["ok"] is False
    assert AHT20Sensor(bus, 0x38, verify_crc=False).read()["ok"] is True  # AHT10 mode


def test_aht20_probe_fails_on_empty_address():
    assert AHT20Sensor.probe(FakeBus({}), 0x38) is False


# ---------------------------------------------------------------------------
# BME280 / BMP280
# ---------------------------------------------------------------------------


def hum_double(adc_h, t_fine, c):
    """Datasheet double-precision humidity formula (independent of the integer one)."""
    var_h = t_fine - 76800.0
    var_h = (adc_h - (c["H4"] * 64.0 + c["H5"] / 16384.0 * var_h)) * (
        c["H2"] / 65536.0 * (1.0 + c["H6"] / 67108864.0 * var_h * (1.0 + c["H3"] / 67108864.0 * var_h))
    )
    var_h = var_h * (1.0 - c["H1"] * var_h / 524288.0)
    return max(0.0, min(100.0, var_h))


def test_bme280_datasheet_example():
    dev = FakeBME280(chip_id=0x60)
    bus = FakeBus({0x76: dev})
    sensor = BME280Sensor(bus, 0x76)
    r = sensor.read()
    assert sensor.chip_id == 0x60 and sensor.has_humidity and sensor.name == "bme280"
    assert sensor._cal["T1"] == BMP280_EXAMPLE_CAL["T1"] and sensor._cal["P9"] == BMP280_EXAMPLE_CAL["P9"]
    assert {k: sensor._cal[k] for k in BME280_HUM_CAL} == BME280_HUM_CAL
    # BMP280 datasheet 3.11.3: adc_T=519888, adc_P=415148 -> 25.08 degC, 100653 Pa
    assert r["ok"] and r["temperature_c"] == 25.08
    assert r["pressure_hpa"] == pytest.approx(1006.53, abs=0.01)
    _, t_fine = sensor.compensate_temperature(519888)
    assert t_fine == 128422
    expected_h = hum_double(BME280_EXAMPLE_ADC_H, t_fine, BME280_HUM_CAL)
    assert r["humidity_pct"] == pytest.approx(expected_h, abs=0.05)
    # forced-mode register writes: ctrl_hum then ctrl_meas (osrs x1, mode=forced)
    assert (0xF2, 0x01) in dev.writes and (0xF4, 0x25) in dev.writes


def test_bmp280_has_no_humidity():
    bus = FakeBus({0x77: FakeBME280(chip_id=0x58)})
    sensor = BME280Sensor(bus, 0x77)
    r = sensor.read()
    assert sensor.name == "bmp280" and r["sensor"] == "bmp280"
    assert r["ok"] and r["temperature_c"] == 25.08 and r["humidity_pct"] is None


def test_bme280_probe_by_chip_id():
    bus = FakeBus({0x76: FakeBME280(chip_id=0x60), 0x77: FakeBME280(chip_id=0x58), 0x44: FakeSHT3x()})
    assert BME280Sensor.probe(bus, 0x76) is True
    assert BME280Sensor.probe(bus, 0x77) is True
    assert BME280Sensor.probe(bus, 0x44) is False
    assert BME280Sensor.probe(bus, 0x75) is False


def test_bme280_bad_chip_id_on_read():
    dev = FakeBME280(chip_id=0x12)
    r = BME280Sensor(FakeBus({0x76: dev}), 0x76).read()
    assert r["ok"] is False and "chip id" in r["error"]


# ---------------------------------------------------------------------------
# HTU21D
# ---------------------------------------------------------------------------


def test_htu21d_read_and_commands():
    dev = FakeHTU21D(raw_t=0x7AE1, raw_h=0x8002)
    bus = FakeBus({0x40: dev})
    r = HTU21DSensor(bus, 0x40).read()
    assert dev.commands == [b"\xf3", b"\xf5"]
    assert r["ok"] and r["sensor"] == "htu21d"
    assert r["temperature_c"] == pytest.approx(-46.85 + 175.72 * 0x7AE0 / 65536, abs=0.01)
    assert r["humidity_pct"] == pytest.approx(-6 + 125 * 0x8000 / 65536, abs=0.01)


def test_htu21d_crc_mismatch():
    r = HTU21DSensor(FakeBus({0x40: FakeHTU21D(corrupt=True)}), 0x40).read()
    assert r["ok"] is False and "crc" in r["error"]


def test_htu21d_probe_rejects_other_chip_at_0x40():
    assert HTU21DSensor.probe(FakeBus({0x40: FakeHTU21D()}), 0x40) is True
    assert HTU21DSensor.probe(FakeBus({0x40: FakeINA219()}), 0x40) is False
    assert HTU21DSensor.probe(FakeBus({}), 0x40) is False


# ---------------------------------------------------------------------------
# probe ordering
# ---------------------------------------------------------------------------


def test_probe_order_and_forced_address():
    bus = FakeBus({0x38: FakeAHT20(), 0x76: FakeBME280(), 0x40: FakeHTU21D()})
    assert isinstance(probe_i2c(bus), AHT20Sensor)
    assert isinstance(probe_i2c(bus, addr=0x76), BME280Sensor)
    assert isinstance(probe_i2c(bus, addr=0x40), HTU21DSensor)
    assert probe_i2c(bus, addr=0x45) is None
    assert isinstance(probe_i2c(FakeBus({0x45: FakeSHT3x()})), SHT3xSensor)
    assert probe_i2c(FakeBus({0x40: FakeINA219()})) is None
    assert probe_i2c(FakeBus({})) is None


def test_sensor_close_owns_bus():
    bus = FakeBus({0x44: FakeSHT3x()})
    sensor = probe_i2c(bus, owns_bus=True)
    assert sensor is not None
    sensor.close()
    assert bus.closed is True
    assert sensor.read()["ok"] is False
    bus2 = FakeBus({0x44: FakeSHT3x()})
    SHT3xSensor(bus2, 0x44).close()
    assert bus2.closed is False


# ---------------------------------------------------------------------------
# SMBus2Bus wrapper against a fake smbus2 module
# ---------------------------------------------------------------------------


def test_smbus2_bus_wrapper(monkeypatch):
    dev = FakeSHT3x()
    fake = make_smbus2({0x44: dev, 0x76: FakeBME280()})
    monkeypatch.setitem(sys.modules, "smbus2", fake)
    assert i2c.smbus2_available() is True
    bus = SMBus2Bus(1)
    assert ("open", 1) in fake.calls
    bus.write_bytes(0x44, b"\x24\x00")
    data = bus.read_bytes(0x44, 6)
    assert isinstance(data, bytes) and len(data) == 6
    assert bus.read_byte_data(0x76, 0xD0) == 0x60
    assert len(bus.read_block(0x76, 0x88, 26)) == 26
    bus.write_byte_data(0x76, 0xF4, 0x25)
    with pytest.raises(OSError):
        bus.read_bytes(0x45, 1)
    bus.close()
    assert ("close", 1) in fake.calls
    # whole driver stack over the wrapper
    r = SHT3xSensor(SMBus2Bus(1), 0x44).read()
    assert r["ok"]


def test_smbus2_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)
    assert i2c.smbus2_available() is False
    with pytest.raises(RuntimeError):
        SMBus2Bus(1)
