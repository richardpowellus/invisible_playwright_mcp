"""A WebAuthn request is refused at once; it never waits on a security key.

2026-11-25: Meta Business Suite's "confirm it's you" modal calls
navigator.credentials.get() in an iframe. Firefox answered with its native
security-key doorhanger, which is browser chrome no tool here can see or
dismiss, so the promise never settled, the modal's button spun forever and
Meta never offered its other methods. The e2e test below stays pending past
its deadline without `webauthn_prefs`.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading

import pytest

from invisible_playwright_mcp.mcp import actions, server
from invisible_playwright_mcp.mcp.session import StealthSession, webauthn_prefs
from invisible_playwright_mcp.mcp.work import Work


def test_no_authenticator_transport_is_enabled():
    prefs = webauthn_prefs()
    assert prefs["security.webauth.webauthn_enable_usbtoken"] is False
    # The soft token is Mozilla's test authenticator: it would answer, not refuse.
    assert prefs["security.webauth.webauthn_enable_softtoken"] is False
    # The API itself stays on, as in stock Firefox.
    assert "security.webauth.webauthn" not in prefs


PAGE = b"""<!doctype html><title>Passkey</title><h1>Confirm it's you</h1>"""

# Each settles within 5 s or reports that it is still pending.
CEREMONIES = """async () => {
  const race = p => Promise.race([
    p.then(() => 'resolved', e => e.name),
    new Promise(r => setTimeout(() => r('pending'), 5000))]);
  const challenge = new Uint8Array(32);
  return JSON.stringify({
    api: typeof PublicKeyCredential,
    get: await race(navigator.credentials.get({publicKey: {
      challenge, rpId: 'localhost', userVerification: 'preferred'}})),
    create: await race(navigator.credentials.create({publicKey: {
      challenge, rp: {id: 'localhost', name: 'test'},
      user: {id: new Uint8Array(16), name: 'u', displayName: 'u'},
      pubKeyCredParams: [{type: 'public-key', alg: -7}]}})),
  });
}"""


@pytest.fixture
def passkey_site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *_args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # localhost, not 127.0.0.1: it is a secure context AND a valid RP ID.
        yield "http://localhost:%d/" % httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.e2e
@pytest.mark.skipif(sys.platform not in ("linux", "win32"),
                    reason="the engine supports Linux and Windows, not darwin")
async def test_a_passkey_request_is_refused_and_the_page_can_fall_back(
        passkey_site, tmp_path, monkeypatch):
    options = {"headless": True}
    if os.environ.get("STEALTHFOX_BINARY"):
        options["binary_path"] = os.environ["STEALTHFOX_BINARY"]
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(tmp_path))
    session = StealthSession(**options)
    work = Work("webauthn-e2e")
    monkeypatch.setattr(server, "owners", None)
    monkeypatch.setattr(server, "work", work)
    await session.start()
    work._open["main"] = session
    try:
        async with asyncio.timeout(120):
            await server.browser_navigate(passkey_site)
            result = json.loads(await session.page().evaluate(CEREMONIES))
            # Before webauthn_prefs both stayed "pending": the USB token
            # manager waited for a key behind a doorhanger nobody could see.
            assert result == {"api": "function", "get": "NotAllowedError",
                              "create": "NotAllowedError"}, result
    finally:
        await session.close()
