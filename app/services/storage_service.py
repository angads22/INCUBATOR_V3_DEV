"""
Egg-photo storage with disk-pressure auto-prune.

Labeled candling photos are saved under ``<captures_dir>/eggs`` and tracked in
the ``egg_photos`` table. Because the Pi Zero runs off a small SD card, an
unbounded photo pile would eventually fill the disk and wedge the appliance
(no logging, no DB writes, failed captures). The janitor here keeps that from
happening: whenever the filesystem gets close to full — or an optional
directory cap / age cap is exceeded — it deletes the OLDEST non-pinned photos
(and their files) until there's comfortable headroom again.

The decision of *what* to delete is a pure function (:func:`plan_pruning`) so it
is exhaustively unit-tested with no disk I/O. The :class:`StorageService`
wrapper does the stat-ing, file deletion, and DB bookkeeping around it.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EggPhoto

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_SAFE_LABEL = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"


def _slug(label: str) -> str:
    """A short filesystem-safe slug from a free-text label."""
    cleaned = "".join(c if c in _SAFE_LABEL else "-" for c in (label or "egg").strip())
    cleaned = cleaned.strip("-") or "egg"
    return cleaned[:24]


@dataclass(frozen=True)
class _Photo:
    id: int
    size: int
    created_ts: float
    pinned: bool


def plan_pruning(
    photos: Iterable[dict[str, Any]],
    *,
    free_bytes: int,
    dir_bytes: int,
    min_free_bytes: int,
    target_free_bytes: int,
    max_dir_bytes: int,
    keep_min: int,
    now_ts: float,
    retention_seconds: float,
) -> list[int]:
    """Decide which photo ids to delete, oldest-first. Pure — no I/O.

    ``photos`` are dicts with ``id``, ``size`` (bytes), ``created_ts`` (epoch
    seconds) and ``pinned`` (bool).

    Rules, in order:
      * pinned photos are never deleted;
      * the newest ``keep_min`` photos (by time) are always protected;
      * any remaining photo older than ``retention_seconds`` is pruned (when a
        retention is configured);
      * if free space is below ``min_free_bytes`` — or the directory exceeds
        ``max_dir_bytes`` when that cap is enabled — the oldest remaining
        candidates are pruned (simulating the freed bytes) until free space
        reaches ``target_free_bytes`` and the directory is back under its cap,
        or there are no more candidates.
    """
    items = [
        _Photo(int(p["id"]), int(p["size"]), float(p["created_ts"]), bool(p.get("pinned", False)))
        for p in photos
    ]
    # Oldest first for deletion order; newest-first slice marks the protected set.
    oldest_first = sorted(items, key=lambda p: (p.created_ts, p.id))
    newest_first = sorted(items, key=lambda p: (p.created_ts, p.id), reverse=True)
    protected = {p.id for p in newest_first[: max(0, keep_min)]}

    target_free_bytes = max(target_free_bytes, min_free_bytes)
    to_delete: list[int] = []
    sim_free = free_bytes
    sim_dir = dir_bytes

    def deletable(p: _Photo) -> bool:
        return not p.pinned and p.id not in protected and p.id not in to_delete

    # 1) Age-based retention.
    if retention_seconds and retention_seconds > 0:
        for p in oldest_first:
            if not deletable(p):
                continue
            if now_ts - p.created_ts > retention_seconds:
                to_delete.append(p.id)
                sim_free += p.size
                sim_dir -= p.size

    # 2) Space/quota relief.
    def under_pressure() -> bool:
        if sim_free < min_free_bytes:
            return True
        if max_dir_bytes and sim_dir > max_dir_bytes:
            return True
        return False

    def relieved() -> bool:
        return sim_free >= target_free_bytes and (not max_dir_bytes or sim_dir <= max_dir_bytes)

    if under_pressure():
        for p in oldest_first:
            if relieved():
                break
            if not deletable(p):
                continue
            to_delete.append(p.id)
            sim_free += p.size
            sim_dir -= p.size

    return to_delete


class StorageService:
    """Saves labeled egg photos and prunes them under disk pressure."""

    def __init__(
        self,
        captures_dir: str,
        *,
        enabled: bool = True,
        min_free_mb: int = 300,
        target_free_mb: int = 600,
        max_dir_mb: int = 1024,
        keep_min: int = 12,
        retention_days: int = 0,
    ) -> None:
        self.enabled = enabled
        self.root = Path(captures_dir) / "eggs"
        self.min_free_bytes = max(0, min_free_mb) * _MB
        self.target_free_bytes = max(0, target_free_mb) * _MB
        self.max_dir_bytes = max(0, max_dir_mb) * _MB
        self.keep_min = max(0, keep_min)
        self.retention_seconds = max(0, retention_days) * 86400

    # ------------------------------------------------------------------
    # Disk helpers
    # ------------------------------------------------------------------

    def _disk_free_total(self) -> tuple[int, int]:
        """Free + total bytes on the captures filesystem. Robust to a missing dir."""
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
            return usage.free, usage.total
        except Exception as exc:  # noqa: BLE001 — never let a stat error break a capture
            logger.debug("disk_usage(%s) failed: %s", probe, exc)
            return 0, 0

    @staticmethod
    def _ts(dt: datetime) -> float:
        # Stored timestamps are naive UTC (see models._utcnow).
        return dt.replace(tzinfo=timezone.utc).timestamp() if dt else 0.0

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def save_photo(
        self,
        db: Session,
        jpeg_bytes: bytes,
        *,
        label: str = "egg",
        egg_id: int | None = None,
        backend: str = "unknown",
        confidence: float | None = None,
    ) -> EggPhoto:
        """Write JPEG bytes to a labeled file and track it, then enforce limits."""
        self.root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{_slug(label)}_{ts}_{uuid.uuid4().hex[:6]}.jpg"
        dest = self.root / name
        dest.write_bytes(jpeg_bytes)
        return self._track(db, dest, label=label, egg_id=egg_id, backend=backend, confidence=confidence)

    def register(
        self,
        db: Session,
        path: str,
        *,
        label: str = "egg",
        egg_id: int | None = None,
        backend: str = "unknown",
        confidence: float | None = None,
    ) -> EggPhoto | None:
        """Track a file that already exists on disk (e.g. a candling capture)."""
        p = Path(path)
        if not p.is_file():
            return None
        return self._track(db, p, label=label, egg_id=egg_id, backend=backend, confidence=confidence)

    def _track(
        self,
        db: Session,
        path: Path,
        *,
        label: str,
        egg_id: int | None,
        backend: str,
        confidence: float | None,
    ) -> EggPhoto:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        existing = db.scalar(select(EggPhoto).where(EggPhoto.path == key))
        if existing is not None:
            existing.label = label or existing.label
            existing.backend = backend
            existing.confidence = confidence
            existing.size_bytes = size
            row = existing
        else:
            row = EggPhoto(
                egg_id=egg_id,
                label=label or "egg",
                path=key,
                backend=backend,
                confidence=confidence,
                size_bytes=size,
            )
            db.add(row)
        db.commit()
        db.refresh(row)
        self.enforce(db)
        return row

    # ------------------------------------------------------------------
    # Pruning
    # ------------------------------------------------------------------

    def enforce(self, db: Session) -> dict[str, Any]:
        """Prune oldest non-pinned photos until disk pressure is relieved."""
        if not self.enabled:
            return {"enabled": False, "deleted": 0, "freed_bytes": 0}
        rows = db.scalars(select(EggPhoto)).all()
        dir_bytes = sum(int(r.size_bytes or 0) for r in rows)
        free, _total = self._disk_free_total()
        photos = [
            {"id": r.id, "size": int(r.size_bytes or 0), "created_ts": self._ts(r.created_at), "pinned": r.pinned}
            for r in rows
        ]
        ids = plan_pruning(
            photos,
            free_bytes=free,
            dir_bytes=dir_bytes,
            min_free_bytes=self.min_free_bytes,
            target_free_bytes=self.target_free_bytes,
            max_dir_bytes=self.max_dir_bytes,
            keep_min=self.keep_min,
            now_ts=time.time(),
            retention_seconds=self.retention_seconds,
        )
        if not ids:
            return {"enabled": True, "deleted": 0, "freed_bytes": 0}
        by_id = {r.id: r for r in rows}
        freed = 0
        deleted = 0
        for pid in ids:
            row = by_id.get(pid)
            if row is None:
                continue
            try:
                Path(row.path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not delete %s: %s", row.path, exc)
            freed += int(row.size_bytes or 0)
            deleted += 1
            db.delete(row)
        db.commit()
        logger.info("Storage janitor pruned %d photo(s), freed ~%d MB", deleted, freed // _MB)
        return {"enabled": True, "deleted": deleted, "freed_bytes": freed}

    def delete(self, db: Session, photo_id: int) -> bool:
        row = db.scalar(select(EggPhoto).where(EggPhoto.id == photo_id))
        if row is None:
            return False
        try:
            Path(row.path).unlink(missing_ok=True)
        except OSError:
            pass
        db.delete(row)
        db.commit()
        return True

    def set_pinned(self, db: Session, photo_id: int, pinned: bool) -> EggPhoto | None:
        row = db.scalar(select(EggPhoto).where(EggPhoto.id == photo_id))
        if row is None:
            return None
        row.pinned = pinned
        db.commit()
        db.refresh(row)
        return row

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def usage(self, db: Session) -> dict[str, Any]:
        rows = db.scalars(select(EggPhoto)).all()
        dir_bytes = sum(int(r.size_bytes or 0) for r in rows)
        free, total = self._disk_free_total()
        return {
            "enabled": self.enabled,
            "photo_count": len(rows),
            "pinned_count": sum(1 for r in rows if r.pinned),
            "dir_mb": round(dir_bytes / _MB, 1),
            "free_mb": round(free / _MB, 1),
            "total_mb": round(total / _MB, 1),
            "used_pct": round((1 - free / total) * 100, 1) if total else None,
            "min_free_mb": self.min_free_bytes // _MB,
            "target_free_mb": self.target_free_bytes // _MB,
            "max_dir_mb": self.max_dir_bytes // _MB,
            "keep_min": self.keep_min,
            "retention_days": self.retention_seconds // 86400 if self.retention_seconds else 0,
            "under_pressure": bool(free and free < self.min_free_bytes)
            or bool(self.max_dir_bytes and dir_bytes > self.max_dir_bytes),
        }
