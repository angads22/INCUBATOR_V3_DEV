"""Silence applies to the alert types active at the time; a NEW alert type
re-sounds the buzzer, and a cleared type is un-muted."""

from app.services.alert_service import RANGE_DEBOUNCE_POLLS, AlertService


class _Gpio:
    def __init__(self):
        self.alarm = False
        self.writes = []

    def set_alarm(self, on):
        self.alarm = on
        self.writes.append(on)
        return {"ok": True, "alarm": on}


SETTINGS = {"target_temp_c": "37.5", "alert_temp_tolerance_c": "1.0", "target_humidity_pct": "55",
            "alert_humidity_tolerance_pct": "10", "alarm_enabled": "true", "alert_sensor_fail_count": "3"}


def _reading(temp=37.5, hum=55.0):
    return {"ok": True, "mock": True, "temperature_c": temp, "humidity_pct": hum}


def _types(svc):
    return sorted(a["type"] for a in svc.alert_state()["active"])


def _feed(svc, reading, times=RANGE_DEBOUNCE_POLLS):
    for _ in range(times):
        svc.record_reading(reading, SETTINGS)


def test_new_alert_type_resounds_after_silence():
    gpio = _Gpio()
    svc = AlertService(gpio)
    _feed(svc, _reading(hum=30.0))                  # dry climate: humidity_low
    assert _types(svc) == ["humidity_low"] and gpio.alarm is True
    state = svc.silence()
    assert gpio.alarm is False and state["silenced"] is True
    assert state["silenced_types"] == ["humidity_low"]
    _feed(svc, _reading(hum=30.0))                  # still dry: stays quiet
    assert gpio.alarm is False
    _feed(svc, _reading(temp=40.0, hum=30.0))       # temperature emergency
    assert _types(svc) == ["humidity_low", "temp_high"]
    assert gpio.alarm is True                        # heard, despite the muted humidity alert
    state = svc.alert_state()
    assert state["silenced"] is False and state["silenced_types"] == ["humidity_low"]
    assert {a["type"]: a["silenced"] for a in state["active"]} == {"humidity_low": True, "temp_high": False}


def test_silencing_again_mutes_everything_active():
    gpio = _Gpio()
    svc = AlertService(gpio)
    _feed(svc, _reading(temp=40.0, hum=30.0))
    svc.silence()
    assert gpio.alarm is False and svc.alert_state()["silenced"] is True
    _feed(svc, _reading(temp=40.0, hum=30.0))
    assert gpio.alarm is False                       # sticky for both


def test_cleared_alert_is_unmuted_and_sounds_when_it_returns():
    gpio = _Gpio()
    svc = AlertService(gpio)
    _feed(svc, _reading(hum=30.0))
    svc.silence()
    _feed(svc, _reading(hum=55.0), times=1)         # humidity recovers -> cleared
    assert _types(svc) == [] and svc.alert_state()["silenced_types"] == []
    _feed(svc, _reading(hum=30.0))                  # comes back later
    assert gpio.alarm is True


def test_muted_alert_stays_muted_while_another_clears():
    gpio = _Gpio()
    svc = AlertService(gpio)
    _feed(svc, _reading(temp=40.0, hum=30.0))
    svc.silence()
    _feed(svc, _reading(temp=37.5, hum=30.0), times=1)   # temp_high clears, humidity_low remains muted
    assert _types(svc) == ["humidity_low"] and gpio.alarm is False
    assert svc.alert_state()["silenced"] is True


def test_silence_with_nothing_active_does_not_pre_mute():
    gpio = _Gpio()
    svc = AlertService(gpio)
    svc.silence()
    _feed(svc, _reading(temp=40.0))
    assert gpio.alarm is True


def test_alarm_disabled_setting_still_respected():
    gpio = _Gpio()
    svc = AlertService(gpio)
    quiet = dict(SETTINGS, alarm_enabled="false")
    for _ in range(RANGE_DEBOUNCE_POLLS):
        svc.record_reading(_reading(temp=40.0), quiet)
    assert _types(svc) == ["temp_high"] and gpio.alarm is False
