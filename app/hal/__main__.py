"""``python -m app.hal`` — print what the HAL detects, as JSON. Never raises.

Useful when porting to a new board: shows the board model, which GPIO backend
would be used and the gpiochips it sees, the sensor found (with one reading),
the status LED picked and the Wi-Fi interface(s) with AP capability.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Callable


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # diagnostics must never crash
        return {"error": f"{type(exc).__name__}: {exc}"}


def _gpio_report(backend_name: str, chip: str) -> dict[str, Any]:
    from .gpio import available_backends, get_gpio_backend, list_gpio_chips

    report: dict[str, Any] = {
        "requested": backend_name,
        "available": _safe(available_backends),
        "chips": list_gpio_chips(),
    }
    try:
        backend = get_gpio_backend(backend_name, chip)
    except Exception as exc:
        report["backend"] = None
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    try:
        report["backend"] = backend.name
        report["api_version"] = getattr(backend, "api_version", None)
        report["chip_info"] = _safe(backend.chips)
    finally:
        _safe(backend.close)
    return report


def _sensor_report(settings: Any) -> dict[str, Any]:
    from .sensors import get_sensor

    sensor = get_sensor(
        getattr(settings, "sensor_backend", "auto"),
        i2c_bus=int(getattr(settings, "sensor_i2c_bus", 1)),
        i2c_addr=int(getattr(settings, "sensor_i2c_addr", 0)),
        iio_device=getattr(settings, "sensor_iio_device", ""),
        dht_pin=getattr(settings, "gpio_dht_pin", None),
    )
    if sensor is None:
        return {"requested": getattr(settings, "sensor_backend", "auto"), "sensor": None}
    try:
        return {"requested": getattr(settings, "sensor_backend", "auto"), "sensor": sensor.name, "reading": _safe(sensor.read)}
    finally:
        _safe(sensor.close)


def _led_report(spec: str) -> dict[str, Any]:
    from .led import StatusLed, list_sysfs_leds

    report: dict[str, Any] = {"requested": spec, "sysfs_leds": list_sysfs_leds()}
    led = StatusLed.create(spec)
    if led is None:
        report["led"] = None
        return report
    try:
        report["led"] = led.describe()
    finally:
        _safe(led.close)
    return report


def _wifi_report() -> dict[str, Any]:
    from .wifi import supports_ap_mode, wifi_interface, wifi_interfaces

    iface = wifi_interface()
    return {
        "interfaces": wifi_interfaces(),
        "interface": iface,
        "env_WIFI_IFACE": os.environ.get("WIFI_IFACE") or None,
        "supports_ap_mode": _safe(lambda: supports_ap_mode(iface)),
    }


def build_report(settings: Any = None) -> dict[str, Any]:
    from .board import detect_board

    if settings is None:
        try:
            from app.config import settings as app_settings

            settings = app_settings
        except Exception:
            settings = None
    gpio_backend = getattr(settings, "gpio_backend", "auto")
    gpio_chip = getattr(settings, "gpio_chip", "")
    led_spec = getattr(settings, "status_led", "auto")
    return {
        "board": _safe(lambda: detect_board().as_dict()),
        "gpio": _safe(lambda: _gpio_report(gpio_backend, gpio_chip)),
        "sensor": _safe(lambda: _sensor_report(settings)),
        "led": _safe(lambda: _led_report(led_spec)),
        "wifi": _safe(_wifi_report),
        "python": sys.version.split()[0],
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv or "--verbose" in argv
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING, stream=sys.stderr)
    try:
        report = build_report()
    except Exception as exc:  # pragma: no cover - build_report already guards each part
        report = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
