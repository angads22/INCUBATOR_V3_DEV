"""Fake hardware libraries and devices shared by the HAL tests (no tests here).

Each ``make_*`` returns a ``types.ModuleType`` ready to be injected into
``sys.modules`` with ``monkeypatch.setitem``; the fakes record every call so
tests can assert the exact call shapes the real libraries expect.
"""

from __future__ import annotations

import enum
import errno
import struct
import types
from typing import Any

from app.hal.sensors.i2c import I2CBus, crc8

# ---------------------------------------------------------------------------
# libgpiod 2.x
# ---------------------------------------------------------------------------


def make_gpiod_v2(chips: dict[str, dict[int, str]], *, labels: dict[str, str] | None = None) -> types.ModuleType:
    """``chips`` maps a chip device path to ``{offset: line_name}``."""
    mod = types.ModuleType("gpiod")
    line = types.ModuleType("gpiod.line")

    class Direction(enum.Enum):
        AS_IS = 1
        INPUT = 2
        OUTPUT = 3

    class Value(enum.Enum):
        INACTIVE = 0
        ACTIVE = 1

    class Bias(enum.Enum):
        AS_IS = 1
        UNKNOWN = 2
        DISABLED = 3
        PULL_UP = 4
        PULL_DOWN = 5

    line.Direction, line.Value, line.Bias = Direction, Value, Bias
    mod.line = line
    mod.calls: list[tuple] = []
    mod.requests: list[Any] = []
    mod.chip_opens: list[str] = []

    class LineSettings:
        def __init__(self, direction=Direction.AS_IS, output_value=Value.INACTIVE, bias=Bias.AS_IS, **kw):
            self.direction = direction
            self.output_value = output_value
            self.bias = bias
            self.extra = kw

        def __eq__(self, other):
            return isinstance(other, LineSettings) and (self.direction, self.output_value, self.bias) == (
                other.direction,
                other.output_value,
                other.bias,
            )

        def __repr__(self):
            return f"LineSettings(direction={self.direction}, output_value={self.output_value}, bias={self.bias})"

    class ChipInfo:
        def __init__(self, path):
            self.name = path.rsplit("/", 1)[-1]
            self.label = (labels or {}).get(path, "fake-label")
            self.num_lines = max(chips[path]) + 1 if chips[path] else 0

    class LineInfo:
        def __init__(self, offset, name):
            self.offset = offset
            self.name = name

    class Chip:
        def __init__(self, path):
            if path not in chips:
                raise OSError(errno.ENOENT, f"no such chip {path}")
            self.path = path
            self.closed = False
            mod.chip_opens.append(path)

        def get_info(self):
            return ChipInfo(self.path)

        def line_offset_from_id(self, name):
            for off, n in chips[self.path].items():
                if n == name:
                    return off
            raise ValueError(f"no line {name!r}")

        def get_line_info(self, offset):
            return LineInfo(offset, chips[self.path].get(offset, ""))

        def close(self):
            self.closed = True

    class LineRequest:
        def __init__(self, path, consumer, config):
            self.path = path
            self.consumer = consumer
            self.config = config
            self.values = {off: s.output_value for off, s in config.items()}
            self.released = False

        def set_value(self, offset, value):
            mod.calls.append(("set_value", offset, value))
            self.values[offset] = value

        def get_value(self, offset):
            mod.calls.append(("get_value", offset))
            return self.values[offset]

        def release(self):
            mod.calls.append(("release",))
            self.released = True

    def request_lines(path, consumer=None, config=None):
        if path not in chips:
            raise OSError(errno.ENOENT, f"no such chip {path}")
        mod.calls.append(("request_lines", path, consumer, config))
        req = LineRequest(path, consumer, config)
        mod.requests.append(req)
        return req

    mod.request_lines = request_lines
    mod.LineSettings = LineSettings
    mod.Chip = Chip
    return mod


# ---------------------------------------------------------------------------
# libgpiod 1.x
# ---------------------------------------------------------------------------


