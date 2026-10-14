"""User-space I2C drivers over smbus2: SHT3x, AHT20/AHT10, BME280/BMP280, HTU21D/SI7021.

Every driver talks to the bus through the tiny :class:`I2CBus` interface so the
unit tests can substitute a fake that replays datasheet bytes. ``smbus2`` is
imported lazily inside :class:`SMBus2Bus` only.
"""

from __future__ import annotations

import logging
import os
import struct
import time
from typing import Any, Callable

from .. import _util
from .base import Sensor, reading

logger = logging.getLogger(__name__)

I2C_DEV_DIR = "/dev"


def i2c_device_path(bus: int, dev_dir: str | None = None) -> str:
    return os.path.join(dev_dir or I2C_DEV_DIR, f"i2c-{int(bus)}")


def smbus2_available() -> bool:
    return _util.optional_import("smbus2") is not None


# ----------------------------------------------------------------------------
# Bus wrapper
# ----------------------------------------------------------------------------

class I2CBus:
    """Minimal bus primitives. Subclass/fake this in tests."""

    def write_bytes(self, addr: int, data: bytes) -> None:
        raise NotImplementedError

    def read_bytes(self, addr: int, length: int) -> bytes:
        raise NotImplementedError

    def write_byte_data(self, addr: int, register: int, value: int) -> None:
        self.write_bytes(addr, bytes([register & 0xFF, value & 0xFF]))

    def read_byte_data(self, addr: int, register: int) -> int:
        return self.read_block(addr, register, 1)[0]

    def read_block(self, addr: int, register: int, length: int) -> bytes:
        """Write ``register`` then read ``length`` bytes (combined transaction)."""
        self.write_bytes(addr, bytes([register & 0xFF]))
        return self.read_bytes(addr, length)

    def close(self) -> None:
        pass


class SMBus2Bus(I2CBus):
    """Real bus using ``smbus2`` (``i2c_rdwr`` for raw transfers)."""

    def __init__(self, bus_num: int) -> None:
        smbus2 = _util.optional_import("smbus2")
        if smbus2 is None:
            raise RuntimeError("smbus2 not importable (pip install smbus2)")
        self._smbus2 = smbus2
        self._bus = smbus2.SMBus(int(bus_num))
        self.bus_num = int(bus_num)

    def write_bytes(self, addr: int, data: bytes) -> None:
        msg = self._smbus2.i2c_msg.write(addr, list(data))
        self._bus.i2c_rdwr(msg)

    def read_bytes(self, addr: int, length: int) -> bytes:
        msg = self._smbus2.i2c_msg.read(addr, length)
        self._bus.i2c_rdwr(msg)
        return bytes(msg)

    def write_byte_data(self, addr: int, register: int, value: int) -> None:
        self._bus.write_byte_data(addr, register, value)

    def read_byte_data(self, addr: int, register: int) -> int:
        return int(self._bus.read_byte_data(addr, register))

    def read_block(self, addr: int, register: int, length: int) -> bytes:
        return bytes(self._bus.read_i2c_block_data(addr, register, length))

    def close(self) -> None:
        try:
            self._bus.close()
        except Exception:
            pass


def open_bus(bus_num: int) -> I2CBus:
    return SMBus2Bus(bus_num)


# ----------------------------------------------------------------------------
# CRC helpers
# ----------------------------------------------------------------------------

