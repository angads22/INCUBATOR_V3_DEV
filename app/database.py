import logging
import os
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def ensure_sqlite_healthy(db_url: str | None = None) -> bool:
    """Quarantine a corrupted SQLite file so the app can ALWAYS start.

    Farm power cuts mid-write eventually corrupt an SD-card database; without
    this, a corrupt file makes the app crash on startup, systemd restarts it,
    it crashes again — forever — while the incubator sits unheated. Instead:
    run a quick integrity check and, on failure, move the file (and its
    -wal/-shm sidecars) aside with a .corrupt-<timestamp> suffix so the normal
    startup path recreates a fresh database. Settings/history are lost but the
    appliance keeps working; the quarantined file is kept for forensics.

    Returns True when the database is healthy or absent, False if it was
    quarantined. Best-effort by design: never raises.
    """
    url = db_url or settings.db_url
    if not url.startswith("sqlite"):
        return True
    path = url.split("///", 1)[-1]
    if not path or path == ":memory:" or not os.path.exists(path):
        return True

    import sqlite3

    reason = ""
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if row and str(row[0]).lower() == "ok":
            return True
        reason = f"quick_check said {row[0]!r}" if row else "quick_check returned nothing"
    except Exception as exc:  # noqa: BLE001 — any open/read error means quarantine
        reason = f"open/check failed: {exc}"

    stamp = time.strftime("%Y%m%d%H%M%S")
    try:
        os.replace(path, f"{path}.corrupt-{stamp}")
        for sidecar in (f"{path}-wal", f"{path}-shm"):
            if os.path.exists(sidecar):
                os.replace(sidecar, f"{sidecar}.corrupt-{stamp}")
        logger.error(
            "Database corrupt (%s) — quarantined to %s.corrupt-%s and starting fresh",
            reason, path, stamp,
        )
    except Exception as exc:  # noqa: BLE001 — never block startup on the rescue itself
        logger.error("Could not quarantine corrupt database %s: %s", path, exc)
    return False


engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False} if settings.db_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
