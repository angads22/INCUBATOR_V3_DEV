"""OTA apply must be provable: a failed fetch/checkout raises, nothing is
restarted, and .git-ref is never advanced for code that is not on disk."""

import os
import subprocess

import pytest

from app.ota.updater import OtaUpdater
from tests.test_ota_rollback import _Fakes


def _git(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


def _make_repos(tmp_path):
    """origin (with tag img-1.41-x on a second commit) + a shallow install clone on the first."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.check_call(["git", "-C", str(origin), "init", "-q", "-b", "main"], env=env)
    (origin / "VERSION").write_text("1.40")
    subprocess.check_call(["git", "-C", str(origin), "add", "."], env=env)
    subprocess.check_call(["git", "-C", str(origin), "commit", "-q", "-m", "1.40"], env=env)
    install = tmp_path / "install"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(install)], env=env)
    (origin / "VERSION").write_text("1.41")
    subprocess.check_call(["git", "-C", str(origin), "commit", "-q", "-am", "1.41"], env=env)
    subprocess.check_call(["git", "-C", str(origin), "tag", "img-1.41-20260701"], env=env)
    return origin, install


def test_apply_ref_checks_out_tag_and_returns_verified_head(tmp_path):
    from app.ota.__main__ import _apply_ref

    origin, install = _make_repos(tmp_path)
    before = _git(install, "rev-parse", "HEAD")
    head = _apply_ref("img-1.41-20260701", str(install), pip=False)
    assert head == _git(install, "rev-parse", "HEAD") != before
    assert head == _git(origin, "rev-parse", "img-1.41-20260701")
    assert (install / "VERSION").read_text() == "1.41"


def test_apply_ref_rollback_to_local_sha_needs_no_fetch(tmp_path):
    from app.ota.__main__ import _apply_ref

    origin, install = _make_repos(tmp_path)
    prev = _git(install, "rev-parse", "HEAD")
    _apply_ref("img-1.41-20260701", str(install), pip=False)
    # Break the remote: rollback must still work from the local object store.
    subprocess.check_call(["git", "-C", str(install), "remote", "set-url", "origin", str(tmp_path / "gone")])
    assert _apply_ref(prev, str(install), pip=False) == prev
    assert (install / "VERSION").read_text() == "1.40"


def test_apply_ref_raises_when_fetch_fails_and_head_is_unchanged(tmp_path):
    from app.ota.__main__ import OtaApplyError, _apply_ref

    origin, install = _make_repos(tmp_path)
    before = _git(install, "rev-parse", "HEAD")
    subprocess.check_call(["git", "-C", str(install), "remote", "set-url", "origin", str(tmp_path / "gone")])
    with pytest.raises(OtaApplyError, match="fetch"):
        _apply_ref("img-1.41-20260701", str(install), pip=False)
    assert _git(install, "rev-parse", "HEAD") == before
    assert (install / "VERSION").read_text() == "1.40"


def test_apply_ref_raises_for_unknown_tag(tmp_path):
    from app.ota.__main__ import OtaApplyError, _apply_ref

    origin, install = _make_repos(tmp_path)
    with pytest.raises(OtaApplyError):
        _apply_ref("img-9.99-nope", str(install), pip=False)


# ── orchestration: apply failure is not an update ────────────────────────────

class _FailingApply(_Fakes):
    def apply_ref(self, ref):
        raise RuntimeError("git fetch origin failed (exit 128)")


def test_apply_failure_is_reported_without_restart_or_rollback():
    f = _FailingApply("img-1.41-20260701", verify_results=[True])
    result = f.updater().run()
    assert result["updated"] is False and result["reason"] == "apply_failed"
    assert result["version"] == "1.41" and result["current"] == "1.40"
    assert "fetch" in result["error"]
    assert f.restarts == 0 and f.ref == "sha-CURRENT"
    assert "rolled_back" not in result


def test_main_does_not_write_git_ref_after_apply_failure(tmp_path, monkeypatch):
    import app.ota.__main__ as ota

    monkeypatch.setattr(ota, "INSTALL_DIR", str(tmp_path))
    (tmp_path / ".git-ref").write_text("1.40")
    monkeypatch.setattr(ota, "_get_latest", lambda: {"tag": "img-1.41-20260701", "ref": "img-1.41-20260701"})
    monkeypatch.setattr(ota, "_current_ref", lambda: "sha-old")
    restarts = []
    monkeypatch.setattr(ota, "_restart_web", lambda: restarts.append(1))
    monkeypatch.setattr(ota, "_verify", lambda: True)

    def failing(ref, *a, **kw):
        raise ota.OtaApplyError("git fetch failed")

    monkeypatch.setattr(ota, "_apply_ref", failing)
    assert ota.main() == 0
    assert (tmp_path / ".git-ref").read_text() == "1.40"     # NOT advanced
    assert restarts == []


def test_main_writes_git_ref_only_after_verified_update(tmp_path, monkeypatch):
    import app.ota.__main__ as ota

    monkeypatch.setattr(ota, "INSTALL_DIR", str(tmp_path))
    (tmp_path / ".git-ref").write_text("1.40")
    monkeypatch.setattr(ota, "_get_latest", lambda: {"tag": "img-1.41-20260701", "ref": "img-1.41-20260701"})
    monkeypatch.setattr(ota, "_current_ref", lambda: "sha-old")
    monkeypatch.setattr(ota, "_restart_web", lambda: None)
    monkeypatch.setattr(ota, "_verify", lambda: True)
    monkeypatch.setattr(ota, "_apply_ref", lambda ref, *a, **kw: "sha-new")
    assert ota.main() == 0
    assert (tmp_path / ".git-ref").read_text() == "1.41"


def test_rollback_apply_failure_reports_unrecovered():
    class _RollbackFails(_Fakes):
        def apply_ref(self, ref):
            if ref == "sha-CURRENT" and self.applied:
                raise RuntimeError("checkout failed")
            super().apply_ref(ref)

    f = _RollbackFails("img-1.41-20260701", verify_results=[False])
    result = f.updater().run()
    assert result["rolled_back"] is True and result["recovered"] is False
    assert f.restarts == 1                                     # no second restart on a failed rollback


def test_apply_ref_is_passed_as_single_arg_callable():
    import inspect

    from app.ota.__main__ import _apply_ref

    params = inspect.signature(_apply_ref).parameters
    assert list(params)[0] == "ref" and params["install_dir"].default is None
    OtaUpdater(current_version="1", get_latest=lambda: None, current_ref=lambda: "x",
               apply_ref=_apply_ref, verify=lambda: True, restart_web=lambda: None)