def make_gpiod_v1(chips: dict[str, dict[int, str]], *, bias_flags: bool = True) -> types.ModuleType:
    mod = types.ModuleType("gpiod")
    mod.LINE_REQ_DIR_AS_IS = 1
    mod.LINE_REQ_DIR_IN = 2
    mod.LINE_REQ_DIR_OUT = 3
    if bias_flags:
        mod.LINE_REQ_FLAG_BIAS_DISABLE = 8
        mod.LINE_REQ_FLAG_BIAS_PULL_DOWN = 16
        mod.LINE_REQ_FLAG_BIAS_PULL_UP = 32
    mod.calls: list[tuple] = []
    mod.chips_opened: list[Any] = []

    class Line:
        def __init__(self, chip, offset):
            self.chip = chip
            self._offset = offset
            self.requested = None
            self.value = 0
            self.released = False

        def offset(self):
            return self._offset

        def name(self):
            return chips[self.chip.path].get(self._offset, "")

        def request(self, consumer=None, type=None, flags=0, default_vals=None):
            mod.calls.append(("request", self.chip.path, self._offset, consumer, type, flags, default_vals))
            self.requested = {"consumer": consumer, "type": type, "flags": flags, "default_vals": default_vals}
            if default_vals:
                self.value = default_vals[0]

        def set_value(self, value):
            mod.calls.append(("set_value", self._offset, value))
            self.value = value

        def get_value(self):
            mod.calls.append(("get_value", self._offset))
            return self.value

        def release(self):
            mod.calls.append(("release", self._offset))
            self.released = True

    class Chip:
        def __init__(self, path):
            if path not in chips:
                raise OSError(errno.ENOENT, f"no such chip {path}")
            self.path = path
            self.lines: dict[int, Line] = {}
            self.closed = False
            mod.chips_opened.append(self)

        def get_line(self, offset):
            mod.calls.append(("get_line", self.path, offset))
            return self.lines.setdefault(offset, Line(self, offset))

        def find_line(self, name):
            mod.calls.append(("find_line", self.path, name))
            for off, n in chips[self.path].items():
                if n == name:
                    return self.get_line(off)
            return None

        def num_lines(self):
            return max(chips[self.path]) + 1 if chips[self.path] else 0

        def name(self):
            return self.path.rsplit("/", 1)[-1]

        def label(self):
            return "fake-label"

        def close(self):
            self.closed = True

    mod.Chip = Chip
    return mod


def make_gpiod_unknown() -> types.ModuleType:
    """A gpiod module with neither API shape (e.g. a broken install)."""
    return types.ModuleType("gpiod")


# ---------------------------------------------------------------------------
# RPi.GPIO
# ---------------------------------------------------------------------------


def make_rpi_gpio() -> tuple[types.ModuleType, types.ModuleType]:
    """Returns ``(RPi, RPi.GPIO)`` modules; inject both into sys.modules."""
    pkg = types.ModuleType("RPi")
    pkg.__path__ = []  # mark as package
    gpio = types.ModuleType("RPi.GPIO")
    gpio.BCM, gpio.BOARD = 11, 10
    gpio.OUT, gpio.IN = 0, 1
    gpio.HIGH, gpio.LOW = 1, 0
    gpio.PUD_OFF, gpio.PUD_DOWN, gpio.PUD_UP = 20, 21, 22
    gpio.calls: list[tuple] = []
    gpio.levels: dict[int, int] = {}

    def setmode(mode):
        gpio.calls.append(("setmode", mode))

    def setwarnings(flag):
        gpio.calls.append(("setwarnings", flag))

    def setup(pin, direction, initial=None, pull_up_down=None):
        gpio.calls.append(("setup", pin, direction, initial, pull_up_down))
        if initial is not None:
            gpio.levels[pin] = initial
        elif pull_up_down == gpio.PUD_UP:
            gpio.levels[pin] = gpio.HIGH
        else:
            gpio.levels.setdefault(pin, gpio.LOW)

    def output(pin, level):
        gpio.calls.append(("output", pin, level))
        gpio.levels[pin] = level

    def input(pin):  # noqa: A001 - mirrors the real API name
        gpio.calls.append(("input", pin))
        return gpio.levels.get(pin, gpio.LOW)

    def cleanup(pin=None):
        gpio.calls.append(("cleanup", pin))

    gpio.setmode, gpio.setwarnings, gpio.setup = setmode, setwarnings, setup
    gpio.output, gpio.input, gpio.cleanup = output, input, cleanup
    pkg.GPIO = gpio
    return pkg, gpio


