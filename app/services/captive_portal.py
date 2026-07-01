"""Captive-portal responder for first-boot onboarding.

While the setup hotspot is up, phones/laptops fire OS "is there internet?"
probes at well-known URLs. Answering them non-affirmatively makes the device
show a *sign in to network* notification (the captive sheet / CNA).

The CNA is a RESTRICTED webview: it reloads/closes whenever the OS re-checks
connectivity, which wipes form state — so we must NOT run the multi-step signup
inside it. Every probe is therefore answered with one minimal LANDING page whose
only job is to hand off to the REAL browser: "Open setup in your browser" → the
app's /onboarding on :8000, where the Wi-Fi + account wizard runs with durable
state.

This binds :80 (the real app stays on :8000), starts alongside the hotspot, and
is torn down when onboarding completes. Best-effort: if it can't bind (not root,
or port busy on a dev box) it logs and stays out of the way.

Probe behaviour (all return the launcher page, never the wizard itself):
  * Android  /generate_204, /gen_204     → 200 launcher (non-204 → CNA opens)
  * Apple    /hotspot-detect.html        → 200 launcher (non-"Success" → CNA opens)
  * Windows  /connecttest.txt, /ncsi.txt → 200 launcher
  * anything else (any host/path)        → 200 launcher
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


def _landing_page(portal_url: str) -> bytes:
    """A minimal captive-sheet launcher: one button into the real browser."""
    return (
        "<!DOCTYPE html><html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Set up your incubator</title>"
        "<style>"
        "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1512;"
        "color:#eef;margin:0;padding:28px;line-height:1.5}"
        ".card{max-width:440px;margin:0 auto;background:#16201b;border:1px solid #2b8;"
        "border-radius:16px;padding:24px}"
        "h1{font-size:1.35rem;margin:0 0 6px}"
        "a.btn{display:block;text-align:center;background:#2b8;color:#031;font-weight:700;"
        "text-decoration:none;padding:14px;border-radius:10px;margin:18px 0;font-size:1.05rem}"
        ".muted{color:#9cb;font-size:.95rem}"
        "code{background:#0c120f;padding:2px 6px;border-radius:6px}"
        "</style></head><body><div class=\"card\">"
        "<h1>🐣 Set up your incubator</h1>"
        "<p class=\"muted\">This little pop-up can't run the full setup. "
        "Tap below to open it in your normal browser (Safari or Chrome).</p>"
        f"<a class=\"btn\" href=\"{portal_url}\">Open setup in your browser</a>"
        "<p class=\"muted\"><b>iPhone:</b> if it stays here, tap <b>Cancel</b> "
        "(top-left) → <b>Use Without Internet</b>, then open <b>Safari</b> and go to "
        f"<code>{portal_url}</code>.<br><b>Android:</b> tap the <b>“Sign in to "
        "network”</b> notification, then <b>Open in browser</b>.</p>"
        "</div></body></html>"
    ).encode()


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

        landing_html = _landing_page(portal_url)

        class Handler(BaseHTTPRequestHandler):
            # Silence the default stderr request logging.
            def log_message(self, *args) -> None:  # noqa: D401, ANN002
                pass

            def _launcher(self) -> None:
                # Serve the SAME minimal launcher for every probe. It is a
                # non-204, non-"Success" body, so every OS opens its captive
                # sheet — but the sheet only shows a button to the real browser,
                # NOT the multi-step wizard. Deliberately NOT a redirect: a 302
                # into :8000 would run the whole signup inside the CNA.
                body = landing_html
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._launcher()

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
