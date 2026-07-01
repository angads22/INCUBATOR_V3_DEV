"""Growth tracking + vision-driven incubator actions."""

from app.services.growth_service import assess_growth

NOW = 1_000_000.0
DAY = 86400.0


def _o(day, stage="mid", label=None, age_days=0.0):
    return {"day_estimate": day, "stage": stage, "label": label, "created_ts": NOW - age_days * DAY}


# ── Pure assessment ──────────────────────────────────────────────────────────

def test_no_data():
    a = assess_growth([], 21, now_ts=NOW)
    assert a["status"] == "no_data" and a["recommended_actions"] == []


def test_developing_rising_estimates():
    obs = [_o(2, age_days=2), _o(5, age_days=1), _o(8, age_days=0)]
    a = assess_growth(obs, 21, now_ts=NOW)
    assert a["status"] == "developing"
    assert a["growth_per_day"] == 3.0
    assert a["recommended_actions"] == []


def test_stalled_flat_estimates():
    obs = [_o(5.0, age_days=3), _o(5.2, age_days=1.5), _o(5.1, age_days=0)]
    a = assess_growth(obs, 21, now_ts=NOW)
    assert a["status"] == "stalled"
    assert any(x["action"] == "review" for x in a["recommended_actions"])


def test_nonviable_from_labels():
    obs = [_o(0, stage="infertile", label="infertile", age_days=2),
           _o(0, stage="infertile", label="dead_embryo", age_days=0)]
    a = assess_growth(obs, 21, now_ts=NOW)
    assert a["status"] == "nonviable" and a["viable"] is False
    assert any(x["action"] == "flag_nonviable" for x in a["recommended_actions"])


def test_ready_to_hatch_triggers_lockdown():
    a = assess_growth([_o(19, stage="late")], 21, now_ts=NOW)
    assert a["status"] == "ready_to_hatch"
    assert any(x["action"] == "lockdown" for x in a["recommended_actions"])


def test_hatching_triggers_lockdown_and_watch():
    a = assess_growth([_o(20.5, stage="hatching")], 21, now_ts=NOW)
    assert a["status"] == "hatching"
    acts = {x["action"] for x in a["recommended_actions"]}
    assert "lockdown" in acts and "hatch_watch" in acts


# ── Service: record → assess → act ───────────────────────────────────────────

def _db():
    from app.database import get_db
    return next(get_db())


def _make_egg(db):
    from app.models import Incubator, Egg
    inc = Incubator(name="I1")
    db.add(inc)
    db.commit()
    db.refresh(inc)
    egg = Egg(incubator_id=inc.id, label="Egg 1")
    db.add(egg)
    db.commit()
    db.refresh(egg)
    return egg


def test_service_record_and_assess(client):
    from app.services.growth_service import GrowthService
    svc = GrowthService(incubation_days=21, auto_actions=False)
    db = _db()
    try:
        egg = _make_egg(db)
        svc.record(db, egg.id, day_estimate=9.0, stage="mid", confidence=0.8, label="fertile")
        a = svc.assess(db, egg.id)
        assert a["observation_count"] == 1
        assert a["latest_day"] == 9.0 and a["status"] == "developing"
    finally:
        db.close()


def test_lockdown_action_stops_turner_and_raises_humidity(client):
    from app.services.growth_service import GrowthService
    from app.settings_store import get_settings
    svc = GrowthService(incubation_days=21, auto_actions=True, lockdown_humidity_pct=65)
    db = _db()
    try:
        egg = _make_egg(db)
        svc.record(db, egg.id, day_estimate=19.0, stage="late", confidence=0.8)
        result = svc.apply_actions(db, egg.id, hardware=None)
        applied = {a["action"]: a for a in result["applied"]}
        assert applied["lockdown"]["ok"] is True
        s = get_settings(db)
        assert s["turner_enabled"] == "false"
        assert float(s["target_humidity_pct"]) == 65.0
    finally:
        db.close()


def test_flag_nonviable_marks_egg(client):
    from app.services.growth_service import GrowthService
    from app.models import Egg
    svc = GrowthService(incubation_days=21, auto_actions=True)
    db = _db()
    try:
        egg = _make_egg(db)
        svc.record(db, egg.id, day_estimate=0.0, stage="infertile", label="infertile")
        svc.record(db, egg.id, day_estimate=0.0, stage="infertile", label="dead_embryo")
        svc.apply_actions(db, egg.id, hardware=None)
        assert db.get(Egg, egg.id).state == "nonviable"
    finally:
        db.close()


# ── Endpoints + candle integration ───────────────────────────────────────────

def test_growth_endpoints(client):
    db = _db()
    try:
        egg = _make_egg(db)
        egg_id = egg.id
    finally:
        db.close()

    summary = client.get("/api/vision/growth").json()
    assert summary["ok"] and any(e["egg_id"] == egg_id for e in summary["eggs"])

    detail = client.get(f"/api/vision/growth/{egg_id}").json()
    assert detail["ok"] and detail["egg_id"] == egg_id and "observations" in detail

    applied = client.post(f"/api/vision/growth/{egg_id}/apply", json={"actions": ["hatch_watch"]}).json()
    assert applied["ok"] and applied["applied"][0]["action"] == "hatch_watch"


def test_candle_records_growth(client):
    db = _db()
    try:
        egg = _make_egg(db)
        egg_id = egg.id
    finally:
        db.close()

    r = client.post("/api/vision/candle", json={"egg_id": egg_id, "persist": True})
    body = r.json()
    assert body.get("growth") is not None
    assert body["growth"]["observation_count"] >= 1
    # The stage estimate rode along on the candle response.
    assert body["stage"]["ok"] is True
