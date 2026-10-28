"""Controls inside iframes, cross-origin ones included, are reported and reachable.

Plaid Link (2026-10-27): Square's invoice page opens it as an iframe at
https://cdn.plaid.com, and the bank's sign-in fields are inside it. The
snapshot walked the top-level document only, so it listed none of them, and
every selector tool searched the top-level document, so none could reach
them. A credential filler could not log in at all.

`frame` names an iframe by the selectors of each nested iframe, outermost
first, exactly as the snapshot now reports it on each element. The origin
lock (`expect_origin`) is the FIELD's document's origin, which inside a frame
is the frame's: these tests hold that a fill locked to the top page's origin
writes nothing into a cross-origin frame, and one locked to the frame's lands.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import socket
import threading

import pytest

from invisible_playwright_mcp.mcp import actions, masked, server


# --- the argument -------------------------------------------------------------

def test_no_frame_is_the_top_level_document():
    assert actions.frame_chain(None) == []
    assert actions.frame_chain([]) == []


@pytest.mark.parametrize("bad", ["#plaid", [""], ["  "], [1], ["#a"] * (actions.FRAME_DEPTH + 1), {"a": 1}])
def test_a_malformed_frame_is_refused_before_the_page_is_asked(bad):
    with pytest.raises(ValueError):
        actions.frame_chain(bad)


def test_every_selector_tool_declares_it():
    for tool in (server.browser_type, server.browser_click, server.browser_select_option,
                 server.browser_evaluate, server.browser_read_html):
        params = inspect.signature(tool).parameters
        assert "frame" in params and params["frame"].default is None, tool.__name__


class _Element:
    def __init__(self, tag, frame):
        self.tag, self.frame, self.disposed = tag, frame, False

    async def evaluate(self, expression):
        return self.tag

    async def content_frame(self):
        return self.frame

    async def dispose(self):
        self.disposed = True


class _Frame:
    def __init__(self, matches):
        self.matches = matches

    async def query_selector_all(self, selector):
        return self.matches.get(selector, [])


class _Page:
    def __init__(self, main):
        self.main_frame = main


class _Session:
    def __init__(self, page):
        self._page = page

    def page(self):
        return self._page


def test_each_step_must_be_exactly_one_iframe():
    inner = _Frame({})
    one = _Element("IFRAME", inner)
    two = [_Element("IFRAME", inner), _Element("IFRAME", inner)]
    div = _Element("DIV", None)
    blank = _Element("IFRAME", None)
    top = _Frame({"#plaid": [one], ".many": two, "#div": [div], "#blank": [blank]})
    session = _Session(_Page(top))
    assert asyncio.run(actions.frame_target(session, ["#plaid"])) is inner
    assert one.disposed
    for chain, said in ((["#none"], "matches 0"), ([".many"], "matches 2"),
                        (["#div"], "not an <iframe>"), (["#blank"], "no document")):
        with pytest.raises(RuntimeError, match=said):
            asyncio.run(actions.frame_target(session, chain))
    assert all(e.disposed for e in two) and div.disposed and blank.disposed
    assert asyncio.run(actions.frame_target(session, None)) is session._page


def test_the_screenshot_guard_reads_every_frame():
    """PRESENCE_JS skips a cross-origin iframe from its parent (SecurityError),
    so the guard must read each frame itself: a value masked only inside the
    frame refuses the screenshot."""
    class Frame:
        def __init__(self, values):
            self.values = values

        async def eval_on_selector(self, selector, expression):
            return self.values

    class Page:
        frames = [Frame(["nothing here"]), Frame(["Pw-0123456789-secret"])]

    class Session:
        def page(self):
            return Page()

    session = Session()
    masked.registry(session).register("Pw-0123456789-secret")
    with pytest.raises(masked.MaskRefusal):
        asyncio.run(masked.guard_pixels(session))
    Page.frames = [Frame(["nothing here"]), Frame(["still nothing"])]
    asyncio.run(masked.guard_pixels(session))


def test_a_frame_that_cannot_be_read_refuses_the_screenshot():
    class Broken:
        async def eval_on_selector(self, selector, expression):
            raise RuntimeError("frame detached")

    class Page:
        frames = [Broken()]

    class Session:
        def page(self):
            return Page()

    session = Session()
    masked.registry(session).register("Pw-0123456789-secret")
    with pytest.raises(masked.MaskRefusal):
        asyncio.run(masked.guard_pixels(session))


# --- a real browser -----------------------------------------------------------

FRAME = b"""<!doctype html><html><head><title>bank sign-in</title></head><body>
<input id="email" name="email" type="email">
<input id="password" name="password" type="password">
<input id="token" type="text">
<button id="go" type="button" onclick="document.title='clicked in frame'">Go</button>
</body></html>"""


def _parent(frame_url: str) -> bytes:
    return ("""<!doctype html><html><head><title>invoice</title></head><body>
