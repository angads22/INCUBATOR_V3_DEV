"""Hardware abstraction layer: run the incubator on any Linux SBC.

* :mod:`app.hal.board`   — ``detect_board()`` -> :class:`BoardInfo`
* :mod:`app.hal.gpio`    — ``get_gpio_backend()`` (gpiod v2/v1, RPi.GPIO, sysfs, mock), ``parse_pin()``
* :mod:`app.hal.sensors` — ``get_sensor()`` (IIO, SHT3x/AHT20/BME280/HTU21D over I2C, DHT, mock)
* :mod:`app.hal.led`     — :class:`StatusLed`
* :mod:`app.hal.wifi`    — ``wifi_interface()`` / ``wifi_interfaces()`` / ``supports_ap_mode()``

No hardware library is imported at module import time; ``python -m app.hal``
prints what was detected.
"""

from __future__ import annotations

from .board import BoardInfo, detect_board
from .gpio import GpioBackend, GpioBackendUnavailable, InputLine, OutputLine, PinSpec, get_gpio_backend, parse_pin
from .led import StatusLed
from .sensors import MockSensor, Sensor, get_sensor
from .wifi import supports_ap_mode, wifi_interface, wifi_interfaces

__all__ = [
    "BoardInfo",
    "GpioBackend",
    "GpioBackendUnavailable",
    "InputLine",
    "MockSensor",
    "OutputLine",
    "PinSpec",
    "Sensor",
    "StatusLed",
    "detect_board",
    "get_gpio_backend",
    "get_sensor",
    "parse_pin",
    "supports_ap_mode",
    "wifi_interface",
    "wifi_interfaces",
]
