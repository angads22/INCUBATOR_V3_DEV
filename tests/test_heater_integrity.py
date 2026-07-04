"""Heater-circuit integrity via the optional INA219 current sensor.

Cross-checks the commanded heater state against measured current so the
appliance can tell an operator when the independent thermal cutoff has tripped
(commanded ON, no current) or the relay has welded closed (current while OFF).
The physical cutoff is what keeps the unit safe — this only makes the fault
*visible*. See docs/HARDWARE_SAFETY_ADDENDUM.md.
"""

from app.services.alert_service import AlertService, HEATER_DEBOUNCE_POLLS


class _FakeGPIO:
    """Records buzzer writes; stands in for GPIOService in the alert engine."""

    def __init__(self):
        self.alarm = False

    def set_alarm(self, on):
        self.alarm = on
        return {"ok": True, "alarm": on}


def _svc():
    svc = AlertService(gpio=_FakeGPIO())
    svc.reset()
    return svc


_ALARM_ON = {"alarm_enabled": "true"}


def _types(svc):
    return [a["type"] for a in svc.alert_state()["active"]]


# ── no sensor fitted → the check is a silent no-op ───────────────────────────

def test_absent_sensor_is_noop():
    svc = _svc()
    for _ in range(HEATER_DEBOUNCE_POLLS + 2):
        events = svc.evaluate_heater(commanded_on=True, present=False, amps=None, app_settings=_ALARM_ON)
    assert events == []
    assert _types(svc) == []
    assert svc._gpio.alarm is False


# ── commanded ON but no current → thermal cutoff / open circuit ──────────────

def test_cutoff_raises_after_debounce_and_sounds_alarm():
    svc = _svc()
    # First HEATER_DEBOUNCE_POLLS-1 polls debounce silently.
    for _ in range(HEATER_DEBOUNCE_POLLS - 1):
        assert svc.evaluate_heater(True, True, 0.0, _ALARM_ON) == []
    assert _types(svc) == []
    assert svc._gpio.alarm is False
    # The threshold poll raises and latches the buzzer.
    events = svc.evaluate_heater(True, True, 0.0, _ALARM_ON)
    assert any(e["type"] == "heater_cutoff" and e["kind"] == "raised" for e in events)
    assert _types(svc) == ["heater_cutoff"]
    assert svc._gpio.alarm is True


def test_cutoff_clears_when_current_returns():
    svc = _svc()
    for _ in range(HEATER_DEBOUNCE_POLLS):
        svc.evaluate_heater(True, True, 0.0, _ALARM_ON)
    assert _types(svc) == ["heater_cutoff"]
    # Current starts flowing again → fault clears, buzzer stops.
    events = svc.evaluate_heater(True, True, 3.2, _ALARM_ON)
    assert any(e["kind"] == "cleared" for e in events)
    assert _types(svc) == []
    assert svc._gpio.alarm is False


# ── current flowing while commanded OFF → welded relay / shorted SSR ─────────

def test_stuck_relay_raises_after_debounce():
    svc = _svc()
    for _ in range(HEATER_DEBOUNCE_POLLS - 1):
        assert svc.evaluate_heater(False, True, 3.0, _ALARM_ON) == []
    events = svc.evaluate_heater(False, True, 3.0, _ALARM_ON)
    assert any(e["type"] == "heater_stuck" for e in events)
    assert _types(svc) == ["heater_stuck"]
    assert svc._gpio.alarm is True


# ── healthy states never raise ───────────────────────────────────────────────

def test_healthy_on_with_current_is_quiet():
    svc = _svc()
    for _ in range(HEATER_DEBOUNCE_POLLS + 2):
        assert svc.evaluate_heater(True, True, 3.4, _ALARM_ON) == []
    assert _types(svc) == []


def test_healthy_off_without_current_is_quiet():
    svc = _svc()
    for _ in range(HEATER_DEBOUNCE_POLLS + 2):
        assert svc.evaluate_heater(False, True, 0.0, _ALARM_ON) == []
    assert _types(svc) == []


def test_intermittent_fault_never_latches():
    svc = _svc()
    # Fault every other poll — debounce counter resets, never reaching threshold.
    for _ in range(6):
        svc.evaluate_heater(True, True, 0.0, _ALARM_ON)   # faulted
        svc.evaluate_heater(True, True, 3.0, _ALARM_ON)   # healthy resets counter
    assert _types(svc) == []
    assert svc._gpio.alarm is False


def test_sensor_dropping_out_clears_prior_fault():
    svc = _svc()
    for _ in range(HEATER_DEBOUNCE_POLLS):
        svc.evaluate_heater(True, True, 0.0, _ALARM_ON)
    assert _types(svc) == ["heater_cutoff"]
    # Sensor becomes unreadable → we can no longer assert a fault; clear it.
    svc.evaluate_heater(True, False, None, _ALARM_ON)
    assert _types(svc) == []
    assert svc._gpio.alarm is False


# ── GPIO reader: mock mode + disabled sensor report "absent" ─────────────────

def test_gpio_read_absent_when_disabled():
    from app.services.gpio_service import GPIOService

    gpio = GPIOService(
        dht_pin=4, heater_pin=17, fan_pin=27, turner_pin=22, turner_dir_pin=23,
        candle_pin=24, alarm_pin=25, lock_pin=12, door_pin=13, setup_button_pin=18,
        mock=True, heater_current_enabled=False,
    )
    r = gpio.read_heater_current()
    assert r["present"] is False and r["amps"] is None


def test_ina219_shunt_decode():
    """The raw INA219 shunt-voltage register decodes to the right current.

    Register 0x01 is signed, 10 µV/LSB, transmitted big-endian; SMBus hands it
    back byte-swapped. A raw value of 3200 LSB = 32 mV across a 0.1 Ω shunt =
    0.32 A. We feed a fake SMBus that returns the little-endian word to confirm
    the swap + scaling.
    """
    from app.services.gpio_service import GPIOService

    gpio = GPIOService(
        dht_pin=4, heater_pin=17, fan_pin=27, turner_pin=22, turner_dir_pin=23,
        candle_pin=24, alarm_pin=25, lock_pin=12, door_pin=13, setup_button_pin=18,
        mock=False, heater_current_enabled=True, heater_current_shunt_ohms=0.1,
    )
    gpio._mock = False   # force real path on a hardware-free CI host

    class _FakeBus:
        def read_word_data(self, addr, reg):
            big_endian = 3200               # 32 mV → 0.32 A over 0.1 Ω
            return ((big_endian & 0xFF) << 8) | (big_endian >> 8)  # SMBus swaps

    gpio._ina_bus = _FakeBus()
    r = gpio.read_heater_current()
    assert r["present"] is True
    assert abs(r["amps"] - 0.32) < 1e-6
