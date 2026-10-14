"""SQLite tuning for SD cards: WAL/NORMAL pragmas on every connection and
indexes on the hot time columns of existing databases."""

import sqlite3

from sqlalchemy import text

from app.database import (
    INDEXES,
    SQLITE_PRAGMAS,
    ensure_indexes,
    ensure_sqlite_healthy,
    make_engine,
    snapshot_sqlite,
)


def _url(tmp_path, name="t.db"):
    return f"sqlite:///{tmp_path / name}"


def test_pragmas_applied_on_every_connection(tmp_path):
    eng = make_engine(_url(tmp_path))
    with eng.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1      # NORMAL
        assert conn.execute(text("PRAGMA temp_store")).scalar() == 2       # MEMORY
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
    # A second (new) connection gets the per-connection pragmas again.
    eng.dispose()
    with eng.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1
    eng.dispose()


def test_pragma_table_matches_spec():
    assert dict(SQLITE_PRAGMAS) == {
        "journal_mode": "WAL", "synchronous": "NORMAL", "temp_store": "MEMORY", "busy_timeout": "5000",
    }


def test_app_engine_uses_wal():
    from app.database import engine

    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"


def test_health_check_and_snapshot_work_with_wal_sidecars(tmp_path):
    url = _url(tmp_path)
    path = str(tmp_path / "t.db")
    eng = make_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    # WAL mode leaves -wal/-shm sidecars next to the file while a connection is open.
    with eng.connect() as conn:
        conn.execute(text("INSERT INTO t VALUES (2)"))
        conn.commit()
        assert ensure_sqlite_healthy(url) is True
        snap = snapshot_sqlite(url)
    eng.dispose()
    assert snap is not None
    rows = sqlite3.connect(snap).execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert rows == 2


def test_models_declare_indexes_on_time_columns():
    from app.models import ActionLog, SensorLog

    assert SensorLog.__table__.c.captured_at.index is True
    assert ActionLog.__table__.c.created_at.index is True


def test_ensure_indexes_patches_pre_existing_database(tmp_path):
    """A DB created BEFORE index=True existed gets the indexes at startup."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sensor_logs (id INTEGER PRIMARY KEY, incubator_id INTEGER, "
                 "temperature_c REAL, humidity_pct REAL, captured_at DATETIME)")
    conn.execute("CREATE TABLE action_logs (id INTEGER PRIMARY KEY, action TEXT, payload TEXT, created_at DATETIME)")
    conn.commit()
    conn.close()

    eng = make_engine(_url(tmp_path, "old.db"))
    done = ensure_indexes(eng)
    assert set(done) == {name for name, _, _ in INDEXES}
    with eng.connect() as c:
        names = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}
        assert {"ix_sensor_logs_captured_at", "ix_action_logs_created_at"} <= names
        plan = c.execute(text("EXPLAIN QUERY PLAN SELECT * FROM sensor_logs ORDER BY captured_at DESC LIMIT 1")).fetchall()
        assert any("ix_sensor_logs_captured_at" in str(row) for row in plan)
    # Idempotent.
    assert set(ensure_indexes(eng)) == set(done)
    eng.dispose()


def test_ensure_indexes_skips_missing_tables_and_never_raises(tmp_path):
    eng = make_engine(_url(tmp_path, "empty.db"))
    assert ensure_indexes(eng) == []
    eng.dispose()
