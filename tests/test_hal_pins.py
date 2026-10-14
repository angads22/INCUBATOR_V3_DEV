"""parse_pin / PinSpec and the config's string pins."""

import pytest

from app.hal.gpio import PinSpec, parse_pin
from app.hal.gpio.chips import chip_index, list_gpio_chips, normalize_chip_path


@pytest.mark.parametrize(
    "spec, chip, offset, name",
    [
        (17, None, 17, None),
        ("17", None, 17, None),
        ("  4 ", None, 4, None),
        ("0x11", None, 17, None),
        ("gpiochip0:17", "gpiochip0", 17, None),
        ("/dev/gpiochip1:5", "/dev/gpiochip1", 5, None),
        ("1:5", "gpiochip1", 5, None),
        ("GPIO17", None, None, "GPIO17"),
        ("PA12", None, None, "PA12"),
        ("gpiochip1:PA12", "gpiochip1", None, "PA12"),
        ("/dev/gpiochip4:GPIO18", "/dev/gpiochip4", None, "GPIO18"),
    ],
)
def test_parse_pin_shapes(spec, chip, offset, name):
    pin = parse_pin(spec)
    assert (pin.chip, pin.offset, pin.name) == (chip, offset, name)


def test_parse_pin_passthrough_and_str():
    pin = PinSpec(chip="gpiochip0", offset=17)
    assert parse_pin(pin) is pin
    assert str(parse_pin("17")) == "17"
    assert str(parse_pin("gpiochip0:17")) == "gpiochip0:17"
    assert str(parse_pin("GPIO17")) == "GPIO17"
    assert str(parse_pin("gpiochip1:PA12")) == "gpiochip1:PA12"
    assert parse_pin("17").key == "17"


@pytest.mark.parametrize("bad", ["", "   ", None, True, "-3", "gpiochip0:", "17 18", "GPIO 17", "/dev/gpiochip0", "a b"])
def test_parse_pin_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_pin(bad)


def test_pinspec_validation():
    with pytest.raises(ValueError):
        PinSpec()
    with pytest.raises(ValueError):
        PinSpec(offset=-1)
    assert PinSpec(name="PA1").offset is None


def test_chip_helpers(tmp_path):
    (tmp_path / "gpiochip0").touch()
    (tmp_path / "gpiochip10").touch()
    (tmp_path / "gpiochip4").touch()
    (tmp_path / "gpiochipx").touch()
    (tmp_path / "i2c-1").touch()
    assert list_gpio_chips(str(tmp_path)) == [str(tmp_path / "gpiochip0"), str(tmp_path / "gpiochip4"), str(tmp_path / "gpiochip10")]
    assert list_gpio_chips(str(tmp_path / "missing")) == []
    assert chip_index("gpiochip4") == 4
    assert chip_index("/dev/gpiochip4") == 4
    assert chip_index("4") == 4
    assert chip_index("pinctrl") is None
    assert normalize_chip_path("gpiochip4", "/dev") == "/dev/gpiochip4"
    assert normalize_chip_path("4", "/dev") == "/dev/gpiochip4"
    assert normalize_chip_path("/dev/gpiochip4", "/dev") == "/dev/gpiochip4"


def test_config_pins_are_strings(monkeypatch):
    from app.config import Settings, settings

    for attr in ("dht", "heater", "fan", "turner", "turner_dir", "candle", "alarm", "lock", "door", "setup_button"):
        value = getattr(settings, f"gpio_{attr}_pin")
        assert isinstance(value, str) and value, attr
        parse_pin(value)  # every default is a valid pin spec
    assert settings.gpio_heater_pin == "17"
    monkeypatch.setenv("GPIO_HEATER_PIN", " gpiochip1:PA12 ")
    monkeypatch.setenv("SENSOR_I2C_ADDR", "0x44")
    monkeypatch.setenv("GPIO_BACKEND", "GPIOD")
    fresh = Settings()
    assert fresh.gpio_heater_pin == "gpiochip1:PA12"
    assert fresh.sensor_i2c_addr == 0x44
    assert fresh.gpio_backend == "gpiod"
    assert fresh.sensor_backend == "auto"
    assert fresh.status_led == "auto"
    assert fresh.factory_reset_hold_seconds == 15.0
    assert fresh.sound_feedback is True
    assert fresh.setup_state_path.endswith("setup-state.json") or fresh.setup_state_path.endswith("incubator_setup_state.json")
    assert fresh.control_persist_path.endswith("control-persist.json")
