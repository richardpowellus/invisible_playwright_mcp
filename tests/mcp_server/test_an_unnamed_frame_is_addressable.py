"""An iframe with no id or naming attribute still gets a `frame` selector.

Facebook's settings dialogs (2026-10-29) render a "Confirm Name Change
Request" form in a same-origin iframe that carries none of id, name, title,
data-testid or aria-label. The snapshot listed the frame as `unaddressable`
and none of its controls, so a credential filler, which confirms masking
against the snapshot's entry for the field, refused a password that
`browser_type(frame=["iframe"])` could reach.

The snapshot now falls back to the bare tag when it is unique, and otherwise
to a `tag:nth-of-type(n)` path, each checked unique in the parent's light DOM.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading

import pytest

from invisible_playwright_mcp.mcp import actions

FRAME = b"""<!doctype html><html><head><title>confirm</title></head><body>
<input id="save_password" name="save_password" type="password">
</body></html>"""

#: One unnamed frame: the bare tag is unique.
ONE = b"""<!doctype html><html><head><title>settings</title></head><body>
<div><iframe src="/frame" style="width:400px;height:200px"></iframe></div>
</body></html>"""

#: Two unnamed frames under an id'd ancestor, and a hidden third.
TWO = b"""<!doctype html><html><head><title>settings</title></head><body>
<div id="dialogs"><div><iframe src="/frame" style="width:400px;height:120px"></iframe></div>
<div><iframe src="/frame" style="width:400px;height:120px"></iframe></div></div>
<iframe src="/frame" style="display:none"></iframe>
</body></html>"""

PAGES = {"/one": ONE, "/two": TWO, "/frame": FRAME}


@pytest.fixture(scope="module")
def run():
    from invisible_playwright_mcp.mcp.plan import plan_session
    from invisible_playwright_mcp.mcp.session import StealthSession

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path.split("?")[0], b"")
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    loop = asyncio.new_event_loop()
    session = StealthSession(**plan_session().kwargs)
    loop.run_until_complete(session.start())

    def go(path, fn):
        async def body():
            await actions.navigate(session, "http://127.0.0.1:%d%s" % (port, path), wait_until="load")
            return await fn(session)
        return loop.run_until_complete(body())

    go.origin = "http://127.0.0.1:%d" % port
    try:
        yield go
    finally:
        loop.run_until_complete(session.close())
        loop.close()
        srv.shutdown()


def _framed(snap):
    d = json.loads(snap)
    return d, [e for e in d["interactive_elements"] if e.get("frame")]


@pytest.mark.e2e
def test_a_lone_unnamed_frame_is_named_by_its_tag(run):
    d, framed = _framed(run("/one", actions.snapshot))
    assert d["frames"] == [{"origin": run.origin, "frame": ["iframe"]}]
    assert [(e["selector"], e["frame"]) for e in framed] == [("#save_password", ["iframe"])]


@pytest.mark.e2e
def test_sibling_unnamed_frames_are_named_by_path(run):
    d, framed = _framed(run("/two", actions.snapshot))
    chains = [f["frame"] for f in d["frames"]]
    assert all("unaddressable" not in f for f in d["frames"]), d["frames"]
    assert chains == [["#dialogs > div:nth-of-type(1) > iframe:nth-of-type(1)"],
                      ["#dialogs > div:nth-of-type(2) > iframe:nth-of-type(1)"]]
    assert [e["frame"] for e in framed] == chains


@pytest.mark.e2e
def test_the_reported_chain_reaches_that_frame_and_masks_its_password(run):
    chain = ["#dialogs > div:nth-of-type(2) > iframe:nth-of-type(1)"]

    async def body(s):
        out = await actions.type_text(s, "#save_password", "unnamed-frame-pw", run.origin,
                                      "password", frame=chain)
        first = await actions.frame_target(s, ["#dialogs > div:nth-of-type(1) > iframe:nth-of-type(1)"])
        second = await actions.frame_target(s, chain)
        values = [await f.evaluate("() => document.querySelector('#save_password').value")
                  for f in (first, second)]
        return out, values, await actions.snapshot(s)
    out, values, snap = run("/two", body)
    assert out == "typed into #save_password"
    assert values == ["", "unnamed-frame-pw"]
    assert "unnamed-frame-pw" not in snap
    pw = [e for e in json.loads(snap)["interactive_elements"] if e.get("frame") == chain]
    assert [e["text"] for e in pw] == ["•" * 8]
