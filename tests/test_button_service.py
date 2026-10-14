"""SetupButtonService: hold -> setup mode, long hold -> factory reset on release.
Driven with a fake clock and either an injected reader or the mock file."""

import os

from app.services.button_service import SetupButtonService


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _svc(clock, reader, **kw):
    fired, progress = [], []
    svc = SetupButtonService(
        4.0, fired.append, read_pressed=reader, mock_file="/nonexistent/btn",
        factory_reset_hold_seconds=kw.pop("reset", 15.0), on_progress=progress.append, clock=clock, **kw,
    )
    return svc, fired, progress


def test_short_press_does_nothing():
    clock = _Clock()
    svc, fired, progress = _svc(clock, lambda: True)
    for t in (0.0, 1.0, 2.0, 3.9):
        clock.t = t
        assert svc.tick() is None
    clock.t = 4.0
    svc.tick(pressed=False)
    assert fired == [] and progress == []


def test_hold_four_seconds_fires_button_hold_once():
    clock = _Clock()
    svc, fired, progress = _svc(clock, lambda: True)
    clock.t = 0.0
    svc.tick()
    clock.t = 4.0
    assert svc.tick() == "button_hold"
    clock.t = 8.0
    assert svc.tick() is None                       # not repeated while still held
    clock.t = 9.0
    assert svc.tick(pressed=False) is None          # released before 15 s: no reset
    assert fired == ["button_hold"] and progress == []


def test_fifteen_second_hold_arms_then_release_resets():
    clock = _Clock()
    svc, fired, progress = _svc(clock, lambda: True)
    clock.t = 0.0
    svc.tick()
    clock.t = 4.0
    assert svc.tick() == "button_hold"
    clock.t = 14.9
    assert svc.tick() is None and progress == []
    clock.t = 15.0
    assert svc.tick() == "factory_reset_armed"
    assert progress == ["factory_reset_armed"]
    assert fired == ["button_hold"]                 # NOT fired until release
    clock.t = 16.0
    assert svc.tick() is None                       # armed once
    clock.t = 17.0
    assert svc.tick(pressed=False) == "factory_reset"
    assert fired == ["button_hold", "factory_reset"]
    # A fresh press starts over.
    clock.t = 20.0
    svc.tick(pressed=True)
    clock.t = 21.0
    assert svc.tick(pressed=False) is None
    assert fired == ["button_hold", "factory_reset"]


def test_factory_reset_disabled_when_zero():
    clock = _Clock()
    svc, fired, progress = _svc(clock, lambda: True, reset=0)
    clock.t = 0.0
    svc.tick()
    clock.t = 60.0
    svc.tick()
    svc.tick(pressed=False)
    assert fired == ["button_hold"] and progress == []


def test_reset_threshold_must_exceed_hold_threshold():
    svc = SetupButtonService(4.0, lambda e: None, read_pressed=lambda: False, mock_file="/nope", factory_reset_hold_seconds=3.0)
    assert svc.factory_reset_hold_seconds == 0.0


def test_handler_exceptions_do_not_break_the_monitor():
    clock = _Clock()

    def bad(_e):
        raise RuntimeError("boom")

    svc = SetupButtonService(4.0, bad, read_pressed=lambda: True, mock_file="/nope", factory_reset_hold_seconds=15.0, on_progress=bad, clock=clock)
    clock.t = 0.0
    svc.tick()
    clock.t = 4.0
    assert svc.tick() == "button_hold"
    clock.t = 15.0
    assert svc.tick() == "factory_reset_armed"
    assert svc.tick(pressed=False) == "factory_reset"


def test_mock_file_takes_priority_over_reader(tmp_path):
    mock = tmp_path / "btn"
    clock = _Clock()
    fired = []
    svc = SetupButtonService(4.0, fired.append, read_pressed=lambda: False, mock_file=str(mock), clock=clock)
    assert svc._read_pressed() is False             # file absent -> reader (False)
    mock.write_text("1")
    assert svc._read_pressed() is True
    clock.t = 0.0
    svc.tick()
    clock.t = 4.5
    svc.tick()
    assert fired == ["button_hold"]
    mock.write_text("0")
    assert svc._read_pressed() is False


def test_reader_failures_read_as_released():
    def flaky():
        raise OSError("gpio gone")

    svc = SetupButtonService(4.0, lambda e: None, read_pressed=flaky, mock_file="/nope")
    assert svc._read_pressed() is False


def test_gpio_service_read_button_is_the_reader():
    from tests.test_gpio_service import _svc as make_gpio

    gpio = make_gpio()
    clock = _Clock()
    fired = []
    svc = SetupButtonService(4.0, fired.append, read_pressed=gpio.read_button, mock_file="/nope", clock=clock)
    gpio.set_mock_button(True)
    clock.t = 0.0
    svc.tick()
    clock.t = 4.0
    svc.tick()
    assert fired == ["button_hold"]


def test_thread_start_stop(tmp_path):
    svc = SetupButtonService(4.0, lambda e: None, read_pressed=lambda: False, mock_file=str(tmp_path / "none"), poll_interval=0.01)
    svc.start()
    svc.start()                                     # idempotent
    assert svc._thread is not None and svc._thread.is_alive()
    svc.stop()
    assert svc._thread is None


def test_no_hardware_library_imported():
    import app.services.button_service as mod

    src = open(mod.__file__).read()
    assert "RPi" not in src.replace("RPi.GPIO", "") or "import RPi" not in src
    assert "import RPi" not in src
