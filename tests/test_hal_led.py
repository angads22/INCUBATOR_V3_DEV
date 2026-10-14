"""StatusLed over a fake /sys/class/leds tree and over a mock GPIO line."""

import threading
import time

import pytest

from app.hal.gpio import MockGpioBackend
from app.hal.led import PATTERNS, StatusLed, pick_sysfs_led


def make_led(root, name, trigger="none mmc0 [actpwr] heartbeat", max_brightness=255):
    d = root / name
    d.mkdir(parents=True)
    (d / "trigger").write_text(trigger + "\n")
    (d / "brightness").write_text("0\n")
    (d / "max_brightness").write_text(f"{max_brightness}\n")
    return d


def brightness(d):
    return (d / "brightness").read_text().strip()


def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_pattern_table():
    assert PATTERNS["heartbeat"] == [(True, 0.1), (False, 1.9)]
    assert PATTERNS["blink_fast"] == [(True, 0.15), (False, 0.15)]
    assert PATTERNS["blink_double"] == [(True, 0.12), (False, 0.12), (True, 0.12), (False, 1.2)]
    sos = PATTERNS["sos"]
    ons = [sec for on, sec in sos if on]
    assert ons == [0.15] * 3 + [0.45] * 3 + [0.15] * 3
    assert sos[-1] == (False, 1.0)


def test_auto_picks_preferred_led_and_restores_trigger(tmp_path):
    make_led(tmp_path, "mmc0::", trigger="[mmc0] none")
    act = make_led(tmp_path, "ACT")
    make_led(tmp_path, "PWR", trigger="[default-on] none")
    assert pick_sysfs_led(str(tmp_path)) == "ACT"

    led = StatusLed.create("auto", sysfs_root=str(tmp_path))
    assert led is not None and led.kind == "sysfs" and led.name == "ACT"
    assert (act / "trigger").read_text() == "none"  # trigger taken over
    assert led.describe()["restored_trigger"] == "actpwr"

    led.set_pattern("on")
    assert brightness(act) == "255"
    led.set_pattern("off")
    assert brightness(act) == "0"
    assert led.is_running is False  # steady states need no thread

    led.set_pattern("blink_fast")
    assert led.is_running is True and led.pattern == "blink_fast"
    seen = set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(seen) < 2:
        value = brightness(act)
        # A real sysfs attribute write is atomic; a tmp file is not, so a read
        # may catch the file truncated mid-write. Only count complete values.
        if value in ("0", "255"):
            seen.add(value)
        time.sleep(0.01)
    assert seen == {"0", "255"}

    thread = led._thread
    led.close()
    assert wait_until(lambda: not thread.is_alive())
    assert led.is_running is False
    assert (act / "trigger").read_text() == "actpwr"  # restored
    assert brightness(act) == "0"
    led.close()  # idempotent
    led.set_pattern("on")  # ignored after close
    assert brightness(act) == "0"


def test_pattern_change_is_immediate(tmp_path):
    act = make_led(tmp_path, "led0", trigger="[none] heartbeat")
    led = StatusLed.create("led0", sysfs_root=str(tmp_path))
    led.set_pattern("sos")
    assert led.is_running
    led.set_pattern("off")  # steady: written synchronously, thread parks
    assert brightness(act) == "0"
    led.set_pattern("heartbeat")
    assert wait_until(lambda: brightness(act) == "255", timeout=0.5)  # first step of heartbeat is ON
    led.set_pattern("blink_double")
    assert led.pattern == "blink_double"
    # one worker thread only, no matter how many switches
    assert sum(1 for t in threading.enumerate() if t.name == "status-led") == 1
    led.close()
    # Trigger was already "none": nothing is written to it, on open or on close.
    assert (act / "trigger").read_text().strip() == "[none] heartbeat"


def test_unknown_pattern_turns_off(tmp_path, caplog):
    act = make_led(tmp_path, "status")
    led = StatusLed.create("status", sysfs_root=str(tmp_path))
    led.set_pattern("on")
    led.set_pattern("disco")
    assert led.pattern == "off" and brightness(act) == "0"
    led.close()


def test_keyword_fallback_and_none(tmp_path):
    make_led(tmp_path, "orangepi:green:pwr", trigger="[default-on]")
    assert pick_sysfs_led(str(tmp_path)) == "orangepi:green:pwr"
    (tmp_path / "orangepi:green:pwr").rename(tmp_path / "rockchip:blue:user")
    assert pick_sysfs_led(str(tmp_path)) == "rockchip:blue:user"
    (tmp_path / "rockchip:blue:user").rename(tmp_path / "mmc0::")
    assert pick_sysfs_led(str(tmp_path)) is None
    assert StatusLed.create("auto", sysfs_root=str(tmp_path)) is None
    assert StatusLed.create("none", sysfs_root=str(tmp_path)) is None
    assert StatusLed.create("missing-led", sysfs_root=str(tmp_path)) is None
    assert StatusLed.create("auto", sysfs_root=str(tmp_path / "no-such-dir")) is None


def test_io_errors_are_swallowed(tmp_path):
    act = make_led(tmp_path, "ACT")
    led = StatusLed.create("ACT", sysfs_root=str(tmp_path))
    (act / "brightness").unlink()
    (act / "trigger").unlink()
    led.set_pattern("on")
    led.set_pattern("heartbeat")
    time.sleep(0.05)
    led.close()  # restore fails silently


def test_gpio_led_via_mock_backend():
    backend = MockGpioBackend()
    led = StatusLed.create("gpio:25", gpio_backend=backend)
    assert led is not None and led.kind == "gpio"
    line = backend.lines["25"]
    assert line.direction == "out" and line.value is False
    led.set_pattern("on")
    assert line.value is True
    led.set_pattern("off")
    assert line.value is False
    led.set_pattern("blink_fast")
    assert wait_until(lambda: line.value is True, timeout=0.5)
    led.close()
    assert line.released is True and line.value is False

    inverted = StatusLed.create("gpio:!GPIO25", gpio_backend=backend)
    inv_line = backend.lines["GPIO25"]
    assert inv_line.value is True  # idle = HIGH for active-low
    inverted.set_pattern("on")
    assert inv_line.value is False
    inverted.close()


def test_gpio_led_bad_pin_returns_none():
    assert StatusLed.create("gpio:not a pin", gpio_backend=MockGpioBackend()) is None

    class Failing(MockGpioBackend):
        def request_output(self, pin, initial, consumer="incubator"):
            raise RuntimeError("busy")

    assert StatusLed.create("gpio:25", gpio_backend=Failing()) is None


def test_gpio_led_without_backend_uses_auto(monkeypatch):
    import app.hal.led as led_mod

    created = []

    def fake_get_backend(name="auto"):
        b = MockGpioBackend()
        created.append(b)
        return b

    import app.hal.gpio as gpio_pkg

    monkeypatch.setattr(gpio_pkg, "get_gpio_backend", fake_get_backend)
    led = led_mod.StatusLed.create("gpio:12")
    assert led is not None and created and "12" in created[0].lines
    led.close()


@pytest.mark.parametrize("spec", ["none", "NONE", "off", "0", "false"])
def test_disabled_specs(spec):
    assert StatusLed.create(spec) is None