def crc8(data: bytes, *, poly: int = 0x31, init: int = 0xFF) -> int:
    """CRC-8 as used by Sensirion (poly 0x31, init 0xFF: CRC(0xBEEF) == 0x92)
    and by the HTU21D/SI7021 (same polynomial, init 0x00)."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


# ----------------------------------------------------------------------------
# Drivers
# ----------------------------------------------------------------------------

class I2CSensor(Sensor):
    """Common plumbing: bus/address bookkeeping and error-wrapped reads."""

    default_addresses: tuple[int, ...] = ()

    def __init__(self, bus: I2CBus, addr: int, *, owns_bus: bool = False) -> None:
        self.bus = bus
        self.addr = int(addr)
        self._owns_bus = owns_bus
        self._closed = False

    @classmethod
    def probe(cls, bus: I2CBus, addr: int) -> bool:
        raise NotImplementedError

    def _read(self) -> dict[str, Any]:
        raise NotImplementedError

    def read(self) -> dict[str, Any]:
        if self._closed:
            return reading(self.name, ok=False, error="sensor closed")
        try:
            return self._read()
        except Exception as exc:
            logger.debug("%s read failed: %s", self.name, exc)
            return reading(self.name, ok=False, error=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_bus:
            try:
                self.bus.close()
            except Exception:
                pass


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class SHT3xSensor(I2CSensor):
    """Sensirion SHT30/31/35 at 0x44 (ADDR low) or 0x45 (ADDR high)."""

    name = "sht3x"
    default_addresses = (0x44, 0x45)
    CMD_MEASURE_HIGH_REP = bytes([0x24, 0x00])  # single shot, high repeatability, no clock stretching
    CMD_READ_STATUS = bytes([0xF3, 0x2D])
    CMD_SOFT_RESET = bytes([0x30, 0xA2])
    MEASURE_DELAY_S = 0.016

    @classmethod
    def probe(cls, bus: I2CBus, addr: int) -> bool:
        try:
            bus.write_bytes(addr, cls.CMD_READ_STATUS)
            data = bus.read_bytes(addr, 3)
        except Exception:
            return False
        return len(data) == 3 and crc8(bytes(data[:2])) == data[2]

    def _read(self) -> dict[str, Any]:
        self.bus.write_bytes(self.addr, self.CMD_MEASURE_HIGH_REP)
        _sleep(self.MEASURE_DELAY_S)
        data = bytes(self.bus.read_bytes(self.addr, 6))
        if len(data) != 6:
            return reading(self.name, ok=False, error="short read")
        if crc8(data[0:2]) != data[2] or crc8(data[3:5]) != data[5]:
            return reading(self.name, ok=False, error="crc mismatch")
        raw_t = (data[0] << 8) | data[1]
        raw_h = (data[3] << 8) | data[4]
        temp = -45.0 + 175.0 * raw_t / 65535.0
        hum = 100.0 * raw_h / 65535.0
        return reading(self.name, ok=True, temperature_c=temp, humidity_pct=max(0.0, min(100.0, hum)))


class AHT20Sensor(I2CSensor):
    """ASAIR AHT20 (and AHT21/AHT25) at 0x38. The AHT10 shares the protocol but
    sends no CRC byte; set ``verify_crc=False`` for it."""

    name = "aht20"
    default_addresses = (0x38,)
    CMD_INIT = bytes([0xBE, 0x08, 0x00])
    CMD_TRIGGER = bytes([0xAC, 0x33, 0x00])
    CMD_SOFT_RESET = bytes([0xBA])
    STATUS_BUSY = 0x80
    STATUS_CALIBRATED = 0x08
    MEASURE_DELAY_S = 0.08

    def __init__(self, bus: I2CBus, addr: int = 0x38, *, owns_bus: bool = False, verify_crc: bool = True) -> None:
        super().__init__(bus, addr, owns_bus=owns_bus)
        self.verify_crc = verify_crc
        self._initialised = False

    @classmethod
    def _status(cls, bus: I2CBus, addr: int) -> int:
        return bus.read_bytes(addr, 1)[0]

    @classmethod
    def _ensure_calibrated(cls, bus: I2CBus, addr: int) -> bool:
        status = cls._status(bus, addr)
        if status & cls.STATUS_CALIBRATED:
            return True
        bus.write_bytes(addr, cls.CMD_INIT)
        _sleep(0.01)
        return bool(cls._status(bus, addr) & cls.STATUS_CALIBRATED)

    @classmethod
    def probe(cls, bus: I2CBus, addr: int) -> bool:
        try:
            return cls._ensure_calibrated(bus, addr)
        except Exception:
            return False

    def _read(self) -> dict[str, Any]:
        if not self._initialised:
            if not self._ensure_calibrated(self.bus, self.addr):
                return reading(self.name, ok=False, error="sensor not calibrated")
            self._initialised = True
        self.bus.write_bytes(self.addr, self.CMD_TRIGGER)
        _sleep(self.MEASURE_DELAY_S)
        data = bytes(self.bus.read_bytes(self.addr, 7 if self.verify_crc else 6))
        if len(data) < 6:
            return reading(self.name, ok=False, error="short read")
        if data[0] & self.STATUS_BUSY:
            return reading(self.name, ok=False, error="sensor busy")
        if self.verify_crc and (len(data) < 7 or crc8(data[:6]) != data[6]):
            return reading(self.name, ok=False, error="crc mismatch")
        raw_h = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
        raw_t = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
        hum = raw_h * 100.0 / 1048576.0
        temp = raw_t * 200.0 / 1048576.0 - 50.0
        return reading(self.name, ok=True, temperature_c=temp, humidity_pct=max(0.0, min(100.0, hum)))


class BME280Sensor(I2CSensor):
    """Bosch BME280 (humidity + pressure + temperature) / BMP280 (no humidity) at 0x76/0x77.

    Compensation follows the datasheet integer algorithms (BST-BME280-DS002 4.2.3),
    64-bit variant for pressure.
    """

    name = "bme280"
    default_addresses = (0x76, 0x77)
    REG_CHIP_ID = 0xD0
    REG_RESET = 0xE0
    REG_CTRL_HUM = 0xF2
    REG_STATUS = 0xF3
    REG_CTRL_MEAS = 0xF4
    REG_CONFIG = 0xF5
    REG_DATA = 0xF7
    REG_CALIB_00 = 0x88
    REG_CALIB_26 = 0xE1
    CHIP_IDS = {0x60: "bme280", 0x58: "bmp280", 0x56: "bmp280", 0x57: "bmp280"}
    MEASURE_DELAY_S = 0.012

    def __init__(self, bus: I2CBus, addr: int = 0x76, *, owns_bus: bool = False) -> None:
        super().__init__(bus, addr, owns_bus=owns_bus)
        self.chip_id: int | None = None
        self.has_humidity = False
        self._cal: dict[str, int] = {}
        self._initialised = False

    @classmethod
    def probe(cls, bus: I2CBus, addr: int) -> bool:
        try:
            return bus.read_byte_data(addr, cls.REG_CHIP_ID) in cls.CHIP_IDS
        except Exception:
            return False

    # -- calibration -----------------------------------------------------------
    def _init(self) -> None:
        chip_id = self.bus.read_byte_data(self.addr, self.REG_CHIP_ID)
        if chip_id not in self.CHIP_IDS:
            raise RuntimeError(f"unexpected chip id 0x{chip_id:02X} at 0x{self.addr:02X}")
        self.chip_id = chip_id
        self.has_humidity = chip_id == 0x60
        self.name = self.CHIP_IDS[chip_id]
        cal = bytes(self.bus.read_block(self.addr, self.REG_CALIB_00, 26))
        (t1, t2, t3, p1, p2, p3, p4, p5, p6, p7, p8, p9) = struct.unpack("<HhhHhhhhhhhh", cal[0:24])
        c = {"T1": t1, "T2": t2, "T3": t3, "P1": p1, "P2": p2, "P3": p3, "P4": p4, "P5": p5, "P6": p6, "P7": p7, "P8": p8, "P9": p9}
        if self.has_humidity:
            c["H1"] = cal[25]
            hc = bytes(self.bus.read_block(self.addr, self.REG_CALIB_26, 7))
            c["H2"] = struct.unpack("<h", hc[0:2])[0]
            c["H3"] = hc[2]
            h4 = (hc[3] << 4) | (hc[4] & 0x0F)
            h5 = (hc[5] << 4) | (hc[4] >> 4)
            c["H4"] = h4 - 4096 if h4 & 0x800 else h4
            c["H5"] = h5 - 4096 if h5 & 0x800 else h5
            c["H6"] = struct.unpack("<b", hc[6:7])[0]
        self._cal = c
        # Config: standby irrelevant in forced mode; filter off.
        self.bus.write_byte_data(self.addr, self.REG_CONFIG, 0x00)
        self._initialised = True

    # -- compensation (datasheet) ----------------------------------------------
    def compensate_temperature(self, adc_t: int) -> tuple[float, int]:
        c = self._cal
        var1 = (((adc_t >> 3) - (c["T1"] << 1)) * c["T2"]) >> 11
        var2 = (((((adc_t >> 4) - c["T1"]) * ((adc_t >> 4) - c["T1"])) >> 12) * c["T3"]) >> 14
        t_fine = var1 + var2
        temp = ((t_fine * 5 + 128) >> 8) / 100.0
        return temp, t_fine

    def compensate_pressure(self, adc_p: int, t_fine: int) -> float | None:
        c = self._cal
        var1 = t_fine - 128000
        var2 = var1 * var1 * c["P6"]
        var2 = var2 + ((var1 * c["P5"]) << 17)
        var2 = var2 + (c["P4"] << 35)
        var1 = ((var1 * var1 * c["P3"]) >> 8) + ((var1 * c["P2"]) << 12)
        var1 = ((1 << 47) + var1) * c["P1"] >> 33
        if var1 == 0:
            return None
        p = 1048576 - adc_p
        p = (((p << 31) - var2) * 3125) // var1
        var1 = (c["P9"] * (p >> 13) * (p >> 13)) >> 25
        var2 = (c["P8"] * p) >> 19
        p = ((p + var1 + var2) >> 8) + (c["P7"] << 4)
        return p / 256.0  # Pa

    def compensate_humidity(self, adc_h: int, t_fine: int) -> float:
        c = self._cal
        x = t_fine - 76800
        a = (((adc_h << 14) - (c["H4"] << 20) - (c["H5"] * x)) + 16384) >> 15
        inner = (((x * c["H6"]) >> 10) * (((x * c["H3"]) >> 11) + 32768)) >> 10
        b = (((inner + 2097152) * c["H2"]) + 8192) >> 14
        v = a * b
        v = v - (((((v >> 15) * (v >> 15)) >> 7) * c["H1"]) >> 4)
        v = max(0, min(v, 419430400))
        return (v >> 12) / 1024.0

    # -- measurement -------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self._initialised:
            self._init()
        # Forced mode, oversampling x1 on every channel.
        if self.has_humidity:
            self.bus.write_byte_data(self.addr, self.REG_CTRL_HUM, 0x01)
        self.bus.write_byte_data(self.addr, self.REG_CTRL_MEAS, (1 << 5) | (1 << 2) | 0x01)
        _sleep(self.MEASURE_DELAY_S)
        for _ in range(5):
            status = self.bus.read_byte_data(self.addr, self.REG_STATUS)
            if not status & 0x08:  # measuring bit
                break
            _sleep(0.005)
        data = bytes(self.bus.read_block(self.addr, self.REG_DATA, 8))
        if len(data) < 8:
            return reading(self.name, ok=False, error="short read")
        adc_p = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
        adc_t = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
        adc_h = (data[6] << 8) | data[7]
        if adc_t == 0x80000:
            return reading(self.name, ok=False, error="temperature channel disabled")
        temp, t_fine = self.compensate_temperature(adc_t)
        extra: dict[str, Any] = {}
        pressure = self.compensate_pressure(adc_p, t_fine) if adc_p != 0x80000 else None
        if pressure is not None:
            extra["pressure_hpa"] = round(pressure / 100.0, 2)
        hum = self.compensate_humidity(adc_h, t_fine) if self.has_humidity and adc_h != 0x8000 else None
        return reading(self.name, ok=True, temperature_c=temp, humidity_pct=hum, **extra)


class HTU21DSensor(I2CSensor):
    """TE HTU21D / Silicon Labs SI7021 (and SHT21) at 0x40, no-hold master mode."""

    name = "htu21d"
    default_addresses = (0x40,)
    CMD_TEMP_NO_HOLD = 0xF3
    CMD_HUM_NO_HOLD = 0xF5
    CMD_READ_USER_REG = 0xE7
    CMD_SOFT_RESET = 0xFE
    TEMP_DELAY_S = 0.050
    HUM_DELAY_S = 0.016

    @staticmethod
    def crc(data: bytes) -> int:
        return crc8(data, poly=0x31, init=0x00)

    @classmethod
    def _measure(cls, bus: I2CBus, addr: int, cmd: int, delay: float) -> int | None:
        bus.write_bytes(addr, bytes([cmd]))
        _sleep(delay)
        data = bytes(bus.read_bytes(addr, 3))
        if len(data) != 3 or cls.crc(data[:2]) != data[2]:
            return None
        return ((data[0] << 8) | data[1]) & 0xFFFC  # strip status bits

    @classmethod
    def probe(cls, bus: I2CBus, addr: int) -> bool:
        # 0x40 is shared with the INA219 used for heater-current sensing, so a
        # mere ACK is not enough: require a CRC-valid temperature conversion.
        try:
            return cls._measure(bus, addr, cls.CMD_TEMP_NO_HOLD, cls.TEMP_DELAY_S) is not None
        except Exception:
            return False

    def _read(self) -> dict[str, Any]:
        raw_t = self._measure(self.bus, self.addr, self.CMD_TEMP_NO_HOLD, self.TEMP_DELAY_S)
        if raw_t is None:
            return reading(self.name, ok=False, error="temperature crc mismatch")
        raw_h = self._measure(self.bus, self.addr, self.CMD_HUM_NO_HOLD, self.HUM_DELAY_S)
        if raw_h is None:
            return reading(self.name, ok=False, error="humidity crc mismatch")
        temp = -46.85 + 175.72 * raw_t / 65536.0
        hum = -6.0 + 125.0 * raw_h / 65536.0
        return reading(self.name, ok=True, temperature_c=temp, humidity_pct=max(0.0, min(100.0, hum)))


DRIVERS: dict[str, type[I2CSensor]] = {
    "sht3x": SHT3xSensor,
    "aht20": AHT20Sensor,
    "bme280": BME280Sensor,
    "htu21d": HTU21DSensor,
}
PROBE_ORDER: tuple[str, ...] = ("sht3x", "aht20", "bme280", "htu21d")


def probe_i2c(
    bus: I2CBus,
    *,
    names: tuple[str, ...] | list[str] = PROBE_ORDER,
    addr: int = 0,
    owns_bus: bool = False,
) -> I2CSensor | None:
    """Probe the drivers in order; ``addr`` != 0 restricts every probe to that address."""
    for name in names:
        driver = DRIVERS[name]
        addresses = (addr,) if addr else driver.default_addresses
        for a in addresses:
            try:
                found = driver.probe(bus, a)
            except Exception as exc:  # pragma: no cover - probe() already guards
                logger.debug("%s probe at 0x%02X raised %s", name, a, exc)
                found = False
            if found:
                logger.info("I2C sensor %s found at 0x%02X", name, a)
                return driver(bus, a, owns_bus=owns_bus)
    return None


BusFactory = Callable[[int], I2CBus]
