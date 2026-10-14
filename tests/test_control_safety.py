"""Control daemon hardening: settings-store outages, the heat-runaway latch
re-check, manual-command baselines, turner direction/clamping, the drain
race, stdlib settings reads, output re-assertion and turn persistence."""

import json
import os
import subprocess
import sys

import pytest

from app.control.bridge import daemon_reading, enqueue_command
from app.control.loop import (
    HEAT_RUNAWAY_SECONDS,
    RUNAWAY_CLEAR_DROP_C,
    RUNAWAY_RECHECK_SECONDS,
    TURN_STEPS,
    ControlDaemon,
    read_app_settings_sqlite,
    runaway_clear,
    sqlite_path_from_url,
)
from app.services.gpio_service import TURNER_MAX_STEPS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeGPIO:
    def __init__(self, temp=37.5, hum=55.0, ok=True):
        self.temp, self.hum, self.ok = temp, hum, ok
        self.heater = None
        self.fan = None
        self.heater_writes = 0
        self.fan_writes = 0
        self.turns = []  # (steps, direction)

    def read_temperature_humidity(self):
        return {"ok": self.ok, "temperature_c": self.temp, "humidity_pct": self.hum}

    def set_heater(self, on):
        self.heater = on
        self.heater_writes += 1
        return {"ok": True}

    def set_fan(self, on):
        self.fan = on
        self.fan_writes += 1
        return {"ok": True}

    def move_turner(self, steps, direction):
        self.turns.append((steps, direction))
        return {"ok": True, "steps": steps, "direction": direction}


def _settings(**over):
    base = {
        "target_temp_c": "37.5", "target_humidity_pct": "55", "alert_humidity_tolerance_pct": "10",
        "turn_interval_hours": "3", "turner_enabled": "true", "heater_enabled": "true",
    }
    base.update(over)
    return base


def _daemon(gpio, tmp_path, provider=None, settings=None, **kw):
    snapshot = _settings(**(settings or {}))
    return ControlDaemon(
        gpio,
        settings_provider=provider or (lambda: snapshot),
        state_path=str(tmp_path / "state.json"),
        command_path=str(tmp_path / "cmd.jsonl"),
        **kw,
    )


# ── settings store outage must not freeze the heater ─────────────────────────

def test_settings_provider_failure_falls_back_to_last_good_settings(tmp_path, caplog):
    calls = {"n": 0}
    good = _settings(target_temp_c="37.5")

    def provider():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("database is locked")
        return good

    gpio = FakeGPIO(temp=36.0)
    d = _daemon(gpio, tmp_path, provider=provider)
    d.loop_once(now_ts=1000.0)
    assert gpio.heater is True                         # cold -> heating on good settings
    gpio.temp = 45.0                                   # chamber overshoots while the DB is locked
    import logging

    with caplog.at_level(logging.ERROR):
        state = d.loop_once(now_ts=1010.0)
        d.loop_once(now_ts=1020.0)
    assert gpio.heater is False                        # thermostat still ran -> heater OFF
    assert state["online"] is True and state["settings_ok"] is False
    assert state["target_temp_c"] == 37.5              # last good target, not a default
    assert sum("settings store unreadable" in r.message for r in caplog.records) == 1   # logged once


def test_settings_provider_failure_on_first_run_uses_defaults(tmp_path):
    def provider():
        raise RuntimeError("disk image is malformed")

    gpio = FakeGPIO(temp=30.0)
    d = _daemon(gpio, tmp_path, provider=provider)
    state = d.loop_once(now_ts=1000.0)
    assert gpio.heater is True and state["target_temp_c"] == 37.5
    gpio.temp = 40.0
    d.loop_once(now_ts=1010.0)
    assert gpio.heater is False


def test_settings_recovery_is_logged_and_snapshot_refreshed(tmp_path, caplog):
    import logging

    state = {"fail": True}
    snap = _settings(target_temp_c="36.0")

    def provider():
        if state["fail"]:
            raise RuntimeError("locked")
        return snap

    d = _daemon(FakeGPIO(), tmp_path, provider=provider)
    d.loop_once(now_ts=1.0)
    state["fail"] = False
    with caplog.at_level(logging.INFO):
        out = d.loop_once(now_ts=2.0)
    assert out["settings_ok"] is True and out["target_temp_c"] == 36.0
    assert any("readable again" in r.message for r in caplog.records)


# ── heat-runaway latch: must be able to clear, must stay for a misplaced sensor ─

