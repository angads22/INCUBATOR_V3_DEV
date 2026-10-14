"""cloud_service must not pull httpx into the web app's cold start."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_importing_cloud_service_does_not_import_httpx():
    code = (
        "import sys\n"
        "import app.services.cloud_service as cs\n"
        "assert 'httpx' not in sys.modules, 'httpx imported eagerly'\n"
        "svc = cs.CloudService()\n"
        "assert svc.register_device('X')['ok'] is False\n"   # disabled by default: no network, no import
        "assert 'httpx' not in sys.modules\n"
        "print('lazy')\n"
    )
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "lazy" in proc.stdout


def test_cloud_calls_still_work_when_enabled(monkeypatch):
    import app.services.cloud_service as cs

    svc = cs.CloudService()
    svc._enabled, svc._api_base, svc._shared_secret = True, "http://127.0.0.1:9", "s"

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"registered": True}

    class _Httpx:
        class HTTPStatusError(Exception):
            pass

        @staticmethod
        def post(url, headers, json, timeout):
            return _Resp()

    monkeypatch.setattr(cs.CloudService, "_http", staticmethod(lambda: _Httpx))
    assert svc.register_device("X") == {"ok": True, "registered": True}
