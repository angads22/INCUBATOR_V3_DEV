"""``python -m app.hal`` — print what the HAL detects, as JSON. Never raises.

``python -m app.hal --safe-outputs`` instead drives every relay/actuator line
to its OFF level (used by incubator-gpio-safe.service at boot on boards other
than the Raspberry Pi, whose firmware already does this; ``--force`` overrides).

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


def _app_settings() -> Any:
    try:
        from app.config import settings as app_settings

        return app_settings
    except Exception:
        return None


def safe_outputs(settings: Any = None, *, force: bool = False) -> dict[str, Any]:
    """Drive every relay/actuator line to its OFF level, then release it.

    Run by ``incubator-gpio-safe.service`` early in boot on boards without a
    firmware-level GPIO default (the Raspberry Pi gets the same guarantee from
    the ``gpio=`` lines in config.txt). Relays are active-LOW by default, so
    their OFF level is HIGH; the turner/candle/buzzer lines idle LOW. Never
    raises: a failure is reported per line and the boot continues.
    """
    from .gpio import get_gpio_backend, parse_pin

    if settings is None:
        settings = _app_settings()
    if settings is None:
        return {"backend": None, "error": "app settings unavailable", "lines": {}}

    # On a Raspberry Pi the firmware already drove these lines to their OFF
    # levels (and pulls) before the kernel booted — the config.txt gpio= lines
    # install.sh writes. Releasing a libgpiod line on the BCM pinctrl driver
    # reverts the pin to an INPUT, so driving-then-releasing here would UNDO
    # that guarantee for the few seconds until the daemon starts. Skip.
    if not force:
        try:
            from .board import detect_board

            board = detect_board()
        except Exception:  # noqa: BLE001
            board = None
        if board is not None and getattr(board, "is_raspberry_pi", False):
            return {
                "backend": None, "ok": True, "skipped": "raspberry-pi", "lines": {},
                "reason": "firmware gpio= lines already hold the OFF levels; a released gpiod line would revert to input",
            }

    active_low = bool(getattr(settings, "gpio_relay_active_low", True))
    relay_off = bool(active_low)   # de-energised relay = HIGH when active-LOW
    plan = {
        "heater": (getattr(settings, "gpio_heater_pin", "17"), relay_off),
        "fan": (getattr(settings, "gpio_fan_pin", "27"), relay_off),
        "lock": (getattr(settings, "gpio_lock_pin", "12"), relay_off),
        "door": (getattr(settings, "gpio_door_pin", "13"), relay_off),
        "turner": (getattr(settings, "gpio_turner_pin", "22"), False),
        "turner_dir": (getattr(settings, "gpio_turner_dir_pin", "23"), False),
        "candle": (getattr(settings, "gpio_candle_pin", "24"), False),
        "alarm": (getattr(settings, "gpio_alarm_pin", "25"), False),
    }
    report: dict[str, Any] = {"backend": None, "relay_active_low": active_low, "lines": {}}
    backend_name = "mock" if getattr(settings, "gpio_mock", False) else getattr(settings, "gpio_backend", "auto")
    try:
        backend = get_gpio_backend(backend_name, getattr(settings, "gpio_chip", ""))
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    report["backend"] = backend.name
    try:
        for name, (pin, level) in plan.items():
            entry: dict[str, Any] = {"pin": str(pin), "level": "high" if level else "low"}
            try:
                line = backend.request_output(parse_pin(pin), initial=level, consumer="incubator-safe")
                try:
                    line.set(level)
                finally:
                    line.release()
                entry["ok"] = True
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
            report["lines"][name] = entry
    finally:
        _safe(backend.close)
    report["ok"] = all(e.get("ok") for e in report["lines"].values())
    return report


def build_report(settings: Any = None) -> dict[str, Any]:
    from .board import detect_board

    if settings is None:
        settings = _app_settings()
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
        if "--safe-outputs" in argv:
            report = safe_outputs(force="--force" in argv)
        else:
            report = build_report()
    except Exception as exc:  # pragma: no cover - both builders already guard each part
        report = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
