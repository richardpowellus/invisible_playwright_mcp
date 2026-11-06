"""CAPTCHA solving uses the existing capture and human-input paths."""
from __future__ import annotations

import asyncio
import base64
import http.server
import os
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from invisible_playwright_mcp.mcp import actions, masked, owners, server


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    async def count(self):
        return self.page.counts.get(self.selector, 1)

    async def evaluate(self, expression, **kwargs):
        if expression == actions._CAPTCHA_CANDIDATES_JS:
            return self.page.candidates
        if expression == actions._CAPTCHA_IMAGE_JS:
            return dict(self.page.identity)
        if expression == actions.FIELD_STATE_JS:
            return {"value": self.page.value, "focused": True, "secret": False}
        raise AssertionError(expression)

    async def scroll_into_view_if_needed(self):
        self.page.events.append("scroll")

    async def bounding_box(self):
        return {"x": 12, "y": 34, "width": 120, "height": 40}


class Page:
    def __init__(self):
        self.events = []
        self.candidates = ["#captcha"]
        self.counts = {}
        self.identity = {"tag": "IMG", "connected": True, "src": "captcha.png",
                         "origin": 1, "url": "https://example.test"}
        self.value = ""
        self.keep = True
        self.frames = [self]

    def locator(self, selector):
        return Locator(self, selector)

    async def evaluate_read(self, expression):
        return {"x": 3, "y": 4}

    async def eval_on_selector(self, selector, expression):
        return [self.value]

    async def screenshot(self, **kwargs):
        self.events.append(("screenshot", kwargs))
        return b"png"

    async def fill(self, selector, text, **kwargs):
        self.events.append(("fill", selector, text))
        self.value = text if self.keep else ""

    async def click(self, selector, **kwargs):
        self.events.append(("click", selector))


class Session:
    def __init__(self):
        self.current = Page()

    def page(self):
        return self.current


@pytest.fixture
def solvers(monkeypatch):
    module = ModuleType("invisible_playwright.recaptcha")
    calls = []

    def image(png, *, module, case_sensitive):
        calls.append(("image", png, module, case_sensitive, threading.current_thread()))
        return "aBc123"

    async def recaptcha(page):
        calls.append(("recaptcha", page))
        return SimpleNamespace(fields_filled=1, callback_called="callback")

    module.solve_image_text = image
    module.solve_recaptcha_async = recaptcha
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("CAPSOLVER_API_KEY", "do-not-echo-this-key")
    return module, calls


async def test_image_capture_type_and_click_use_the_existing_paths(solvers):
    session = Session()
    answer = await actions.solve_captcha(
        session, input_selector="#input", submit_selector="#submit",
        module="common", case_sensitive=True)
    assert solvers[1] == [("image", b"png", "common", True, solvers[1][0][4])]
    assert solvers[1][0][4] is not threading.current_thread()
    assert session.current.events == [
        "scroll", ("screenshot", {"clip": {"x": 15, "y": 38, "width": 120, "height": 40}}),
        ("fill", "#input", "aBc123"), ("click", "#submit")]
    assert "aBc123" in answer and "typed into #input" in answer
    assert "submit clicked" in answer and "snapshot" in answer


async def test_ambiguous_auto_detection_refuses_without_capture_or_payment(solvers):
    session = Session()
    session.current.candidates = ["#one", "#two"]
    with pytest.raises(ValueError, match="candidates:.*#one.*#two"):
        await actions.solve_captcha(session, input_selector="#input")
    assert not session.current.events and not solvers[1]


async def test_missing_key_refuses_before_sending_or_acting(solvers, monkeypatch):
    monkeypatch.delenv("CAPSOLVER_API_KEY")
    session = Session()
    with pytest.raises(ValueError, match="not provisioned to the server"):
        await actions.solve_captcha(session, input_selector="#input")
    assert not session.current.events and not solvers[1]


async def test_recaptcha_uses_async_engine_and_does_not_expose_token(solvers):
    session = Session()
    answer = await actions.solve_captcha(session, kind="recaptcha")
    assert solvers[1] == [("recaptcha", session.current)]
    assert "1 response field" in answer and "callback called" in answer
    assert "snapshot" in answer and not session.current.events