def _trip_runaway(gpio, d, start=1000.0):
    d.loop_once(now_ts=start)                      # cold -> heater ON
    assert gpio.heater is True
    state = None
    t = start
    for i, dt in enumerate(range(60, int(HEAT_RUNAWAY_SECONDS) + 300, 60)):
        gpio.temp = 25.0 + (0.05 if i % 2 else 0.0)   # jitter so the stuck detector is quiet
        t = start + dt
        state = d.loop_once(now_ts=t)
    assert state["sensor_fault"] == "heat_runaway" and gpio.heater is False
    return t


def test_runaway_latch_clears_when_chamber_cools_with_heater_off(tmp_path):
    gpio = FakeGPIO(temp=25.0)
    d = _daemon(gpio, tmp_path)
    now = _trip_runaway(gpio, d)
    latch_ts, latch_temp = d._runaway_latch_ts, d._runaway_latch_temp
    recheck_at = latch_ts + RUNAWAY_RECHECK_SECONDS
    # The chamber cools by more than the clear threshold while OFF: the sensor
    # IS coupled to the heater (weak heater / open door), so heating resumes.
    while now + 60 < recheck_at:
        now += 60
        gpio.temp = latch_temp - 0.2                    # cooling, but inside the window
        state = d.loop_once(now_ts=now)
        assert state["sensor_fault"] == "heat_runaway" and gpio.heater is False
    gpio.temp = latch_temp - RUNAWAY_CLEAR_DROP_C - 0.05
    state = d.loop_once(now_ts=recheck_at)
    assert state["sensor_fault"] == ""
    assert gpio.heater is True                      # resumes heating with a fresh baseline
    assert d._heat_start_ts == recheck_at and d._runaway_latched is False


def test_runaway_latch_holds_when_chamber_does_not_cool(tmp_path):
    gpio = FakeGPIO(temp=25.0)
    d = _daemon(gpio, tmp_path)
    now = _trip_runaway(gpio, d)
    latch_ts = d._runaway_latch_ts
    # Sensor not coupled to the heater: reading never changes meaningfully.
    i = 0
    for k in range(1, 4):
        recheck_at = latch_ts + k * RUNAWAY_RECHECK_SECONDS
        while now < recheck_at:
            now = min(now + 60, recheck_at)
            i += 1
            gpio.temp = 25.0 + (0.05 if i % 2 else 0.0)   # jitter: keep the stuck detector quiet
            state = d.loop_once(now_ts=now)
        assert state["sensor_fault"] == "heat_runaway" and gpio.heater is False
        assert state["online"] is False
        # Re-check window restarted.
        assert d._runaway_latch_ts == recheck_at
    written = json.loads((tmp_path / "state.json").read_text())
    assert written["sensor_fault"] == "heat_runaway" and written["heater_on"] is False


def test_runaway_clear_helper():
    assert runaway_clear(None, 25.0, 24.0, 10_000) is None
    assert runaway_clear(1000.0, 25.0, 24.0, 1000.0 + 100) is None           # window not elapsed
    assert runaway_clear(1000.0, 25.0, 24.6, 1000.0 + RUNAWAY_RECHECK_SECONDS) is True
    assert runaway_clear(1000.0, 25.0, 24.9, 1000.0 + RUNAWAY_RECHECK_SECONDS) is False
    assert runaway_clear(1000.0, 25.0, None, 1000.0 + RUNAWAY_RECHECK_SECONDS) is False


# ── manual heater command gets the anti-runaway baseline ─────────────────────

def test_manual_heater_on_command_is_covered_by_runaway_guard(tmp_path, monkeypatch):
    import app.control.loop as loop_mod

    gpio = FakeGPIO(temp=37.5)                      # in band: thermostat would hold
    d = _daemon(gpio, tmp_path)
    d.loop_once(now_ts=1000.0)
    assert gpio.heater is False
    monkeypatch.setattr(loop_mod.time, "time", lambda: 1000.0)
    (tmp_path / "cmd.jsonl").write_text(json.dumps({"action": "heater", "value": True}) + "\n")
    d.loop_once(now_ts=1000.0)
    assert d._heater_on is True and d._heat_start_ts == 1000.0 and d._heat_start_temp == 37.5
    state = None
    for i, dt in enumerate(range(60, int(HEAT_RUNAWAY_SECONDS) + 300, 60)):
        gpio.temp = 37.5 + (0.05 if i % 2 else 0.0)     # misplaced sensor: no rise
        state = d.loop_once(now_ts=1000.0 + dt)
    assert state["sensor_fault"] == "heat_runaway" and gpio.heater is False


# ── turner commands: direction + clamp + schedule ────────────────────────────

