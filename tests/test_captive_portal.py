"""Captive-portal responder: OS probes are redirected to the onboarding page."""

import socket

import httpx
import pytest

from app.services.captive_portal import CaptivePortalResponder

PORTAL = "http://10.42.0.1:8000/onboarding"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def responder():
    r = CaptivePortalResponder(PORTAL, port=_free_port())
    assert r.start() is True
    yield r
    r.stop()
    assert r.is_running() is False


def _base(r: CaptivePortalResponder) -> str:
    return f"http://127.0.0.1:{r._port}"


def test_android_probe_redirects_to_portal(responder):
    resp = httpx.get(_base(responder) + "/generate_204", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == PORTAL


def test_catch_all_redirects_to_portal(responder):
    resp = httpx.get(_base(responder) + "/anything/else", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == PORTAL


def test_apple_cna_gets_non_success_page(responder):
    resp = httpx.get(_base(responder) + "/hotspot-detect.html", follow_redirects=False)
    assert resp.status_code == 200
    # Must NOT be Apple's "Success" body, or iOS won't open the captive sheet.
    assert "Success" not in resp.text
    assert PORTAL in resp.text
