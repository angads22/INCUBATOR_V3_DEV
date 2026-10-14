"""StorageService.enforce() must be cheap when nothing needs pruning: one
disk stat + one cached aggregate, never a full egg_photos row load."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.models import EggPhoto
from app.services.storage_service import StorageService


def _svc(tmp_path, **kw):
    kw.setdefault("min_free_mb", 1)
    kw.setdefault("target_free_mb", 2)
    kw.setdefault("max_dir_mb", 0)
    kw.setdefault("keep_min", 0)
    return StorageService(str(tmp_path), **kw)


def _seed(db, n, size=1000, age_days=0):
    import uuid

    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    for i in range(n):
        db.add(EggPhoto(label="egg", path=f"/tmp/nope/{uuid.uuid4().hex}-{i}.jpg", size_bytes=size, created_at=created))
    db.commit()


class _SqlSpy:
    def __init__(self, engine):
        self.statements = []
        self._engine = engine
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement.lower())

    def close(self):
        event.remove(self._engine, "before_cursor_execute", self._record)

    @property
    def row_loads(self):
        # A full row load selects egg_photos.path; aggregates and tuple loads do not.
        return [s for s in self.statements if "egg_photos.path" in s and "select" in s]


def test_no_pressure_runs_only_aggregates(client, tmp_path):
    from app.database import SessionLocal, engine

    db = SessionLocal()
    spy = _SqlSpy(engine)
    try:
        _seed(db, 20)
        svc = _svc(tmp_path)
        svc._disk_free_total = lambda: (10 * 1024 ** 3, 32 * 1024 ** 3)   # plenty free
        spy.statements.clear()
        for _ in range(5):
            r = svc.enforce(db)
            assert r["deleted"] == 0 and r["checked"] == "aggregate"
        assert spy.row_loads == []
        aggregates = [s for s in spy.statements if "sum(" in s]
        assert len(aggregates) == 1                                        # cached across polls
    finally:
        spy.close()
        db.close()


def test_pressure_loads_rows_and_prunes(client, tmp_path):
    from app.database import SessionLocal, engine

    db = SessionLocal()
    spy = _SqlSpy(engine)
    try:
        _seed(db, 10, size=1024 * 1024)
        svc = _svc(tmp_path)
        svc._disk_free_total = lambda: (0, 32 * 1024 ** 3)                # disk full
        spy.statements.clear()
        r = svc.enforce(db)
        assert r["deleted"] == 2 and r["checked"] == "rows"              # frees up to the 2 MB target
        assert db.query(EggPhoto).count() == 8
    finally:
        spy.close()
        db.close()


def test_write_invalidates_aggregate_cache(client, tmp_path):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        svc = _svc(tmp_path, max_dir_mb=1)
        svc._disk_free_total = lambda: (10 * 1024 ** 3, 32 * 1024 ** 3)
        _seed(db, 1, size=600 * 1024)
        assert svc.enforce(db)["deleted"] == 0
        assert svc._dir_cache is not None
        svc._invalidate_cache()
        _seed(db, 1, size=600 * 1024)                                      # now over the 1 MB cap
        r = svc.enforce(db)
        assert r["deleted"] == 1
        assert svc._dir_cache is None                                      # pruning dropped the cache
    finally:
        db.close()


def test_retention_due_is_detected_from_min_created_at(client, tmp_path):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        svc = _svc(tmp_path, retention_days=7)
        svc._disk_free_total = lambda: (10 * 1024 ** 3, 32 * 1024 ** 3)
        _seed(db, 3, age_days=1)
        assert svc.enforce(db)["checked"] == "aggregate"
        _seed(db, 2, age_days=30)
        svc._invalidate_cache()
        r = svc.enforce(db)
        assert r["deleted"] == 2 and db.query(EggPhoto).count() == 3
    finally:
        db.close()


def test_empty_table_is_a_noop(client, tmp_path):
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        svc = _svc(tmp_path)
        svc._disk_free_total = lambda: (0, 1)
        assert svc.enforce(db)["deleted"] == 0
    finally:
        db.close()
