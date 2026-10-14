"""incubator-control.service runs ``python -m app.control`` — it must exist."""

import importlib
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_control_main_module_imports():
    mod = importlib.import_module("app.control.__main__")
    assert callable(mod.main)


def test_control_main_check_flag_returns_zero_without_side_effects(tmp_path, monkeypatch):
    from app.control import __main__ as entry

    assert entry.main(["--check"]) == 0


def test_python_m_app_control_check_runs():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.setdefault("GPIO_MOCK", "true")
    env["CONTROL_DAEMON_ENABLED"] = "false"
    proc = subprocess.run(
        [sys.executable, "-m", "app.control", "--check"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_service_unit_exec_matches_the_module():
    unit = (ROOT / "deploy" / "incubator-control.service").read_text()
    assert "-m app.control" in unit
    assert (ROOT / "app" / "control" / "__main__.py").exists()