# ---------------------------------------------------------------------------
# smbus2
# ---------------------------------------------------------------------------


def make_smbus2(devices: dict[int, "FakeI2CDevice"]) -> types.ModuleType:
    mod = types.ModuleType("smbus2")
    mod.calls: list[tuple] = []

    class i2c_msg:  # noqa: N801 - mirrors smbus2
        def __init__(self, addr, data=None, length=0, read=False):
            self.addr = addr
            self.data = list(data or [])
            self.length = length
            self.read_flag = read
            self.buf = b""

        @classmethod
        def write(cls, addr, data):
            return cls(addr, data=data)

        @classmethod
        def read(cls, addr, length):
            return cls(addr, length=length, read=True)

        def __bytes__(self):
            return bytes(self.buf)

        def __iter__(self):
            return iter(self.buf)

    class SMBus:
        def __init__(self, bus):
            self.bus = bus
            self.closed = False
            mod.calls.append(("open", bus))

        def i2c_rdwr(self, *msgs):
            for m in msgs:
                dev = devices.get(m.addr)
                if dev is None:
                    raise OSError(errno.EREMOTEIO, "Remote I/O error")
                if m.read_flag:
                    m.buf = dev.read(m.length)
                    mod.calls.append(("read", m.addr, m.length))
                else:
                    dev.write(bytes(m.data))
                    mod.calls.append(("write", m.addr, bytes(m.data)))

        def write_byte_data(self, addr, reg, value):
            self.i2c_rdwr(i2c_msg.write(addr, [reg, value]))

        def read_byte_data(self, addr, reg):
            w = i2c_msg.write(addr, [reg])
            r = i2c_msg.read(addr, 1)
            self.i2c_rdwr(w, r)
            return bytes(r)[0]

        def read_i2c_block_data(self, addr, reg, length):
            w = i2c_msg.write(addr, [reg])
            r = i2c_msg.read(addr, length)
            self.i2c_rdwr(w, r)
            return list(bytes(r))

        def close(self):
            self.closed = True
            mod.calls.append(("close", self.bus))

    mod.SMBus = SMBus
    mod.i2c_msg = i2c_msg
    return mod


# ---------------------------------------------------------------------------
# I2C devices + bus (drive the drivers without smbus2)
# ---------------------------------------------------------------------------


class FakeI2CDevice:
    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, length: int) -> bytes:
        raise NotImplementedError


class FakeBus(I2CBus):
    """Implements the two primitives; the base class derives the rest."""

    def __init__(self, devices: dict[int, FakeI2CDevice] | None = None) -> None:
        self.devices = dict(devices or {})
        self.log: list[tuple] = []
        self.closed = False

    def write_bytes(self, addr: int, data: bytes) -> None:
        dev = self.devices.get(addr)
        self.log.append(("w", addr, bytes(data)))
        if dev is None:
            raise OSError(errno.EREMOTEIO, f"no device at 0x{addr:02X}")
        dev.write(bytes(data))

    def read_bytes(self, addr: int, length: int) -> bytes:
        dev = self.devices.get(addr)
        self.log.append(("r", addr, length))
        if dev is None:
            raise OSError(errno.EREMOTEIO, f"no device at 0x{addr:02X}")
        return dev.read(length)

    def close(self) -> None:
        self.closed = True