def test_turn_command_passes_direction_and_clamps(tmp_path):
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path, settings={"turner_enabled": "false"})
    enqueue_command(str(tmp_path / "cmd.jsonl"), "turn", -200)
    enqueue_command(str(tmp_path / "cmd.jsonl"), "turn", 100000)
    enqueue_command(str(tmp_path / "cmd.jsonl"), "turn", None)
    d.loop_once(now_ts=1000.0)
    assert gpio.turns == [(200, -1), (TURNER_MAX_STEPS, 1), (TURN_STEPS, 1)]


def test_one_step_nudge_does_not_reset_turn_schedule(tmp_path):
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path, settings={"turner_enabled": "false"})
    d._last_turn_ts = 500.0
    enqueue_command(str(tmp_path / "cmd.jsonl"), "turn", 1)
    d.loop_once(now_ts=1000.0)
    assert gpio.turns == [(1, 1)] and d._last_turn_ts == 500.0
    enqueue_command(str(tmp_path / "cmd.jsonl"), "turn", 200)
    d.loop_once(now_ts=1100.0)
    assert d._last_turn_ts is not None and d._last_turn_ts != 500.0


# ── command-file drain: nothing appended mid-drain is lost ───────────────────

def test_drain_keeps_commands_appended_while_processing(tmp_path):
    cmd = str(tmp_path / "cmd.jsonl")
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path, settings={"turner_enabled": "false"})
    enqueue_command(cmd, "fan", True)

    real_apply = d._apply_command

    def apply_and_write_concurrently(action, value):
        # Simulate the web app appending while the daemon is mid-drain.
        enqueue_command(cmd, "heater", True)
        real_apply(action, value)

    d._apply_command = apply_and_write_concurrently
    d._drain_commands()
    assert gpio.fan is True
    assert os.path.exists(cmd)                       # the late line survived in a fresh file
    d._apply_command = real_apply
    d._drain_commands()
    assert d._heater_on is True
    assert not os.path.exists(cmd) and not os.path.exists(cmd + ".processing")


def test_drain_replays_processing_file_left_by_a_crash(tmp_path):
    cmd = tmp_path / "cmd.jsonl"
    (tmp_path / "cmd.jsonl.processing").write_text(json.dumps({"action": "fan", "value": True}) + "\n")
    enqueue_command(str(cmd), "heater", True)
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path, settings={"turner_enabled": "false"})
    d._drain_commands()
    assert gpio.fan is True and d._heater_on is False   # leftover first; new file untouched
    d._drain_commands()
    assert d._heater_on is True


def test_drain_tolerates_bad_lines(tmp_path):
    cmd = tmp_path / "cmd.jsonl"
    cmd.write_text("not json\n" + json.dumps({"action": "fan", "value": True}) + "\n")
    gpio = FakeGPIO()
    d = _daemon(gpio, tmp_path, settings={"turner_enabled": "false"})
    d._drain_commands()
    assert gpio.fan is True and not cmd.exists()


# ── outputs re-asserted each iteration ───────────────────────────────────────

def test_heater_and_fan_are_rewritten_every_iteration(tmp_path):
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path, settings={"turner_enabled": "false"})
    for i in range(5):
        d.loop_once(now_ts=1000.0 + 10 * i)
    assert gpio.heater_writes == 5 and gpio.fan_writes == 5
    assert gpio.heater is False and gpio.fan is False


# ── last_turn_ts survives a restart ──────────────────────────────────────────

def test_last_turn_persisted_and_restored(tmp_path):
    persist = tmp_path / "persist" / "control-persist.json"
    gpio = FakeGPIO(temp=37.5)
    d = _daemon(gpio, tmp_path, persist_path=str(persist))
    d.loop_once(now_ts=5000.0)
    assert gpio.turns == [(TURN_STEPS, 1)]
    assert json.loads(persist.read_text()) == {"last_turn_ts": 5000.0}

    gpio2 = FakeGPIO(temp=37.5)
    d2 = _daemon(gpio2, tmp_path, persist_path=str(persist))   # "restart"
    assert d2._last_turn_ts == 5000.0
    d2.loop_once(now_ts=5100.0)
    assert gpio2.turns == []                          # not due yet: no surprise turn on boot
    d2.loop_once(now_ts=5000.0 + 3 * 3600)
    assert gpio2.turns == [(TURN_STEPS, 1)]


