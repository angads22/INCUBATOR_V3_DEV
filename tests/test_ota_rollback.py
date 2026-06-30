"""OTA updater: version logic + the forced-failure rollback demonstration."""

from app.ota.updater import OtaUpdater, extract_version, is_newer


def test_extract_version_from_release_tag():
    assert extract_version("img-1.40-20260628") == "1.40"
    assert extract_version("img-2.00-20270101") == "2.00"
    assert extract_version("nightly") is None


def test_is_newer():
    assert is_newer("1.41", "1.40") is True
    assert is_newer("2.00", "1.40") is True
    assert is_newer("1.40", "1.40") is False
    assert is_newer("1.39", "1.40") is False
    assert is_newer("garbage", "1.40") is False


class _Fakes:
    """Records side effects; `verify` is scripted per call."""

    def __init__(self, latest_tag, verify_results):
        self.latest_tag = latest_tag
        self._verify_results = list(verify_results)
        self.applied = []      # refs applied, in order
        self.restarts = 0
        self.ref = "sha-CURRENT"

    def get_latest(self):
        return {"tag": self.latest_tag, "ref": self.latest_tag} if self.latest_tag else None

    def current_ref(self):
        return self.ref

    def apply_ref(self, ref):
        self.applied.append(ref)
        self.ref = ref

    def restart_web(self):
        self.restarts += 1

    def verify(self):
        return self._verify_results.pop(0) if self._verify_results else False

    def updater(self, current="1.40"):
        return OtaUpdater(
            current_version=current,
            get_latest=self.get_latest,
            current_ref=self.current_ref,
            apply_ref=self.apply_ref,
            verify=self.verify,
            restart_web=self.restart_web,
            log=lambda *_: None,
        )


def test_healthy_update_is_applied():
    f = _Fakes("img-1.41-20260701", verify_results=[True])
    result = f.updater().run()
    assert result == {"updated": True, "version": "1.41", "ref": "img-1.41-20260701", "previous": "sha-CURRENT"}
    assert f.applied == ["img-1.41-20260701"]
    assert f.restarts == 1


def test_forced_failure_triggers_automatic_rollback():
    # New version comes up UNHEALTHY (first verify False), rollback then healthy.
    f = _Fakes("img-1.41-20260701", verify_results=[False, True])
    result = f.updater().run()

    assert result["rolled_back"] is True
    assert result["failed_version"] == "1.41"
    assert result["restored"] == "sha-CURRENT"
    assert result["recovered"] is True
    # Applied the new ref, then restored the previous one — no manual step.
    assert f.applied == ["img-1.41-20260701", "sha-CURRENT"]
    assert f.ref == "sha-CURRENT"      # ended back on last-known-good
    assert f.restarts == 2             # web restarted for update + for rollback


def test_no_newer_release_is_a_noop():
    f = _Fakes("img-1.40-20260628", verify_results=[])
    result = f.updater(current="1.40").run()
    assert result == {"update_available": False, "current": "1.40", "latest": "1.40"}
    assert f.applied == [] and f.restarts == 0


def test_offline_is_safe():
    f = _Fakes(None, verify_results=[])
    assert f.updater().run() == {"checked": False, "reason": "no_release_or_offline"}
    assert f.applied == []