class FakeSHT3x(FakeI2CDevice):
    """Answers 0x2400 with raw_t/raw_h (+CRC) and 0xF32D with a status word."""

    def __init__(self, raw_t: int = 0x6666, raw_h: int = 0x8000, corrupt_crc: bool = False) -> None:
        self.raw_t, self.raw_h, self.corrupt_crc = raw_t, raw_h, corrupt_crc
        self.pending = b""
        self.commands: list[bytes] = []

    @staticmethod
    def _word(value: int) -> bytes:
        data = bytes([(value >> 8) & 0xFF, value & 0xFF])
        return data + bytes([crc8(data)])

    def write(self, data: bytes) -> None:
        self.commands.append(data)
        if data == b"\x24\x00":
            frame = self._word(self.raw_t) + self._word(self.raw_h)
            if self.corrupt_crc:
                frame = frame[:5] + bytes([frame[5] ^ 0xFF])
            self.pending = frame
        elif data == b"\xf3\x2d":
            self.pending = self._word(0x8010)
        else:
            self.pending = b""

    def read(self, length: int) -> bytes:
        out, self.pending = self.pending[:length], self.pending[length:]
        return out


class FakeAHT20(FakeI2CDevice):
    def __init__(self, raw_t: int = 0x70000, raw_h: int = 0x80000, calibrated: bool = True, with_crc: bool = True) -> None:
        self.raw_t, self.raw_h = raw_t, raw_h
        self.calibrated = calibrated
        self.with_crc = with_crc
        self.commands: list[bytes] = []
        self.measured = False

    def status(self) -> int:
        return 0x18 if self.calibrated else 0x10

    def write(self, data: bytes) -> None:
        self.commands.append(data)
        if data == b"\xbe\x08\x00":
            self.calibrated = True
        elif data == b"\xac\x33\x00":
            self.measured = True

    def read(self, length: int) -> bytes:
        if not self.measured:
            return bytes([self.status()])[:length]
        self.measured = False
        h, t = self.raw_h & 0xFFFFF, self.raw_t & 0xFFFFF
        frame = bytes([self.status(), (h >> 12) & 0xFF, (h >> 4) & 0xFF, ((h & 0xF) << 4) | ((t >> 16) & 0xF), (t >> 8) & 0xFF, t & 0xFF])
        if self.with_crc:
            frame += bytes([crc8(frame)])
        return frame[:length]


BMP280_EXAMPLE_CAL = {
    "T1": 27504, "T2": 26435, "T3": -1000,
    "P1": 36477, "P2": -10685, "P3": 3024, "P4": 2855, "P5": 140, "P6": -7, "P7": 15500, "P8": -14600, "P9": 6000,
}
BME280_HUM_CAL = {"H1": 75, "H2": 355, "H3": 0, "H4": 333, "H5": 0, "H6": 30}
BMP280_EXAMPLE_ADC_T = 519888
BMP280_EXAMPLE_ADC_P = 415148
BME280_EXAMPLE_ADC_H = 30000


