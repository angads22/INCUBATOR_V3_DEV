"""SetupModeService: tmpfs-backed state with a reason + timestamp, so setup
mode can prove *recent physical presence* and never outlives a boot."""

import dataclasses
import json
import time

from app.services.setup_mode_service import PHYSICAL_REASONS, SetupModeService


def test_default_state_file_comes_from_settings_and_parent_is_created(monkeypatch, tmp_path):
    from app.config import settings

    target = tmp_path / "run" / "incubator" / "setup-state.json"
    monkeypatch.setattr("app.config.settings", dataclasses.replace(settings, setup_state_path=str(target)))
    svc = SetupModeService()
    assert svc.state_file == target
    svc.enter_setup_mode("button_hold")
    assert target.exists()                       # parent dirs created on save
    assert json.loads(target.read_text())["reason"] == "button_hold"


def test_enter_records_reason_and_timestamp_and_persists(tmp_path):
    path = str(tmp_path / "s.json")
    svc = SetupModeService(path)
    before = time.time()
    svc.enter_setup_mode("button_hold")
    state = svc.status()
    assert state["setup_mode"] is True
    assert state["reason"] == "button_hold" == svc.reason()
    assert state["entered_at"] >= before
    again = SetupModeService(path)               # survives a process restart
    assert again.is_setup_mode() and again.reason() == "button_hold"
    assert again.entered_at() == state["entered_at"]


def test_entered_recently_honours_window_and_reasons(tmp_path):
    svc = SetupModeService(str(tmp_path / "s.json"))
    assert svc.entered_recently(600) is False
    svc.enter_setup_mode("button_hold")
    t0 = svc.entered_at()
    assert svc.entered_recently(600, PHYSICAL_REASONS, now=t0 + 10) is True
    assert svc.entered_recently(600, PHYSICAL_REASONS, now=t0 + 601) is False
    assert svc.entered_recently(600, ("auto_boot",), now=t0 + 10) is False
    assert svc.entered_recently(600, PHYSICAL_REASONS, now=t0 - 5) is False   # clock went backwards
    svc.enter_setup_mode("manual_trigger")        # from a browser: not presence
    t1 = svc.entered_at()
    assert svc.entered_recently(600, PHYSICAL_REASONS, now=t1 + 1) is False
    assert svc.entered_recently(600, now=t1 + 1) is True
    svc.exit_setup_mode()
    assert svc.entered_recently(600) is False and svc.entered_at() is None
    assert svc.reason() == "normal"


def test_corrupt_and_legacy_state_files(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert SetupModeService(str(path)).is_setup_mode() is False
    path.write_text("[1, 2]")
    assert SetupModeService(str(path)).is_setup_mode() is False
    # Pre-timestamp format: setup mode is honoured but can never count as
    # recent presence because its age is unknown.
    path.write_text(json.dumps({"setup_mode": True, "reason": "manual_trigger"}))
    svc = SetupModeService(str(path))
    assert svc.is_setup_mode() is True and svc.entered_at() is None
    assert svc.entered_recently(600) is False
