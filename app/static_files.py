"""Static-asset serving tuned for small boards and slow Wi-Fi.

``CachedStaticFiles`` lets browsers keep CSS/JS/fonts/icons instead of
re-downloading them on every page view: a request carrying a ``v`` query
parameter (templates render ``/static/css/x.css?v=<app version>``) is treated
as immutable for a year, because the URL changes whenever the version does;
anything else is cached for an hour.

``add_web_middleware`` adds gzip for text responses (HTML, CSS, JS, JSON)
above 1 KiB. Images, fonts and the camera stream are excluded by Starlette's
defaults, so a Pi Zero never spends CPU re-compressing a JPEG.

Both are wired into ``app.main`` (mount + middleware); this module itself has
no dependency on the rest of the app so it can be unit-tested in isolation.
"""

from __future__ import annotations

import os
from typing import Any

from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_SHORT = "public, max-age=3600"

# Level 6 is the classic sweet spot: ~95% of the size win of level 9 for a
# fraction of the CPU, which matters on a single slow ARM core.
GZIP_MINIMUM_SIZE = 1024
GZIP_LEVEL = 6


def _is_versioned(scope: Scope) -> bool:
    """True when the request URL carries a non-empty ``v`` query parameter."""
    raw = scope.get("query_string", b"") or b""
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1", "replace")
    for part in raw.split("&"):
        key, sep, value = part.partition("=")
        if key == "v" and sep and value:
            return True
    return False


class CachedStaticFiles(StaticFiles):
    """``StaticFiles`` that sets ``Cache-Control`` on every file it serves."""

    def file_response(
        self,
        full_path: "str | os.PathLike[str]",
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Any:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = CACHE_IMMUTABLE if _is_versioned(scope) else CACHE_SHORT
        return response


def add_web_middleware(app: Any) -> Any:
    """Attach the response middleware the web front-end relies on (gzip)."""
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE, compresslevel=GZIP_LEVEL)
    return app


__all__ = ["CachedStaticFiles", "add_web_middleware", "CACHE_IMMUTABLE", "CACHE_SHORT"]
