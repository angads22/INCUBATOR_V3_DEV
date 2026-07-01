"""
Growth tracking + vision-driven incubator actions.

Every candling produces a vision reading (estimated incubation day + stage +
fertility label). Persisting those per egg gives a *timeline*, and this service
turns that timeline into:

  * a growth trajectory — is the embryo advancing, stalled, non-viable, or
    ready to hatch;
  * recommended (and optionally auto-executed) incubator actions:
      - lockdown  → stop the egg turner and raise the humidity target for hatch
                    (the control loop reads turner_enabled / target_humidity_pct
                    from settings, so this is all it takes);
      - flag_nonviable → mark the egg so the operator can pull it;
      - hatch_watch → advisory that hatching is imminent.

The decision logic (:func:`assess_growth`) is a PURE function so it is fully
unit-tested with no DB or hardware. :class:`GrowthService` records observations,
runs the assessment, and applies actions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Egg, GrowthObservation

if TYPE_CHECKING:
    from ..services.hardware_service import HardwareService

logger = logging.getLogger(__name__)

# Classifier labels that indicate an egg is not developing.
NONVIABLE_LABELS = {"infertile", "dead_embryo", "blood_ring", "dead", "rotten"}

# Defaults (overridable via settings).
DEFAULT_LOCKDOWN_DAYS_BEFORE_HATCH = 3
DEFAULT_LOCKDOWN_HUMIDITY_PCT = 65.0
# Below this development-per-real-day, a mid-incubation embryo looks stalled.
MIN_GROWTH_PER_DAY = 0.2


def _ts(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt else 0.0


def assess_growth(
    observations: list[dict[str, Any]],
    incubation_days: int,
    *,
    now_ts: float,
    lockdown_days_before_hatch: int = DEFAULT_LOCKDOWN_DAYS_BEFORE_HATCH,
    min_growth_per_day: float = MIN_GROWTH_PER_DAY,
) -> dict[str, Any]:
    """Assess an egg's development from its observation timeline. Pure.

    ``observations`` are dicts with ``day_estimate`` (float), ``stage`` (str),
    ``label`` (str|None), ``created_ts`` (epoch seconds), in any order.

    Returns {status, latest_day, latest_stage, growth_per_day, observation_count,
    viable, recommended_actions:[{action, reason}]}. ``status`` is one of
    no_data | developing | stalled | nonviable | ready_to_hatch | hatching.
    """
    days = max(1, int(incubation_days))
    obs = sorted(observations, key=lambda o: o.get("created_ts", 0.0))
    if not obs:
        return {
            "status": "no_data", "latest_day": None, "latest_stage": None,
            "growth_per_day": None, "observation_count": 0, "viable": None,
            "recommended_actions": [],
        }

    latest = obs[-1]
    latest_day = float(latest.get("day_estimate") or 0.0)
    latest_stage = latest.get("stage") or "unclear"

    # Viability from the most recent readings (need corroboration — a single
    # blurry frame shouldn't condemn an egg).
    recent = obs[-3:]
    nonviable_hits = sum(
        1 for o in recent
        if (o.get("label") in NONVIABLE_LABELS) or o.get("stage") == "infertile"
    )
    nonviable = nonviable_hits >= 2
    viable = not nonviable

    # Development-per-real-day between the first and last reading.
    growth_per_day = None
    if len(obs) >= 2:
        span_days = (obs[-1].get("created_ts", 0.0) - obs[0].get("created_ts", 0.0)) / 86400.0
        if span_days >= 0.5:
            first_day = float(obs[0].get("day_estimate") or 0.0)
            growth_per_day = round((latest_day - first_day) / span_days, 2)

    actions: list[dict[str, str]] = []

    if nonviable:
        status = "nonviable"
        actions.append({
            "action": "flag_nonviable",
            "reason": f"{nonviable_hits} recent readings look infertile / non-viable",
        })
        return {
            "status": status, "latest_day": round(latest_day, 1), "latest_stage": latest_stage,
            "growth_per_day": growth_per_day, "observation_count": len(obs),
            "viable": viable, "recommended_actions": actions,
        }

    status = "developing"

    near_hatch = latest_day >= (days - lockdown_days_before_hatch)
    if latest_stage == "hatching":
        status = "hatching"
    elif near_hatch:
        status = "ready_to_hatch"

    if near_hatch or latest_stage == "hatching":
        actions.append({
            "action": "lockdown",
            "reason": f"day ~{round(latest_day)} of {days}: stop turning and raise humidity for hatch",
        })
    if latest_stage == "hatching":
        actions.append({"action": "hatch_watch", "reason": "hatching signs detected — check on the eggs"})

    # Stalled: enough time has passed but development isn't climbing, and we're
    # not simply already near the end.
    if (
        status == "developing"
        and growth_per_day is not None
        and growth_per_day < min_growth_per_day
        and latest_day < days * 0.9
    ):
        status = "stalled"
        actions.append({
            "action": "review",
            "reason": "development has not advanced between candlings — inspect this egg",
        })

    return {
        "status": status, "latest_day": round(latest_day, 1), "latest_stage": latest_stage,
        "growth_per_day": growth_per_day, "observation_count": len(obs),
        "viable": viable, "recommended_actions": actions,
    }


class GrowthService:
    """Persists vision observations and drives incubator actions from them."""

    def __init__(
        self,
        incubation_days: int = 21,
        *,
        auto_actions: bool = True,
        lockdown_humidity_pct: float = DEFAULT_LOCKDOWN_HUMIDITY_PCT,
        lockdown_days_before_hatch: int = DEFAULT_LOCKDOWN_DAYS_BEFORE_HATCH,
    ) -> None:
        self.incubation_days = max(1, int(incubation_days))
        self.auto_actions = auto_actions
        self.lockdown_humidity_pct = lockdown_humidity_pct
        self.lockdown_days_before_hatch = lockdown_days_before_hatch

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        db: Session,
        egg_id: int,
        *,
        day_estimate: float,
        stage: str = "unclear",
        confidence: float = 0.0,
        label: str | None = None,
        backend: str = "unknown",
        source: str = "candle",
    ) -> GrowthObservation:
        row = GrowthObservation(
            egg_id=egg_id,
            day_estimate=float(day_estimate or 0.0),
            stage=stage or "unclear",
            confidence=float(confidence or 0.0),
            label=label,
            backend=backend,
            source=source,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    # ------------------------------------------------------------------
    # Assessment
    # ------------------------------------------------------------------

    def _observations(self, db: Session, egg_id: int) -> list[dict[str, Any]]:
        rows = db.scalars(
            select(GrowthObservation).where(GrowthObservation.egg_id == egg_id)
        ).all()
        return [
            {
                "day_estimate": r.day_estimate, "stage": r.stage, "label": r.label,
                "confidence": r.confidence, "created_ts": _ts(r.created_at), "id": r.id,
            }
            for r in rows
        ]

    def assess(self, db: Session, egg_id: int) -> dict[str, Any]:
        import time

        result = assess_growth(
            self._observations(db, egg_id),
            self.incubation_days,
            now_ts=time.time(),
            lockdown_days_before_hatch=self.lockdown_days_before_hatch,
        )
        result["egg_id"] = egg_id
        return result

    def summary(self, db: Session) -> list[dict[str, Any]]:
        eggs = db.scalars(select(Egg).order_by(Egg.id)).all()
        out = []
        for egg in eggs:
            a = self.assess(db, egg.id)
            out.append({
                "egg_id": egg.id,
                "label": egg.label or f"Egg {egg.id}",
                "state": egg.state,
                "status": a["status"],
                "latest_day": a["latest_day"],
                "latest_stage": a["latest_stage"],
                "observation_count": a["observation_count"],
                "recommended_actions": a["recommended_actions"],
            })
        return out

    # ------------------------------------------------------------------
    # Acting on the incubator
    # ------------------------------------------------------------------

    def apply_actions(
        self,
        db: Session,
        egg_id: int,
        hardware: "HardwareService | None" = None,
        actions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute the recommended (or explicitly requested) actions.

        Returns {applied:[{action, ok, detail}], assessment}. Safe: lockdown just
        adjusts settings the control loop already honours; flagging is a DB mark.
        """
        assessment = self.assess(db, egg_id)
        wanted = actions if actions is not None else [a["action"] for a in assessment["recommended_actions"]]
        applied: list[dict[str, Any]] = []
        for action in wanted:
            applied.append(self._apply_one(db, egg_id, action, hardware))
        db.commit()
        assessment["applied"] = applied
        return assessment

    def _apply_one(self, db: Session, egg_id: int, action: str, hardware) -> dict[str, Any]:
        from ..settings_store import update_settings

        if action == "lockdown":
            # Stop turning + raise humidity for hatch. The control loop and the
            # web app both read these from settings, so this is authoritative.
            update_settings(db, {
                "turner_enabled": "false",
                "target_humidity_pct": str(self.lockdown_humidity_pct),
            })
            return {"action": action, "ok": True,
                    "detail": f"turner off, humidity target → {self.lockdown_humidity_pct}%"}

        if action == "flag_nonviable":
            egg = db.scalar(select(Egg).where(Egg.id == egg_id))
            if egg is not None:
                egg.state = "nonviable"
            return {"action": action, "ok": egg is not None,
                    "detail": "egg marked non-viable" if egg else "egg not found"}

        if action in ("hatch_watch", "review"):
            # Advisory only — surfaced to the operator, no hardware change.
            return {"action": action, "ok": True, "detail": "advisory noted"}

        return {"action": action, "ok": False, "detail": "unknown action"}
