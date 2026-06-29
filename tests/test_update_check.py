"""Update-check endpoint: offline-safe contract + version comparison."""

from app.routes import web


def test_update_check_is_offline_safe(client, monkeypatch):
    # Force the cache cold and any network call to fail — the endpoint must
    # still answer with a quiet, well-formed "no update" payload.
    web._update_cache["data"] = None
    web._update_cache["ts"] = 0.0

    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("no internet")

    monkeypatch.setattr(httpx, "get", boom)

    body = client.get("/api/update-check").json()
    assert body == {"update_available": False, "latest": None, "url": None, "notes": ""}


def test_is_newer_version_compares_mmm():
    assert web._is_newer_version("1.41", "1.40") is True
    assert web._is_newer_version("2.00", "1.40") is True
    assert web._is_newer_version("1.40", "1.40") is False
    assert web._is_newer_version("1.39", "1.40") is False
    # Garbage tags never claim an update.
    assert web._is_newer_version("nightly", "1.40") is False
