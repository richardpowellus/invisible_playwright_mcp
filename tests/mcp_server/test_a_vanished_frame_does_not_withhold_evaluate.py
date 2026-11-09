"""A frame that is removed while browser_evaluate checks it does not withhold.

⛔ 2026-11-09: billpay's first page read after a fresh QBO sign-in failed three
times with "browser_evaluate answer withheld: the page's password fields could
not be checked against it". The check reads every frame's secret fields, and a
frame removed between ``page.frames`` and its read does not always fail:
measured against the 2026-11-08 build on a page that adds and removes iframes,
18 of 32 such reads raised TargetClosedError at once and 14 HUNG until the
check's 5 s budget ran out. One hung read withheld the whole answer, every time.

The claims:
  * a detached frame is skipped, whether its read hangs or fails;
  * the other frames are still read, and their secrets still taken out;
  * a frame that is still attached and fails, or a page that does not answer
    in time, still withholds - and the refusal names the frame's origin and
    what happened, never its path or the engine's message;
  * on a real page that churns iframes, evaluate answers.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading

import pytest

from invisible_playwright_mcp.mcp import actions, clean

PASSWORD = "correct-horse-battery-9"


class Frame:
    def __init__(self, url, read, detach_after=None):
        self.url, self._read, self._detached = url, read, False
        self.detach_after = detach_after

    def is_detached(self):
        return self._detached

    async def eval_on_selector(self, selector, script):
        assert selector == "html" and script is actions.SECRET_VALUES_JS
        if self.detach_after is not None:
            asyncio.get_running_loop().call_later(self.detach_after, self._gone)
        return await self._read()

    def _gone(self):
        self._detached = True


class Session:
    def __init__(self, *frames):
        self.frames = list(frames)

    def page(self):
        return self


async def _hang():
    await asyncio.Event().wait()


def _values(*values):
    async def read():
        return list(values)
    return read


def _raises(error):
    async def read():
        raise error
    return read


def _check(session, answer):
    return asyncio.run(actions._without_secret_values(session, answer))


def test_a_frame_that_detaches_while_its_read_hangs_is_skipped():
    session = Session(Frame("https://qbo.intuit.com/app/bills", _values(PASSWORD)),
                      Frame("https://gone.example/x?token=1", _hang, detach_after=0.2))
    out = _check(session, json.dumps(["pw", PASSWORD]))
    assert PASSWORD not in out and clean.MASKED_PASSWORD in out


def test_a_frame_that_detaches_and_fails_is_skipped():
    frame = Frame("https://gone.example/", _raises(RuntimeError("Target closed")))
    frame._detached = True
    out = _check(Session(Frame("https://a.example/", _values()), frame), '"ok"')
    assert out == '"ok"'


def test_an_attached_frame_that_fails_still_withholds_and_says_which(monkeypatch):
    session = Session(Frame("https://a.example/", _values()),
                      Frame("https://risk.example/path?secret=x",
                            _raises(RuntimeError("page said " + PASSWORD))))
    with pytest.raises(ValueError) as refused:
        _check(session, '"ok"')
    message = str(refused.value)
    assert message.startswith(actions.EVALUATE_WITHHELD)
    assert "https://risk.example" in message and "RuntimeError" in message
    assert "/path" not in message and "secret" not in message and PASSWORD not in message


def test_a_page_that_does_not_answer_in_time_still_withholds(monkeypatch):
    monkeypatch.setattr(actions, "SECRET_CHECK_SECONDS", 0.3)
    session = Session(Frame("https://qbo.intuit.com/app/bills", _hang))
    with pytest.raises(ValueError) as refused:
        _check(session, '"ok"')
    assert str(refused.value) == (actions.EVALUATE_WITHHELD
                                  + ": https://qbo.intuit.com did not answer within 0.3 s")


def test_an_unexpected_shape_still_withholds():
    with pytest.raises(ValueError, match="unexpected shape"):
        _check(Session(Frame("https://a.example/", _values(1))), '"ok"')


CHURN = b"""<!doctype html><html><body><input type=password id=pw>
<script>setInterval(() => { const f = document.createElement('iframe');
  f.src = '/child'; document.body.appendChild(f); setTimeout(() => f.remove(), 15); }, 5);
</script></body></html>"""


@pytest.mark.e2e
def test_evaluate_answers_on_a_page_that_churns_iframes():
    """Known-bad on the 2026-11-08 build: 15 of 15 answers withheld."""
    from invisible_playwright_mcp.mcp.plan import plan_session
    from invisible_playwright_mcp.mcp.session import StealthSession

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = CHURN if self.path == "/" else b"<!doctype html><p>child</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # the frame was removed before its body arrived

        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def run():
        session = StealthSession(**plan_session().kwargs)
        try:
            await session.start()
            await actions.navigate(session, "http://127.0.0.1:%d/" % port)
            await actions.type_text(session, "#pw", PASSWORD)
            answers = []
            for _ in range(10):
                answers.append(await actions.evaluate(
                    session, "() => document.querySelector('#pw').value"))
            return answers
        finally:
            await session.close()

    try:
        answers = asyncio.run(run())
    finally:
        srv.shutdown()
    assert len(answers) == 10
    assert all(PASSWORD not in a and clean.MASKED_PASSWORD in a for a in answers)
