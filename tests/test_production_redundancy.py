"""Production redundancies: DB snapshots+restore, safety clamps, app watchdog,
and log retention.

The redundancy ladder these complete:
  Restart=always (exits) → sd_notify WatchdogSec (hangs) → hardware watchdog
  (kernel) — and on the data side: quick_check quarantine → snapshot restore
  (keep the farmer's account/settings/history) → fresh DB (last resort).
"""

import os
import socket
import sqlite3
import tempfile
import time

from app.database import ensure_sqlite_healthy, snapshot_sqlite


def _make_db(path, marker="keepme"):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    conn.commit()
    conn.close()


# ── Snapshots ────────────────────────────────────────────────────────────────

def test_snapshot_written_once_per_day_and_rotated():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "incubator.db")
    _make_db(path)
    url = f"sqlite:///{path}"

    first = snapshot_sqlite(url)
    assert first and os.path.exists(first)
    assert snapshot_sqlite(url) is None            # same day → no duplicate

    # Plant stale snapshots, then regenerate today's: rotation keeps newest 3.
    bdir = os.path.dirname(first)
    for stamp in ("20200101", "20200102", "20200103"):
        open(os.path.join(bdir, f"incubator-{stamp}.db"), "wb").close()
    os.remove(first)                                # drop today's guard file
    assert snapshot_sqlite(url, keep=3) is not None
    snaps = sorted(f for f in os.listdir(bdir) if f.endswith(".db"))
    assert len(snaps) == 3                          # rotated down to keep=3
    assert snaps[-1].startswith("incubator-" + time.strftime("%Y%m%d"))


def test_corruption_restores_newest_snapshot():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "incubator.db")
    _make_db(path, marker="farmer-data")
    url = f"sqlite:///{path}"
    assert snapshot_sqlite(url) is not None

    # Corrupt the live database (power cut mid-write).
    with open(path, "wb") as fh:
        fh.write(b"not a database at all" * 200)

    assert ensure_sqlite_healthy(url) is True       # restored, not factory-reset
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT x FROM t").fetchone()
    conn.close()
    assert row[0] == "farmer-data"                  # the farmer's data survived
    assert any(".corrupt-" in f for f in os.listdir(d))  # forensics kept


def test_corruption_without_snapshot_still_starts_fresh():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "incubator.db")
    with open(path, "wb") as fh:
        fh.write(b"garbage" * 100)
    assert ensure_sqlite_healthy(f"sqlite:///{path}") is False
    assert not os.path.exists(path)                 # quarantined; fresh start


def test_corrupt_snapshot_is_skipped_during_restore():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "incubator.db")
    _make_db(path, marker="good")
    url = f"sqlite:///{path}"
    good = snapshot_sqlite(url)
    # A NEWER but corrupt snapshot must be skipped in favor of the good one.
    bdir = os.path.dirname(good)
    with open(os.path.join(bdir, "incubator-29990101.db"), "wb") as fh:
        fh.write(b"corrupt snapshot" * 50)
    with open(path, "wb") as fh:
        fh.write(b"corrupt live db" * 100)
    assert ensure_sqlite_healthy(url) is True
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT x FROM t").fetchone()[0] == "good"
    conn.close()


# ── Safety clamps ────────────────────────────────────────────────────────────

def test_targets_clamped_to_safe_band():
    from app.control.loop import (
        TEMP_TARGET_MAX_C, TEMP_TARGET_MIN_C, clamp_target,
    )

    assert clamp_target(100.0, TEMP_TARGET_MIN_C, TEMP_TARGET_MAX_C) == TEMP_TARGET_MAX_C
    assert clamp_target(-5.0, TEMP_TARGET_MIN_C, TEMP_TARGET_MAX_C) == TEMP_TARGET_MIN_C
    assert clamp_target(37.5, TEMP_TARGET_MIN_C, TEMP_TARGET_MAX_C) == 37.5


def test_corrupt_target_cannot_cook_the_eggs(tmp_path):
    """A DB that says target_temp_c=100 must not make the loop keep heating."""
    from tests.test_control_loop import FakeGPIO, _daemon

    gpio = FakeGPIO(temp=45.0)   # already way above any sane target
    d = _daemon(gpio, tmp_path, settings={"target_temp_c": "100"})
    d._heater_on = True
    d.loop_once(now_ts=1000.0)
    assert gpio.heater is False  # clamped to 42 → 45 °C reads as too hot → OFF


# ── App-level watchdog (sd_notify) ───────────────────────────────────────────

def test_sd_notify_noop_without_socket(monkeypatch):
    from app.sd_notify import sd_ready, sd_watchdog_ping

    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_ready() is False          # dev box: quiet no-op, never raises
    assert sd_watchdog_ping() is False


def test_sd_notify_delivers_to_systemd_socket(monkeypatch):
    from app.sd_notify import sd_ready, sd_watchdog_ping

    d = tempfile.mkdtemp()
    sock_path = os.path.join(d, "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(2.0)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)

    assert sd_ready() is True
    assert server.recv(64) == b"READY=1"
    assert sd_watchdog_ping() is True
    assert server.recv(64) == b"WATCHDOG=1"
    server.close()


def test_service_units_declare_app_watchdog():
    for unit in ("deploy/incubator.service", "deploy/incubator-control.service"):
        text = open(unit).read()
        assert "Type=notify" in text, unit
        assert "WatchdogSec=" in text, unit
        assert "Restart=always" in text, unit       # exit-coverage still there


# ── Log retention ────────────────────────────────────────────────────────────

def test_retention_sweep_prunes_old_rows(client):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app import main
    from app.database import get_db
    from app.models import ActionLog, Incubator, SensorLog

    db = next(get_db())
    try:
        inc = Incubator(name="I1")
        db.add(inc); db.commit(); db.refresh(inc)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(SensorLog(incubator_id=inc.id, temperature_c=37.0, humidity_pct=50.0,
                         captured_at=now - timedelta(days=400)))
        db.add(SensorLog(incubator_id=inc.id, temperature_c=37.5, humidity_pct=55.0,
                         captured_at=now))
        db.add(ActionLog(action="old.event", created_at=now - timedelta(days=400)))
        db.add(ActionLog(action="new.event", created_at=now))
        db.commit()

        main._sensor_poller._last_retention_sweep = 0.0
        main._sensor_poller._retention_sweep(db)

        temps = [r.temperature_c for r in db.scalars(select(SensorLog)).all()]
        actions = [r.action for r in db.scalars(select(ActionLog)).all()]
        assert temps == [37.5]                      # old sensor row pruned
        assert "old.event" not in actions and "new.event" in actions
    finally:
        db.close()


# ── Boot-safe GPIO states (relays OFF from power-on) ─────────────────────────

def test_boot_safe_gpio_directives_are_installed_and_gated():
    """Firmware must drive relay pins OFF before the kernel loads.

    During boot the pins would otherwise be inputs with pull-downs, which an
    active-LOW relay board reads as ON — i.e. the heater blasting from
    power-on until the app starts (worse in a crash loop).
    """
    init = open("init_pi.sh").read()
    assert "gpio=12,13,17,27=op,dh" in init      # active-LOW relays → OFF (high)
    assert "gpio=22,23,24,25=op,dl" in init      # turner/candle/buzzer → OFF (low)
    gate = open("build_image.sh").read()
    assert "gpio=12,13,17,27=op,dh" in gate      # zero-tolerance gate enforces it