<input id="top-password" name="top-password" type="password">
<iframe id="link" title="Link" src="%s" style="position:absolute;left:40px;top:120px;width:600px;height:400px;border:3px solid #000"></iframe>
<iframe title="hidden ad" src="%s" style="display:none"></iframe>
</body></html>""" % (frame_url, frame_url)).encode()


def _serve(body_for):
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = body_for()
            self.send_response(200)
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
    return srv, port


@pytest.fixture(scope="module")
def run():
    """A parent on 127.0.0.1 embedding a frame on localhost: two origins."""
    from invisible_playwright_mcp.mcp.plan import plan_session
    from invisible_playwright_mcp.mcp.session import StealthSession

    frame_srv, frame_port = _serve(lambda: FRAME)
    frame_url = "http://localhost:%d/" % frame_port
    parent_srv, parent_port = _serve(lambda: _parent(frame_url))
    url = "http://127.0.0.1:%d/" % parent_port
    loop = asyncio.new_event_loop()
    session = StealthSession(**plan_session().kwargs)
    loop.run_until_complete(session.start())

    def go(fn):
        async def body():
            await actions.navigate(session, url, wait_until="load")
            return await fn(session)
        return loop.run_until_complete(body())

    go.frame_origin = "http://localhost:%d" % frame_port
    go.top_origin = "http://127.0.0.1:%d" % parent_port
    try:
        yield go
    finally:
        loop.run_until_complete(session.close())
        loop.close()
        frame_srv.shutdown()
        parent_srv.shutdown()


@pytest.mark.e2e
def test_the_snapshot_lists_controls_inside_a_cross_origin_frame(run):
    snap = run(lambda s: actions.snapshot(s))
    d = json.loads(snap)
    framed = {e.get("selector"): e for e in d["interactive_elements"] if e.get("frame")}
    for wanted in ("#email", "#password", "#go"):
        assert wanted in framed, sorted(map(str, framed))
        assert framed[wanted]["frame"] == ["#link"]
        assert framed[wanted]["frame_origin"] == run.frame_origin
    top = [e for e in d["interactive_elements"] if not e.get("frame")]
    assert [e.get("selector") for e in top] == ["#top-password"]
    # The hidden frame is not reported; the shown one is, by origin only.
    assert d["frames"] == [{"origin": run.frame_origin, "frame": ["#link"]}]
    # `at` is in the top viewport (frame box at 40+3, 120+3), `frame_at` in the frame's.
    go = framed["#go"]
    assert go["at"] == [go["frame_at"][0] + 43, go["frame_at"][1] + 123]


@pytest.mark.e2e
def test_a_fill_locked_to_the_top_origin_writes_nothing_in_the_frame(run):
    async def body(s):
        with pytest.raises(RuntimeError, match="nothing was written"):
            await actions.type_text(s, "#password", "not-for-this-origin", run.top_origin,
                                    "password", frame=["#link"])
        frame = await actions.frame_target(s, ["#link"])
        return await frame.evaluate("() => document.querySelector('#password').value")
    assert run(body) == ""


@pytest.mark.e2e
def test_a_fill_locked_to_the_frame_origin_lands_in_the_frame(run):
    async def body(s):
        out = await actions.type_text(s, "#password", "frame-password-1", run.frame_origin,
                                      "password", frame=["#link"])
        frame = await actions.frame_target(s, ["#link"])
        inside = await frame.evaluate("() => document.querySelector('#password').value")
        top = await s.page().evaluate("() => document.querySelector('#top-password').value")
        return out, inside, top
    out, inside, top = run(body)
    assert out == "typed into #password"
    assert inside == "frame-password-1" and top == ""


@pytest.mark.e2e
def test_the_frame_s_readers_mask_its_password(run):
    async def body(s):
        await actions.type_text(s, "#password", "frame-password-2", run.frame_origin,
                                "password", frame=["#link"])
        return await actions.snapshot(s), await actions.read_html(s, "form", frame=["#link"])
    snap, html = run(body)
    assert "frame-password-2" not in snap and "frame-password-2" not in html
    pw = [e for e in json.loads(snap)["interactive_elements"]
          if e.get("frame") and e.get("selector") == "#password"][0]
    assert pw["text"] == "•" * 8


@pytest.mark.e2e
def test_a_masked_value_inside_the_frame_refuses_pixels(run):
    async def body(s):
        await actions.type_text(s, "#token", "masked-token-0123", run.frame_origin, "text",
                                mask_value=True, frame=["#link"])
        try:
            await actions.screenshot_png(s)
        except masked.MaskRefusal as refused:
            return str(refused)
        finally:
            masked.forget(s)
        return "captured"
    assert run(body) == masked.SCREENSHOT_REFUSED


@pytest.mark.e2e
def test_click_and_evaluate_reach_inside_the_frame(run):
    async def body(s):
        await actions.click(s, "#go", frame=["#link"])
        inside = json.loads(await actions.evaluate(s, "document.title", frame=["#link"]))
        outside = json.loads(await actions.evaluate(s, "document.title"))
        origin = json.loads(await actions.evaluate(s, "location.origin", frame=["#link"]))
        return inside, outside, origin
    inside, outside, origin = run(body)
    assert (inside, outside, origin) == ("clicked in frame", "invoice", run.frame_origin)
