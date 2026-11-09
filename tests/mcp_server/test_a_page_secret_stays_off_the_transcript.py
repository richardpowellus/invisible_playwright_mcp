"""A secret the PAGE shows is redacted from every result by its shape.

⛔ 2026-11-08: Ramp's authenticator setup drawer shows the base32 key in a
``<code>`` and names its copy button ``Copy <key>``. browser_snapshot listed
the button with that name and a live TOTP seed reached the transcript.
``masked`` could not help: it masks only what this server typed.

The claims:
  * the Ramp shape - ``<code>SEED</code>`` and ``aria-label="Copy SEED"`` - is
    redacted from a real snapshot, read_text and read_html, and evaluate;
  * each secret shape is redacted wherever it stands in a string;
  * ordinary page text, selectors and coordinates pass untouched;
  * the redaction is applied at the one result boundary to every caller but a
    handle-authenticated (credential filler) call, which must still read it;
  * evaluate does not return a password box's value.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from invisible_playwright_mcp.mcp import actions, clean, masked
from invisible_playwright_mcp.mcp.secret_shapes import REDACTED, redact_secret_shapes
from test_owners import owners as owners  # noqa: F401 - the owner-mode fixture

#: Synthetic: the RFC 6238 example key's alphabet, never enrolled anywhere.
SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
GROUPED = "jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp"
PASSWORD = "correct-horse-battery-9"
FRAME_PASSWORD = "framed-horse-battery-8"


@pytest.mark.parametrize("text", [
    SEED,
    "Copy " + SEED,
    'aria-label="Copy %s"' % SEED,
    SEED.lower(),
    "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP",
    GROUPED,
    "JBSW-Y3DP-EHPK-3PXP",
    "otpauth://totp/Ramp:bot%40example.com?secret=" + SEED + "&issuer=Ramp",
    "otpauth%3A%2F%2Ftotp%2FRamp%3Fsecret%3D" + SEED,
    json.dumps("otpauth://totp/x?secret=" + SEED).replace("/", "\\/"),
    "otpauth-migration://offline?data=CjEKCkhlbGxvId6tvu8SBlJhbXA",
    "https://x.example/setup?secret=GEZDGNBV&x=1",
    SEED + "====",
])
def test_every_secret_shape_is_redacted(text):
    out = redact_secret_shapes(text)
    assert REDACTED in out
    for fragment in (SEED, SEED.lower(), "Y3DP", "y3dp", "GEZDGNBV", "CjEKCkhlbGxv"):
        assert fragment not in out, out


@pytest.mark.parametrize("text", [
    "Setup key\n  abcd efgh ijkl mnop",
    "Your recovery codes: 8f3k-2m9q, 1234-5678, a1b2c3d4",
    "Backup codes\n- 12345 67890\n- 23456 78901",
    "Secret key: hello-world-x9y8z7",
    "Client secret wqvz-82kd-pp01",
])
def test_a_code_beside_its_label_is_redacted(text):
    out = redact_secret_shapes(text)
    for fragment in ("ijkl", "2m9q", "1234-5678", "a1b2c3d4", "12345", "67890",
                     "x9y8z7", "82kd"):
        assert fragment not in out, out


@pytest.mark.parametrize("text", [
    "Scan the QR code with your authenticator app, then enter the 6-digit code.",
    'button "Continue" selector: "button:nth-match(.btn, 2)" at: [512, 340]',
    "INTERNATIONALIZATION and internationalization are long words",
    "CONGRATULATIONS ON YOUR NEW CARD",
    "Order #A1B2C3 total $1,234.56 on 2026-11-08",
    "sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "bh_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg",
    '{"origin":"https://app.ramp.com"}',
    "THIS PAGE HAS SOME TEXT HERE",
])
def test_ordinary_text_passes_untouched(text):
    assert redact_secret_shapes(text) == text


def test_the_boundary_redacts_unless_a_fill_handle_called():
    result = {"content": [{"type": "text", "text": "button \"Copy %s\"" % SEED}]}
    assert SEED not in json.dumps(masked.redact_result(result, []))
    assert SEED in json.dumps(masked.redact_result(result, [], shapes=False))


async def test_the_server_turns_shapes_off_only_for_a_resolved_handle(owners, monkeypatch):
    """Through the real result boundary in owner mode: the owner's own read is
    redacted; the same read made with its fill handle (the credential filler)
    is not; a forged handle is refused AND redacted."""
    from mcp.server.fastmcp.tools.base import Tool

    from invisible_playwright_mcp.mcp import server
    from invisible_playwright_mcp.mcp.owners import HANDLE_KEY
    from test_owners import call, handle, identity, success, text

    success(await call("A", "browser_open"))
    capability = handle(await call("A", "browser_status"))

    async def answer() -> str:
        return 'button "Copy %s"' % SEED

    tool = server.mcp._tool_manager.get_tool("browser_read_text")
    stub = Tool.from_function(answer, name=tool.name)
    for name in ("fn_metadata", "fn", "parameters"):
        monkeypatch.setattr(tool, name, getattr(stub, name))

    own = success(await call("A", "browser_read_text"))
    assert SEED not in own and REDACTED in own
    filler = success(await call("F", "browser_read_text",
                                meta={**identity("F"), HANDLE_KEY: capability}))
    assert SEED in filler
    forged = await call("F", "browser_read_text",
                        meta={**identity("F"), HANDLE_KEY: "bh_" + SEED + "A" * 11})
    assert forged.isError and SEED not in text(forged)


PAGE = ("""<!doctype html>
<html><head><title>Set up an authenticator app</title></head><body>
  <h2>Set up an authenticator app</h2>
  <p>Enter this setup key in your authenticator app</p>
  <code id="setup-key">%s</code>
  <button aria-label="Copy %s" title="Copy %s">Copy</button>
  <a href="otpauth://totp/Ramp:bot?secret=%s&amp;issuer=Ramp">Open in app</a>
  <label>Password <input id="pw" type="password"></label>
  <iframe id="inner" srcdoc="<input id='ipw' type='password'>"></iframe>
  <input id="code" placeholder="6-digit code">
  <button>Complete setup</button>
