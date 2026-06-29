"""Captive-portal responder for first-boot onboarding.

While the setup hotspot is up, phones/laptops fire OS "is there internet?"
probes at well-known URLs. Answering them so the device shows a *sign in to
network* notification — and opens our onboarding page in the captive sheet —
removes the need to type http://10.42.0.1:8000 by hand.

This binds :80 (the real app stays on :8000) and is started alongside the
hotspot and torn down when onboarding completes. It is best-effort: if it
can't bind (e.g. not root, or port busy on a dev box) it logs and stays out of
the way rather than breaking onboarding.

Probe behaviour:
  * Android  /generate_204, /gen_204            → 302 to the portal (not 204)
  * Apple    /hotspot-detect.html               → a non-"Success" page → CNA opens
  * Windows  /connecttest.txt, /ncsi.txt, /redirect → 302 to the portal
  * anything else (any host/path)               → 302 to the portal
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class CaptivePortalResponder:
    def __init__(self, portal_url: str, port: int = 80) -> None:
        self._portal_url = portal_url
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self._server is not None:
            return True
        portal_url = self._portal_url

        class Handler(BaseHTTPRequestHandler):
            # Silence the default stderr request logging.
            def log_message(self, *args) -> None:  # noqa: D401, ANN002
                pass

            def _redirect(self) -> None:
                self.send_response(302)
                self.send_header("Location", portal_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _apple_cna(self) -> None:
                # iOS/macOS open the captive sheet when the body is NOT Apple's
                # expected "Success" page. A meta-refresh sends them onward.
                body = (
                    "<!DOCTYPE html><html><head>"
                    f'<meta http-equiv="refresh" content="0;url={portal_url}">'
                    "<title>Incubator setup</title></head><body>"
                    f'<a href="{portal_url}">Set up your incubator</a>'
                    "</body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in ("/hotspot-detect.html", "/library/test/success.html"):
                    self._apple_cna()
                else:
                    self._redirect()

            do_POST = do_GET  # noqa: N815

        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", self._port), Handler)
        except OSError as exc:
            logger.warning("Captive portal responder could not bind :%d: %s", self._port, exc)
            self._server = None
            return False

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Captive portal responder on :%d → %s", self._port, portal_url)
        return True

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("captive responder stop error: %s", exc)
        finally:
            self._server = None
            self._thread = None

    def is_running(self) -> bool:
        return self._server is not None
