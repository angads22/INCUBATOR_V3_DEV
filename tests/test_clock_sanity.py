"""Boot-time clock-rollback detection (RTC-less power-cut breadcrumb).

An RTC-less Pi freezes its clock across a power cut, so on boot it can read
earlier than events already logged. _detect_clock_rollback notes this (it does
not fix the clock — the DS3231 RTC / NTP does) so a backwards timeline is
explainable. It must reuse existing log rows and never write unless a real
rollback is seen.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import main
from app.database import get_db
from app.models import ActionLog


def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _rollback_rows(db):
    return [
        r for r in db.scalars(select(ActionLog)).all()
        if r.action == "system.clock_rolled_back"
    ]


def test_rollback_logged_when_clock_behind_newest_log(client):
    db = next(get_db())
    try:
        # A log entry stamped an hour in the "future" relative to now → the
        # system clock is effectively an hour behind.
        future = _naive_utc(datetime.now(timezone.utc) + timedelta(hours=1))
        db.add(ActionLog(action="setup_complete", payload="x", created_at=future))
        db.commit()

        main._detect_clock_rollback(db)

        rows = _rollback_rows(db)
        assert len(rows) == 1
        assert "behind" in rows[0].payload
    finally:
        db.close()


def test_no_rollback_when_clock_is_current(client):
    db = next(get_db())
    try:
        recent = _naive_utc(datetime.now(timezone.utc) - timedelta(seconds=5))
        db.add(ActionLog(action="setup_complete", payload="x", created_at=recent))
        db.commit()

        main._detect_clock_rollback(db)

        assert _rollback_rows(db) == []
    finally:
        db.close()


def test_no_rollback_on_empty_db(client):
    db = next(get_db())
    try:
        main._detect_clock_rollback(db)          # no logs at all → nothing to compare
        assert _rollback_rows(db) == []
    finally:
        db.close()
