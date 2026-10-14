"""FeedbackService: LED patterns + buzzer chirps per event, never fighting the
alert engine's latched alarm, never blocking the caller."""

import threading
import time

from app.services.feedback_service import BUZZER_PATTERNS, EVENTS, LED_PATTERNS, FeedbackService


class _Led:
    def __init__(self):
        self.patterns = []
        self.closed = False

    def set_pattern(self, name):
        self.patterns.append(name)

    def close(self):
        self.closed = True


class _Gpio:
    def __init__(self, alarm=False):
        self.alarm = alarm
        self.pulses = []
        self.done = threading.Event()

    def get_state(self):
        return {"alarm": self.alarm}

    def pulse_alarm(self, seconds):
        if self.alarm:
            return {"ok": True, "pulsed": False, "alarm": True}
        self.pulses.append(round(seconds, 3))
        self.done.set()
        return {"ok": True, "pulsed": True, "alarm": False}


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_pattern_tables_cover_every_event():
    expected = {"boot", "setup_ready", "connecting", "connected", "failed", "button_ack",
                "factory_reset_armed", "factory_reset", "alarm"}
    assert set(EVENTS) == expected and set(LED_PATTERNS) == expected
    assert LED_PATTERNS["setup_ready"] == "blink_double" and LED_PATTERNS["connected"] == "heartbeat"
    assert LED_PATTERNS["failed"] == "sos" and LED_PATTERNS["alarm"] is None and LED_PATTERNS["button_ack"] is None
    assert len(BUZZER_PATTERNS["setup_ready"]) == 2 and len(BUZZER_PATTERNS["factory_reset_armed"]) == 3
    assert BUZZER_PATTERNS["connected"][-1][0] > BUZZER_PATTERNS["connected"][0][0]   # rising: ends long
    assert BUZZER_PATTERNS["alarm"] == [] and BUZZER_PATTERNS["connecting"] == []


def test_event_sets_led_pattern_and_chirps():
    led, gpio = _Led(), _Gpio()
    fb = FeedbackService(gpio, led, sound=True, sleep=lambda s: None)
    fb.event("setup_ready")
    assert led.patterns == ["blink_double"]
    assert _wait(lambda: len(gpio.pulses) == 2)
    assert gpio.pulses == [0.08, 0.08]
    assert fb.last_event == "setup_ready"
    fb.close()
    assert led.closed is True


def test_event_returns_immediately_even_with_slow_sleep():
    led, gpio = _Led(), _Gpio()
    fb = FeedbackService(gpio, led, sound=True, sleep=lambda s: time.sleep(0.2))
    t0 = time.monotonic()
    fb.event("connected")
    assert time.monotonic() - t0 < 0.1
    assert _wait(lambda: len(gpio.pulses) >= 1)
    fb.close()


def test_sound_off_skips_buzzer_but_keeps_led():
    led, gpio = _Led(), _Gpio()
    fb = FeedbackService(gpio, led, sound=False)
    fb.event("boot")
    time.sleep(0.05)
    assert gpio.pulses == [] and led.patterns == ["blink_fast"]
    assert fb._thread is None
    fb.close()


def test_latched_alarm_is_never_interrupted():
    led, gpio = _Led(), _Gpio(alarm=True)
    fb = FeedbackService(gpio, led, sound=True, sleep=lambda s: None)
    fb.event("connected")
    time.sleep(0.1)
    assert gpio.pulses == []
    fb.close()


def test_alarm_event_leaves_led_and_buzzer_alone():
    led, gpio = _Led(), _Gpio()
    fb = FeedbackService(gpio, led, sound=True)
    fb.event("alarm")
    fb.event("button_ack")
    time.sleep(0.05)
    assert led.patterns == []                       # neither changes the LED
    assert _wait(lambda: gpio.pulses == [0.25])
    fb.close()


def test_missing_hardware_is_a_noop():
    fb = FeedbackService(None, None, sound=True)
    fb.event("boot")
    fb.event("nonsense")
    fb.close()
    assert fb.last_event == "boot"


def test_new_pattern_replaces_pending_one():
    led, gpio = _Led(), _Gpio()
    gate = threading.Event()
    fb = FeedbackService(gpio, led, sound=True, sleep=lambda s: gate.wait(1.0))
    fb.event("boot")                                # 1 beep, then blocks in sleep
    assert _wait(lambda: len(gpio.pulses) == 1)
    fb.event("setup_ready")                         # queued
    fb.event("failed")                              # replaces setup_ready
    gate.set()
    assert _wait(lambda: len(gpio.pulses) == 2)
    time.sleep(0.05)
    assert gpio.pulses == [0.08, 0.7]               # boot + failed; setup_ready dropped
    fb.close()


def test_close_is_idempotent_and_stops_thread():
    led, gpio = _Led(), _Gpio()
    fb = FeedbackService(gpio, led, sound=True, sleep=lambda s: None)
    fb.event("boot")
    fb.close()
    fb.close()
    fb.event("failed")                              # ignored after close
    time.sleep(0.02)
    assert led.patterns == ["blink_fast"]
    assert fb._thread is None or not fb._thread.is_alive()


def test_with_real_gpio_service_mock_backend():
    from app.hal.led import StatusLed
    from tests.test_gpio_service import _svc as make_gpio

    gpio = make_gpio()
    gpio.setup()
    led = StatusLed.create("gpio:5", gpio_backend=gpio._backend)
    fb = FeedbackService(gpio, led, sound=True, sleep=lambda s: None)
    fb.event("boot")
    assert _wait(lambda: fb._thread is not None and fb.last_event == "boot")
    assert led.pattern == "blink_fast"
    gpio.set_alarm(True)                            # alert engine latches the buzzer
    fb.event("connected")
    time.sleep(0.05)
    assert gpio.get_state()["alarm"] is True        # still latched: feedback never cleared it
    fb.close()
    assert led._closed is True