</body></html>""" % (SEED, SEED, SEED, SEED)).encode()


def _serve():
    import http.server
    import socket
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % port


@pytest.fixture(scope="module")
def reads():
    from invisible_playwright_mcp.mcp.plan import plan_session
    from invisible_playwright_mcp.mcp.session import StealthSession

    srv, url = _serve()

    async def run():
        session = StealthSession(**plan_session().kwargs)
        try:
            await session.start()
            await actions.navigate(session, url)
            await actions.type_text(session, "#pw", PASSWORD)
            await actions.type_text(session, "#ipw", FRAME_PASSWORD, frame=["#inner"])
            out = {
                "snapshot": await actions.snapshot(session),
                "read_text": await actions.read_text(session, "body"),
                "read_html": await actions.read_html(session, "full"),
                "evaluate": await actions.evaluate(
                    session, "() => [document.querySelector('#setup-key').textContent,"
                    " document.querySelector('#pw').value]"),
                # Script in the TOP page reading a same-origin iframe's field.
                "evaluate_frame": await actions.evaluate(
                    session, "() => document.querySelector('#inner')"
                    ".contentDocument.querySelector('#ipw').value"),
            }
            return out
        finally:
            await session.close()

    try:
        yield asyncio.run(run())
    finally:
        srv.shutdown()


@pytest.mark.e2e
@pytest.mark.parametrize("tool", ["snapshot", "read_text", "read_html", "evaluate"])
def test_the_ramp_setup_drawer_leaks_no_seed(reads, tool):
    """Known-bad: the raw readers print the seed; the boundary must not."""
    raw = reads[tool]
    assert SEED in raw or tool == "evaluate", "fixture no longer reproduces the leak"
    out = json.dumps(masked.redact_result({"text": raw}, []))
    assert SEED not in out and SEED[:16] not in out and SEED[16:] not in out


@pytest.mark.e2e
def test_evaluate_does_not_return_a_password_value(reads):
    assert PASSWORD not in reads["evaluate"]
    assert clean.MASKED_PASSWORD in reads["evaluate"]


def test_a_digitless_key_is_caught_by_its_length_or_its_label():
    """A random 32-character key has no 2-7 about one time in 750."""
    digitless = "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEF"
    assert digitless not in redact_secret_shapes("code " + digitless)
    assert "ABCDEFGHIJKLMNOP" not in redact_secret_shapes("Setup key: ABCDEFGHIJKLMNOP")


@pytest.mark.e2e
def test_evaluate_does_not_return_a_password_from_a_same_origin_frame(reads):
    """Known-bad: only the evaluated document's fields were read (MCP #858)."""
    assert FRAME_PASSWORD not in reads["evaluate_frame"]
    assert clean.MASKED_PASSWORD in reads["evaluate_frame"]