def test_persist_file_missing_or_corrupt_is_ignored(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text("{garbage")
    d = _daemon(FakeGPIO(), tmp_path, persist_path=str(bad))
    assert d._last_turn_ts is None
    d2 = _daemon(FakeGPIO(), tmp_path, persist_path=str(tmp_path / "missing.json"))
    assert d2._last_turn_ts is None


# ── settings via stdlib sqlite3 (no SQLAlchemy in the daemon) ────────────────

def test_sqlite_path_from_url():
    assert sqlite_path_from_url("sqlite:///./database/incubator.db") == "./database/incubator.db"
    assert sqlite_path_from_url("sqlite:////var/incubator/db.sqlite") == "/var/incubator/db.sqlite"
    assert sqlite_path_from_url("sqlite://") is None
    assert sqlite_path_from_url("sqlite:///:memory:") is None
    assert sqlite_path_from_url("postgresql://x/y") is None


def test_read_app_settings_with_stdlib_against_sqlalchemy_created_db(tmp_path):
    from app.database import Base, make_engine
    import app.models  # noqa: F401
    from app.settings_store import ensure_defaults, update_settings
    from sqlalchemy.orm import Session

    path = tmp_path / "inc.db"
    eng = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=eng)
    with Session(eng) as db:
        ensure_defaults(db)
        update_settings(db, {"target_temp_c": "38.1"})
    eng.dispose()

    out = read_app_settings_sqlite(str(path))
    assert out["target_temp_c"] == "38.1" and out["heater_enabled"] == "true"
    assert all(isinstance(v, str) for v in out.values())


def test_read_app_settings_missing_file_or_table_is_empty(tmp_path):
    assert read_app_settings_sqlite(str(tmp_path / "nope.db")) == {}
    import sqlite3

    sqlite3.connect(tmp_path / "empty.db").close()
    assert read_app_settings_sqlite(str(tmp_path / "empty.db")) == {}


def test_default_settings_provider_reads_the_app_db():
    from app.control.loop import _default_settings_provider
    from app.database import get_db
    from app.settings_store import ensure_defaults

    db = next(get_db())
    try:
        ensure_defaults(db)
    finally:
        db.close()
    out = _default_settings_provider()
    assert out["heater_enabled"] == "true"


def test_daemon_process_never_imports_sqlalchemy(tmp_path):
    """The control daemon runs beside the web app on a 512 MB board: it must
    read settings with sqlite3, not the ORM."""
    from app.database import Base, make_engine
    import app.models  # noqa: F401

    path = tmp_path / "inc.db"
    eng = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=eng)
    eng.dispose()
    code = (
        "import sys\n"
        "from app.control.loop import _default_settings_provider, build_daemon\n"
        "d = build_daemon()\n"
        "d.loop_once()\n"
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy imported'\n"
        "assert 'app.database' not in sys.modules and 'app.models' not in sys.modules\n"
        "print('lean')\n"
    )
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": ROOT, "GPIO_MOCK": "true", "INCUBATOR_DB_URL": f"sqlite:///{path}",
        "CONTROL_STATE_PATH": str(tmp_path / "state.json"),
        "CONTROL_COMMAND_PATH": str(tmp_path / "cmd.jsonl"),
        "CONTROL_PERSIST_PATH": str(tmp_path / "persist.json"),
    })
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "lean" in proc.stdout


def test_build_daemon_owns_climate_lines_and_persists():
    from app.config import settings
    from app.control.loop import build_daemon
    from app.services.gpio_service import DAEMON_OUTPUTS

    d = build_daemon()
    assert d._gpio.owned_outputs == DAEMON_OUTPUTS
    assert d._persist_path == settings.control_persist_path
    assert d._gpio.sensor_name == "mock"   # GPIO_MOCK=true in tests


# ── bridge: the web app sees the daemon's actuator state ─────────────────────

def test_daemon_reading_passes_heater_and_fan_state(tmp_path):
    path = str(tmp_path / "state.json")
    with open(path, "w") as fh:
        json.dump({"ts": 1000.0, "online": True, "temperature_c": 37.6, "humidity_pct": 55.0,
                   "heater_on": True, "fan_on": False, "sensor_fault": "", "last_turn_ts": 900.0}, fh)
    r = daemon_reading(path, now=1005.0)
    assert r["ok"] is True and r["heater_on"] is True and r["fan_on"] is False
    assert r["sensor_fault"] == "" and r["last_turn_ts"] == 900.0 and r["fresh"] is True
    stale = daemon_reading(path, now=5000.0)
    assert stale["ok"] is False and stale["heater_on"] is None and stale["fresh"] is False
    missing = daemon_reading(str(tmp_path / "none.json"))
    assert missing["heater_on"] is None and missing["ok"] is False


def test_daemon_reading_carries_runaway_fault(tmp_path):
    path = str(tmp_path / "state.json")
    with open(path, "w") as fh:
        json.dump({"ts": 1000.0, "online": False, "temperature_c": 25.0, "humidity_pct": 40.0,
                   "heater_on": False, "fan_on": False, "sensor_fault": "heat_runaway"}, fh)
    r = daemon_reading(path, now=1001.0)
    assert r["ok"] is False and r["sensor_fault"] == "heat_runaway" and r["heater_on"] is False
