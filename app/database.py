import logging
import os
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _sqlite_path(db_url: str | None) -> str | None:
    url = db_url or settings.db_url
    if not url.startswith("sqlite"):
        return None
    path = url.split("///", 1)[-1]
    if not path or path == ":memory:":
        return None
    return path


def _quick_check_ok(path: str) -> bool:
    import sqlite3

    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        return bool(row and str(row[0]).lower() == "ok")
    except Exception:  # noqa: BLE001
        return False


def _backup_dir(path: str) -> str:
    return os.path.join(os.path.dirname(path) or ".", "backups")


def snapshot_sqlite(db_url: str | None = None, keep: int = 3) -> str | None:
    """Daily database snapshot so corruption never means starting from zero.

    Uses SQLite's online backup API (safe while the app is running) to copy
    the database to ``<dbdir>/backups/incubator-YYYYMMDD.db``, at most once
    per day, keeping the ``keep`` newest snapshots. A few hundred KB per copy
    — cheap insurance for the account, settings, and growth history that
    ensure_sqlite_healthy would otherwise have to abandon on corruption.
    Returns the snapshot path when one was written, else None. Never raises.
    """
    path = _sqlite_path(db_url)
    if not path or not os.path.exists(path):
        return None
    try:
        import sqlite3

        dest_dir = _backup_dir(path)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"incubator-{time.strftime('%Y%m%d')}.db")
        if os.path.exists(dest):
            return None  # already snapshotted today
        src = sqlite3.connect(path, timeout=5)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        # Rotate: newest ``keep`` snapshots survive.
        snaps = sorted(
            f for f in os.listdir(dest_dir)
            if f.startswith("incubator-") and f.endswith(".db")
        )
        for old in snaps[:-keep] if keep > 0 else []:
            try:
                os.remove(os.path.join(dest_dir, old))
            except OSError:
                pass
        logger.info("Database snapshot written: %s", dest)
        return dest
    except Exception as exc:  # noqa: BLE001 — snapshots must never break startup
        logger.warning("Database snapshot failed: %s", exc)
        return None


def _restore_newest_backup(path: str) -> bool:
    """Copy the newest healthy snapshot into place. True on success."""
    dest_dir = _backup_dir(path)
    if not os.path.isdir(dest_dir):
        return False
    snaps = sorted(
        (f for f in os.listdir(dest_dir) if f.startswith("incubator-") and f.endswith(".db")),
        reverse=True,
    )
    for name in snaps:
        candidate = os.path.join(dest_dir, name)
        if not _quick_check_ok(candidate):
            continue
        try:
            import shutil

            shutil.copy2(candidate, path)
            logger.warning("Restored database from snapshot %s", candidate)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not restore snapshot %s: %s", candidate, exc)
    return False


def ensure_sqlite_healthy(db_url: str | None = None) -> bool:
    """Quarantine a corrupted SQLite file so the app can ALWAYS start —
    and restore the newest healthy snapshot instead of starting empty.

    Farm power cuts mid-write eventually corrupt an SD-card database; without
    this, a corrupt file makes the app crash on startup, systemd restarts it,
    it crashes again — forever — while the incubator sits unheated. Instead:
    run a quick integrity check and, on failure, move the file (and its
    -wal/-shm sidecars) aside with a .corrupt-<timestamp> suffix, then try to
    restore the newest snapshot written by :func:`snapshot_sqlite` — so the
    farmer keeps their account, settings, and growth history from (at worst)
    yesterday. Only when no healthy snapshot exists does the device fall back
    to a fresh database. The quarantined file is kept for forensics.

    Returns True when the database is healthy, absent, or restored from a
    snapshot; False only when it was quarantined with nothing to restore.
    Best-effort by design: never raises.
    """
    path = _sqlite_path(db_url)
    if not path or not os.path.exists(path):
        return True

    if _quick_check_ok(path):
        return True

    stamp = time.strftime("%Y%m%d%H%M%S")
    try:
        os.replace(path, f"{path}.corrupt-{stamp}")
        for sidecar in (f"{path}-wal", f"{path}-shm"):
            if os.path.exists(sidecar):
                os.replace(sidecar, f"{sidecar}.corrupt-{stamp}")
        logger.error(
            "Database corrupt — quarantined to %s.corrupt-%s", path, stamp,
        )
    except Exception as exc:  # noqa: BLE001 — never block startup on the rescue itself
        logger.error("Could not quarantine corrupt database %s: %s", path, exc)

    if _restore_newest_backup(path):
        return True
    logger.error("No healthy snapshot to restore — starting with a fresh database")
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
