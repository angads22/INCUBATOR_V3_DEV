"""libgpiod v2 / v1 backends driven by fake ``gpiod`` modules injected into sys.modules."""

import sys

import pytest

from app.hal.gpio import GpioBackendUnavailable, get_gpio_backend, parse_pin
from app.hal.gpio.gpiod_v1 import GpiodV1Backend
from app.hal.gpio.gpiod_v2 import GpiodV2Backend
from tests.test_hal_fakes import make_gpiod_unknown, make_gpiod_v1, make_gpiod_v2


@pytest.fixture
def dev(tmp_path):
    """A fake /dev with a Pi-5-like layout: gpiochip0 (misc lines) + gpiochip4 (pinctrl, GPIO<n> names)."""
    d = tmp_path / "dev"
    d.mkdir()
    (d / "gpiochip0").touch()
    (d / "gpiochip4").touch()
    return d


def pi_chips(dev):
    return {
        str(dev / "gpiochip0"): {0: "ID_SDA", 1: "ID_SCL", 5: "PA12"},
        str(dev / "gpiochip4"): {n: f"GPIO{n}" for n in range(28)},
    }


# ---------------------------------------------------------------------------
# v2
# ---------------------------------------------------------------------------


def test_v2_selected_when_request_lines_exists(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = get_gpio_backend("auto", dev_dir=str(dev))
    assert isinstance(backend, GpiodV2Backend)
    assert backend.name == "gpiod" and backend.api_version == 2


def test_v2_output_call_shape_and_bcm_lookup(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    Direction, Value = fake.line.Direction, fake.line.Value
    backend = get_gpio_backend("gpiod", dev_dir=str(dev))

    line = backend.request_output(parse_pin("17"), initial=True)
    # BCM 17 -> the chip whose line is *named* GPIO17 (gpiochip4 on a Pi 5)
    req_call = [c for c in fake.calls if c[0] == "request_lines"][0]
    assert req_call[1] == str(dev / "gpiochip4")
    assert req_call[2] == "incubator"
    assert req_call[3] == {17: fake.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)}
    assert line.get() is True

    line.set(False)
    assert ("set_value", 17, Value.INACTIVE) in fake.calls
    assert line.get() is False
    line.set(True)
    assert fake.calls[-1] == ("set_value", 17, Value.ACTIVE)

    line.release()
    assert fake.requests[0].released is True
    line.release()  # idempotent
    assert fake.calls.count(("release",)) == 1


def test_v2_initial_low_and_custom_consumer(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("27"), initial=False, consumer="incubator-heater")
    call = fake.calls[-1]
    assert call[0] == "request_lines" and call[2] == "incubator-heater"
    assert call[3][27].output_value == fake.line.Value.INACTIVE


@pytest.mark.parametrize(
    "pull, bias_name",
    [("up", "PULL_UP"), ("down", "PULL_DOWN"), ("none", "DISABLED")],
)
def test_v2_input_bias(monkeypatch, dev, pull, bias_name):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    line = backend.request_input(parse_pin("GPIO18"), pull=pull)
    call = fake.calls[-1]
    assert call[0] == "request_lines"
    assert call[1] == str(dev / "gpiochip4")
    settings = call[3][18]
    assert settings.direction == fake.line.Direction.INPUT
    assert settings.bias == getattr(fake.line.Bias, bias_name)
    # Fresh fake request reports INACTIVE until something drives it.
    assert line.get() is False
    fake.requests[-1].values[18] = fake.line.Value.ACTIVE
    assert line.get() is True


def test_v2_explicit_chip_and_name_resolution(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    # chip:offset is taken literally
    backend.request_output(parse_pin("gpiochip0:17"), initial=False)
    assert fake.calls[-1][1] == str(dev / "gpiochip0")
    # a line name is searched across chips
    backend.request_output(parse_pin("PA12"), initial=False)
    assert fake.calls[-1][1] == str(dev / "gpiochip0") and 5 in fake.calls[-1][3]
    # a chip-qualified name is only searched on that chip
    with pytest.raises(LookupError):
        backend.request_output(parse_pin("gpiochip4:PA12"), initial=False)
    with pytest.raises(LookupError):
        backend.request_output(parse_pin("NOPE"), initial=False)


def test_v2_offset_without_gpio_name_falls_back_to_gpiochip0(monkeypatch, dev):
    fake = make_gpiod_v2({str(dev / "gpiochip0"): {0: "ID_SDA"}, str(dev / "gpiochip4"): {0: "X0"}})
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("3"), initial=False)
    assert fake.calls[-1][1] == str(dev / "gpiochip0") and 3 in fake.calls[-1][3]


def test_v2_chip_hint_wins_for_bare_offsets(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = get_gpio_backend("gpiod", chip="gpiochip0", dev_dir=str(dev))
    backend.request_output(parse_pin("17"), initial=False)
    assert fake.calls[-1][1] == str(dev / "gpiochip0")
    backend2 = get_gpio_backend("gpiod", chip=str(dev / "gpiochip4"), dev_dir=str(dev))
    backend2.request_output(parse_pin("2"), initial=False)
    assert fake.calls[-1][1] == str(dev / "gpiochip4")


def test_v2_name_lookup_is_cached(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("17"), initial=False)
    opens = len(fake.chip_opens)
    assert opens >= 1
    backend.request_input(parse_pin("17"))
    assert len(fake.chip_opens) == opens  # second resolution served from cache


def test_v2_chip_info_and_line_name(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev), labels={str(dev / "gpiochip4"): "pinctrl-rp1"})
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    info = backend.chips()
    assert [c["path"] for c in info] == [str(dev / "gpiochip0"), str(dev / "gpiochip4")]
    assert info[1]["label"] == "pinctrl-rp1" and info[1]["num_lines"] == 28
    assert backend.line_name(str(dev / "gpiochip4"), 17) == "GPIO17"
    assert backend.line_name(str(dev / "gpiochip9"), 0) is None


def test_v2_close_releases_everything(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    backend = GpiodV2Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("17"), initial=False)
    backend.request_input(parse_pin("18"))
    backend.close()
    assert all(r.released for r in fake.requests) and len(fake.requests) == 2
    backend.close()  # idempotent


# ---------------------------------------------------------------------------
# v1
# ---------------------------------------------------------------------------


def test_v1_selected_when_only_chip_api(monkeypatch, dev):
    fake = make_gpiod_v1(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.delitem(sys.modules, "gpiod.line", raising=False)
    backend = get_gpio_backend("auto", dev_dir=str(dev))
    assert isinstance(backend, GpiodV1Backend)
    assert backend.name == "gpiod" and backend.api_version == 1


def test_v1_output_call_shape(monkeypatch, dev):
    fake = make_gpiod_v1(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    backend = get_gpio_backend("gpiod", dev_dir=str(dev))
    chip4 = str(dev / "gpiochip4")

    line = backend.request_output(parse_pin("17"), initial=True)
    assert ("find_line", chip4, "GPIO17") in fake.calls or ("find_line", str(dev / "gpiochip0"), "GPIO17") in fake.calls
    assert ("get_line", chip4, 17) in fake.calls
    assert ("request", chip4, 17, "incubator", fake.LINE_REQ_DIR_OUT, 0, [1]) in fake.calls
    assert line.get() is True
    line.set(False)
    assert fake.calls[-1] == ("set_value", 17, 0)
    assert line.get() is False
    line.set(True)
    assert fake.calls[-1] == ("set_value", 17, 1)
    line.release()
    assert ("release", 17) in fake.calls
    line.release()
    assert fake.calls.count(("release", 17)) == 1


def test_v1_initial_low(monkeypatch, dev):
    fake = make_gpiod_v1(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    backend = GpiodV1Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("gpiochip0:5"), initial=False, consumer="heater")
    assert fake.calls[-1] == ("request", str(dev / "gpiochip0"), 5, "heater", fake.LINE_REQ_DIR_OUT, 0, [0])


@pytest.mark.parametrize(
    "pull, flag_attr",
    [("up", "LINE_REQ_FLAG_BIAS_PULL_UP"), ("down", "LINE_REQ_FLAG_BIAS_PULL_DOWN"), ("none", "LINE_REQ_FLAG_BIAS_DISABLE")],
)
def test_v1_input_flags(monkeypatch, dev, pull, flag_attr):
    fake = make_gpiod_v1(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    backend = GpiodV1Backend(dev_dir=str(dev))
    line = backend.request_input(parse_pin("18"), pull=pull)
    chip4 = str(dev / "gpiochip4")
    assert fake.calls[-1] == ("request", chip4, 18, "incubator", fake.LINE_REQ_DIR_IN, getattr(fake, flag_attr), None)
    assert line.get() is False
    fake.chips_opened[-1].lines[18].value = 1
    assert line.get() is True


def test_v1_without_bias_flags_uses_zero(monkeypatch, dev):
    fake = make_gpiod_v1(pi_chips(dev), bias_flags=False)
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    backend = GpiodV1Backend(dev_dir=str(dev))
    backend.request_input(parse_pin("18"), pull="up")
    assert fake.calls[-1][5] == 0


def test_v1_name_lookup_and_chip_info(monkeypatch, dev):
    fake = make_gpiod_v1(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    backend = GpiodV1Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("PA12"), initial=False)
    assert fake.calls[-1][1:3] == (str(dev / "gpiochip0"), 5)
    with pytest.raises(LookupError):
        backend.request_output(parse_pin("NOPE"), initial=False)
    info = backend.chips()
    assert info[1]["num_lines"] == 28 and info[1]["name"] == "gpiochip4"
    assert backend.line_name(str(dev / "gpiochip4"), 2) == "GPIO2"


def test_v1_close_releases_lines_and_chips(monkeypatch, dev):
    fake = make_gpiod_v1(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    backend = GpiodV1Backend(dev_dir=str(dev))
    backend.request_output(parse_pin("17"), initial=False)
    backend.request_input(parse_pin("gpiochip0:5"))
    backend.close()
    assert ("release", 17) in fake.calls and ("release", 5) in fake.calls
    assert all(c.closed for c in fake.chips_opened)


# ---------------------------------------------------------------------------
# availability / errors
# ---------------------------------------------------------------------------


def test_explicit_gpiod_without_chip_devices_raises(monkeypatch, tmp_path):
    fake = make_gpiod_v2({})
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    empty = tmp_path / "dev"
    empty.mkdir()
    with pytest.raises(GpioBackendUnavailable, match="gpiochip"):
        get_gpio_backend("gpiod", dev_dir=str(empty))


def test_explicit_gpiod_without_module_raises(monkeypatch, dev):
    monkeypatch.setitem(sys.modules, "gpiod", None)  # makes `import gpiod` fail
    with pytest.raises(RuntimeError, match="gpiod"):
        get_gpio_backend("gpiod", dev_dir=str(dev))


def test_gpiod_module_with_unknown_api_is_unavailable(monkeypatch, dev, tmp_path):
    monkeypatch.setitem(sys.modules, "gpiod", make_gpiod_unknown())
    monkeypatch.setitem(sys.modules, "RPi", None)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", None)
    with pytest.raises(GpioBackendUnavailable):
        get_gpio_backend("gpiod", dev_dir=str(dev))
    # auto skips it and lands on mock (no RPi, no writable sysfs)
    backend = get_gpio_backend("auto", dev_dir=str(dev), sysfs_root=str(tmp_path / "nosysfs"))
    assert backend.name == "mock"


def test_explicit_v1_or_v2_aliases(monkeypatch, dev):
    fake = make_gpiod_v2(pi_chips(dev))
    monkeypatch.setitem(sys.modules, "gpiod", fake)
    monkeypatch.setitem(sys.modules, "gpiod.line", fake.line)
    assert isinstance(get_gpio_backend("gpiod-v2", dev_dir=str(dev)), GpiodV2Backend)
    with pytest.raises(GpioBackendUnavailable):
        get_gpio_backend("gpiod_v1", dev_dir=str(dev))  # v2 module cannot serve the v1 API