async def test_masked_pixels_refuse_before_capture_and_payment(solvers):
    session = Session()
    masked.registry(session).register("secret-value")
    session.current.value = "secret-value"
    with pytest.raises(masked.MaskRefusal):
        await actions.solve_captcha(session, image_selector="#captcha", input_selector="#input")
    assert not any(isinstance(event, tuple) and event[0] == "screenshot"
                   for event in session.current.events)
    assert not solvers[1]


async def test_failed_readback_does_not_submit(solvers):
    session = Session()
    session.current.keep = False
    answer = await actions.solve_captcha(
        session, image_selector="#captcha", input_selector="#input", submit_selector="#submit")
    assert "Submit was not clicked" in answer
    assert not any(event == ("click", "#submit") for event in session.current.events)


async def test_navigation_during_solver_does_not_type(solvers):
    session = Session()
    loop = asyncio.get_running_loop()

    def changed(png, *, module, case_sensitive):
        loop.call_soon_threadsafe(session.current.identity.update, {"origin": 2})
        return "answer"

    solvers[0].solve_image_text = changed
    with pytest.raises(RuntimeError, match="changed during solving"):
        await actions.solve_captcha(session, image_selector="#captcha", input_selector="#input")
    assert not any(isinstance(event, tuple) and event[0] == "fill"
                   for event in session.current.events)


async def test_registration_dispatches_to_selected_browser(monkeypatch):
    class Work:
        async def acting(self, action, *args, role=None):
            return action, args, role

    monkeypatch.setattr(server, "owners", None)
    monkeypatch.setattr(server, "work", Work())
    action, args, role = await server.browser_solve_captcha(
        input_selector="#input", frame=["#frame"], browser="support")
    assert action is actions.solve_captcha and role == "support"
    assert args[-1] == ["#frame"]
    assert "browser_solve_captcha" not in owners.HANDLE_TOOLS


@pytest.mark.e2e
@pytest.mark.skipif(sys.platform not in ("linux", "win32"), reason="engine supports Linux and Windows")
@pytest.mark.parametrize("framed", [False, True])
async def test_real_image_is_cropped_and_typed_with_trusted_events(solvers, framed):
    from invisible_playwright_mcp.mcp.session import StealthSession

    challenge = b"""<!doctype html><form id="captcha-form">
        <img id="captcha-image" width="120" height="40"
             src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='40'%3E%3Crect width='120' height='40' fill='red'/%3E%3C/svg%3E">
        <input id="input"><button type="button" id="submit">Submit</button>
        </form><script>
        window.events = [];
        document.addEventListener('keydown', e => events.push(['key', e.isTrusted]));
        document.querySelector('#submit').addEventListener('click', e => events.push(['click', e.isTrusted]));
        </script>"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"<iframe id='frame' src='/challenge'></iframe>"
                    if framed and self.path != "/challenge" else challenge)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    options = {"headless": True}
    if os.environ.get("STEALTHFOX_BINARY"):
        options["binary_path"] = os.environ["STEALTHFOX_BINARY"]
    session = StealthSession(**options)
    try:
        await session.start()
        await actions.navigate(session, "http://127.0.0.1:%d/" % httpd.server_port, "load")
        frame = ["#frame"] if framed else None
        answer = await actions.solve_captcha(
            session, input_selector="#input", submit_selector="#submit", frame=frame)
        assert "submit clicked" in answer
        png = solvers[1][0][1]
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert int.from_bytes(png[16:20], "big") >= 120
        assert int.from_bytes(png[20:24], "big") >= 40
        pixel = await session.page().evaluate("""encoded => new Promise((resolve, reject) => {
            const image = new Image();
            image.onerror = reject;
            image.onload = () => {
                const canvas = document.createElement('canvas');
                canvas.width = image.width; canvas.height = image.height;
                const context = canvas.getContext('2d');
                context.drawImage(image, 0, 0);
                resolve([...context.getImageData(
                    Math.floor(image.width / 2), Math.floor(image.height / 2), 1, 1).data]);
            };
            image.src = 'data:image/png;base64,' + encoded;
        })""", base64.b64encode(png).decode())
        assert pixel[0] > 200 and pixel[1] < 50 and pixel[2] < 50
        target = await actions.frame_target(session, frame)
        assert await target.locator("#input").input_value() == "aBc123"
        events = await target.evaluate("() => window.events")
        assert any(kind == "key" and trusted for kind, trusted in events)
        assert ["click", True] in events
    finally:
        await session.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join()