class FakeBME280(FakeI2CDevice):
    """Register-file model of a BME280 (chip id 0x60) or BMP280 (0x58)."""

    def __init__(self, chip_id: int = 0x60, adc_t: int = BMP280_EXAMPLE_ADC_T, adc_p: int = BMP280_EXAMPLE_ADC_P, adc_h: int = BME280_EXAMPLE_ADC_H) -> None:
        self.regs: dict[int, int] = {}
        self.regs[0xD0] = chip_id
        c = dict(BMP280_EXAMPLE_CAL)
        cal = struct.pack("<HhhHhhhhhhhh", c["T1"], c["T2"], c["T3"], c["P1"], c["P2"], c["P3"], c["P4"], c["P5"], c["P6"], c["P7"], c["P8"], c["P9"])
        for i, b in enumerate(cal):
            self.regs[0x88 + i] = b
        self.regs[0xA0] = 0
        h = BME280_HUM_CAL
        self.regs[0xA1] = h["H1"]
        h2 = struct.pack("<h", h["H2"])
        self.regs[0xE1], self.regs[0xE2] = h2[0], h2[1]
        self.regs[0xE3] = h["H3"]
        h4, h5 = h["H4"] & 0xFFF, h["H5"] & 0xFFF
        self.regs[0xE4] = (h4 >> 4) & 0xFF
        self.regs[0xE5] = (h4 & 0x0F) | ((h5 & 0x0F) << 4)
        self.regs[0xE6] = (h5 >> 4) & 0xFF
        self.regs[0xE7] = h["H6"] & 0xFF
        self.regs[0xF3] = 0x00  # status: idle
        self.regs[0xF7] = (adc_p >> 12) & 0xFF
        self.regs[0xF8] = (adc_p >> 4) & 0xFF
        self.regs[0xF9] = (adc_p & 0xF) << 4
        self.regs[0xFA] = (adc_t >> 12) & 0xFF
        self.regs[0xFB] = (adc_t >> 4) & 0xFF
        self.regs[0xFC] = (adc_t & 0xF) << 4
        self.regs[0xFD] = (adc_h >> 8) & 0xFF
        self.regs[0xFE] = adc_h & 0xFF
        self.pointer = 0
        self.writes: list[tuple[int, int]] = []

    def write(self, data: bytes) -> None:
        if len(data) == 1:
            self.pointer = data[0]
        elif len(data) == 2:
            self.pointer = data[0]
            self.regs[data[0]] = data[1]
            self.writes.append((data[0], data[1]))

    def read(self, length: int) -> bytes:
        out = bytes(self.regs.get(self.pointer + i, 0) for i in range(length))
        self.pointer += length
        return out


class FakeHTU21D(FakeI2CDevice):
    def __init__(self, raw_t: int = 0x7AE1, raw_h: int = 0x8002, corrupt: bool = False) -> None:
        self.raw_t, self.raw_h, self.corrupt = raw_t, raw_h, corrupt
        self.pending = b""
        self.commands: list[bytes] = []

    @staticmethod
    def _frame(value: int) -> bytes:
        data = bytes([(value >> 8) & 0xFF, value & 0xFF])
        return data + bytes([crc8(data, init=0x00)])

    def write(self, data: bytes) -> None:
        self.commands.append(data)
        if data == b"\xf3":
            self.pending = self._frame(self.raw_t)
        elif data == b"\xf5":
            self.pending = self._frame(self.raw_h)
        elif data == b"\xe7":
            self.pending = b"\x02"
        else:
            self.pending = b""
        if self.corrupt and self.pending:
            self.pending = self.pending[:-1] + bytes([self.pending[-1] ^ 0x55])

    def read(self, length: int) -> bytes:
        out, self.pending = self.pending[:length], self.pending[length:]
        return out


class FakeINA219(FakeI2CDevice):
    """Something *else* at 0x40 that ACKs everything and returns register data."""

    def write(self, data: bytes) -> None:
        pass

    def read(self, length: int) -> bytes:
        return bytes([0x39, 0x9F, 0x00][:length])


# ---------------------------------------------------------------------------
# adafruit_dht + board
# ---------------------------------------------------------------------------


def make_adafruit(temperature: float | None = 37.6, humidity: float | None = 54.0, error: Exception | None = None) -> tuple[types.ModuleType, types.ModuleType]:
    adafruit = types.ModuleType("adafruit_dht")
    board = types.ModuleType("board")
    for n in range(28):
        setattr(board, f"D{n}", f"D{n}")
    adafruit.instances: list[Any] = []

    class DHT22:
        def __init__(self, pin, use_pulseio=True):
            self.pin = pin
            self.use_pulseio = use_pulseio
            self.exited = False
            adafruit.instances.append(self)

        @property
        def temperature(self):
            if error:
                raise error
            return temperature

        @property
        def humidity(self):
            if error:
                raise error
            return humidity

        def exit(self):
            self.exited = True

    class DHT11(DHT22):
        pass

    adafruit.DHT22, adafruit.DHT11 = DHT22, DHT11
    return adafruit, board
