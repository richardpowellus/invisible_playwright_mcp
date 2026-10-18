"""browser_type with expect_origin hands the origin check to the engine.

A credential manager filling a login needs the value to land on the login's
own site and nowhere else. Checking the page first and typing afterwards leaves
a gap in which the page can navigate, and fill types a text field key by key,
so a navigation during typing sends the rest of the text to the next page.
The engine checks the field's own document origin in the same step as the
write; these tests hold that the server passes the request through, and that
a call without it is exactly the call it always was.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from invisible_playwright_mcp.mcp import actions, server
from test_open_first import work as work


class _Page:
    def __init__(self):
        self.calls = []

    async def wait_for_selector(self, selector, **kw):
        pass

    async def fill(self, selector, text, **kw):
        self.calls.append((selector, text, kw))

    async def eval_on_selector(self, selector, expression, arg=None, **kw):
        self.reads = getattr(self, "reads", []) + [kw]
        assert arg == {"origin": "https://login.example.com", "text": self.calls[-1][1]}
        return {"kept": True, "empty": not bool(self.calls[-1][1])}

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    async def evaluate(self, *args, **kwargs):
        return {"value": self.calls[-1][1] if self.calls else "", "focused": True, "secret": False}


class _Session:
    def __init__(self):
        self._page = _Page()

    def page(self):
        return self._page


def test_expect_origin_reaches_the_engine():
    s = _Session()
    out = asyncio.run(actions.type_text(s, "#p", "secret", "https://login.example.com"))
    assert out == "typed into #p"
    assert s._page.calls == [("#p", "secret", {"timeout": 15_000, "expect_origin": "https://login.example.com"})]


def test_without_it_the_fill_is_unchanged():
    s = _Session()
    asyncio.run(actions.type_text(s, "#q", "hello"))
    assert s._page.calls == [("#q", "hello", {"timeout": 15_000})]


def test_expect_input_type_rides_with_expect_origin():
    s = _Session()
    asyncio.run(actions.type_text(s, "#p", "secret", "https://login.example.com", "password"))
    assert s._page.calls[-1][2] == {"timeout": 15_000, "expect_origin": "https://login.example.com",
                                    "expect_input_type": "password"}
    with pytest.raises(ValueError):
        asyncio.run(actions.type_text(s, "#p", "secret", None, "password"))


def test_strict_reaches_the_write_and_the_read_back():
    # The engine resolves from a world where closed shadow roots are open, so a
    # caller that checked the field from page script asks for strict: a second
    # match, seen or not, refuses the write instead of taking the first.
    s = _Session()
    asyncio.run(actions.type_text(s, "#p", "secret", "https://login.example.com", "password",
                                  strict=True))
    assert s._page.calls[-1][2] == {"timeout": 15_000, "expect_origin": "https://login.example.com",
                                    "expect_input_type": "password", "strict": True}
    assert s._page.reads and all(kw == {"strict": True} for kw in s._page.reads)


def test_without_strict_the_guarded_write_is_unchanged():
    s = _Session()
    asyncio.run(actions.type_text(s, "#p", "secret", "https://login.example.com"))
    assert "strict" not in s._page.calls[-1][2]
    assert all(kw == {} for kw in s._page.reads)


def test_a_strict_violation_is_a_refusal_that_wrote_nothing():
    class Ambiguous(_Page):
        async def fill(self, selector, text, **kw):
            raise RuntimeError("strict mode violation: locator('#p') resolved to 2 elements: <input value=x>")

    s = _Session()
    s._page = Ambiguous()
    with pytest.raises(RuntimeError) as refused:
        asyncio.run(actions.type_text(s, "#p", "secret", "https://login.example.com", strict=True))
    assert "nothing was written" in str(refused.value) and "value=x" not in str(refused.value)
    # Without strict the same text is not taken as a refusal.
    s._page = Ambiguous()
    with pytest.raises(RuntimeError) as unknown:
        asyncio.run(actions.type_text(s, "#p", "secret", "https://login.example.com"))
    assert "outcome is unknown" in str(unknown.value)


def test_strict_needs_expect_origin():
    with pytest.raises(ValueError):
        asyncio.run(actions.type_text(_Session(), "#p", "secret", strict=True))
    assert "strict" in inspect.signature(server.browser_type).parameters


def test_the_tool_declares_it_as_optional():
    params = inspect.signature(server.browser_type).parameters
    assert "expect_origin" in params
    assert params["expect_origin"].default is None
    assert params["expect_input_type"].default is None



class _FailingPage:
    """A page whose fill fails with `message`, and which records any attempt
    to read it afterwards: the credential path must not diagnose by reading
    page text, which may by then hold the value."""

    def __init__(self, message):
        self.message = message
        self.read = False

    async def fill(self, selector, text, **kw):
        raise RuntimeError(self.message)

    async def evaluate(self, *a, **kw):
        self.read = True
        return {"covered_by": {"text": "hunter2"}}


class _FailingSession:
    def __init__(self, message):
        self._page = _FailingPage(message)

    def page(self):
        return self._page


def _fail(message):
    s = _FailingSession(message)
    with pytest.raises(RuntimeError) as err:
        asyncio.run(actions.type_text(s, "#p", "hunter2", "https://login.example.com"))
    return s._page, err.value


def test_a_refusal_says_nothing_was_written_in_one_fixed_sentence():
    page, err = _fail("fill expect_origin='https://login.example.com', actual origin='https://evil.example': "
                      "error:origin: origin mismatch or target detached/stale; nothing was written")
    assert str(err) == ("expect_origin refused: the field's page is not on the expected origin "
                        "or the field changed; nothing was written")
    assert err.__cause__ is None and err.__suppress_context__
    assert not page.read


def test_an_unknown_outcome_never_claims_nothing_was_written():
    page, err = _fail("fill expect_origin='https://login.example.com', actual origin='https://login.example.com': "
                      "TargetClosedError: target unavailable, detached or stale; value was written; trusted event delivery failed")
    assert "nothing was written" not in str(err)
    assert "outcome is unknown" in str(err)
    assert not page.read


def test_the_value_never_comes_back_in_an_error():
    page, err = _fail("fill expect_origin='https://login.example.com': something echoed hunter2 back; value was written")
    assert "hunter2" not in str(err)
    assert err.__cause__ is None and err.__suppress_context__


@pytest.mark.parametrize("state", [{"kept": False, "empty": False}, None, "unreadable"])
def test_an_unkept_guarded_write_is_unknown_and_never_retyped(monkeypatch, state):
    s = _Session()

    async def read(*args, **kwargs):
        if state == "unreadable":
            raise RuntimeError("hunter2 echoed by the engine")
        return state

    monkeypatch.setattr(s._page, "eval_on_selector", read)
    with pytest.raises(RuntimeError, match="outcome is unknown") as err:
        asyncio.run(actions.type_text(s, "#p", "hunter2", "https://login.example.com", "password"))
    assert "hunter2" not in str(err.value)
    assert "typed into" not in str(err.value)
    assert len(s._page.calls) == 1


@pytest.mark.parametrize("late", [False, True])
def test_only_an_observed_empty_field_is_rewritten_with_both_guards(monkeypatch, late):
    s = _Session()
    kept = {"kept": True, "empty": False}
    values = iter(([kept] if late else []) + [{"kept": False, "empty": True}])

    async def read(*args, **kwargs):
        return next(values, kept)

    monkeypatch.setattr(s._page, "eval_on_selector", read)
    assert asyncio.run(actions.type_text(
        s, "#p", "hunter2", "https://login.example.com", "password")) == "typed into #p"
    assert s._page.calls == [("#p", "hunter2", {
        "timeout": 15_000, "expect_origin": "https://login.example.com",
        "expect_input_type": "password"})] * 2


def test_repeated_empty_reads_stop_after_three_guarded_writes(monkeypatch):
    s = _Session()

    async def read(*args, **kwargs):
        return {"kept": False, "empty": True}

    monkeypatch.setattr(s._page, "eval_on_selector", read)
    with pytest.raises(RuntimeError, match="three writes") as err:
        asyncio.run(actions.type_text(s, "#p", "hunter2", "https://login.example.com"))
    assert "hunter2" not in str(err.value) and "nothing was written" not in str(err.value)
    assert len(s._page.calls) == 3


def test_a_repeat_refusal_does_not_claim_the_whole_call_wrote_nothing(monkeypatch):
    s = _Session()
    fill = s._page.fill

    async def guarded_fill(*args, **kwargs):
        if s._page.calls:
            raise RuntimeError("expect_origin refused: hunter2; nothing was written")
        await fill(*args, **kwargs)

    async def read(*args, **kwargs):
        return {"kept": False, "empty": True}

    monkeypatch.setattr(s._page, "fill", guarded_fill)
    monkeypatch.setattr(s._page, "eval_on_selector", read)
    with pytest.raises(RuntimeError, match="outcome is unknown") as err:
        asyncio.run(actions.type_text(s, "#p", "hunter2", "https://login.example.com"))
    assert "nothing was written" not in str(err.value) and "hunter2" not in str(err.value)


def test_an_empty_guarded_clear_is_kept_without_rewriting():
    s = _Session()
    assert asyncio.run(actions.type_text(s, "#p", "", "https://login.example.com")) == "typed into #p"
    assert len(s._page.calls) == 1


def test_a_plain_field_emptied_by_the_page_is_not_rewritten(monkeypatch):
    s = _Session()

    async def read(*args, **kwargs):
        return {"value": "", "focused": True, "secret": True}

    monkeypatch.setattr(s._page, "evaluate", read)
    result = asyncio.run(actions.type_text(s, "#p", "hunter2"))
    assert "empty after typing" in result and "hunter2" not in result
    assert len(s._page.calls) == 1


async def test_guarded_mcp_success_is_terminal_and_exact_despite_background_news(work, monkeypatch):
    from invisible_playwright_mcp.mcp import work as work_module
    from test_owners import call, success

    success(await call("filler", "browser_open"))
    monkeypatch.setattr(work._open["main"], "page", lambda: _PageForRetry())
    # A guarded call must not return the normal long-typing progress response.
    monkeypatch.setattr(work_module, "ANSWER_WITHIN_S", 0.001)
    prior = asyncio.create_task(asyncio.sleep(0, result="typed into #old"))
    await prior
    work._typing["main"] = work_module._Typing(prior, "#old", 3, 0)
    result = await call("filler", "browser_type", {
        "selector": "#p", "text": "hunter2", "expect_origin": "https://login.example.com",
        "expect_input_type": "password"})
    assert success(result) == "typed into #p"
    assert "#old" in work.typing_status("main")


class _PageForRetry(_Page):
    async def eval_on_selector(self, *args, **kwargs):
        return {"kept": len(self.calls) > 1, "empty": len(self.calls) == 1}


async def test_origin_refusal_is_an_mcp_error_with_the_fillers_no_write_contract(work, monkeypatch):
    from test_owners import call, success, text

    success(await call("filler", "browser_open"))
    page = _FailingPage("expect_origin='https://evil.invalid': hunter2; nothing was written")
    monkeypatch.setattr(work._open["main"], "page", lambda: page)
    result = await call("filler", "browser_type", {
        "selector": "#p", "text": "hunter2", "expect_origin": "https://login.example.com",
        "expect_input_type": "password"})
    assert result.isError and "nothing was written" in text(result)
    assert "expect_origin refused" in text(result) and "hunter2" not in text(result)
    assert not page.read


# --- through the real engine -------------------------------------------------


@pytest.fixture(scope="module")
def login_page():
    import http.server
    import threading

    body = b"<html><body><input id=p type=password></body></html>"
    vuetify = b"""<html><body><input id=p type=password><script>
    window.focuses = 0;
    window.renders = 0;
    const field = document.querySelector('#p');
    field.addEventListener('focus', () => {
        if (++window.focuses === 1) queueMicrotask(() => {
            field.value = '';
            ++window.renders;
        });
    });
    </script></body></html>"""
    # The native fill never focuses, so a focus-time render cannot wipe it; a
    # page that empties the field after the first value it hears still can.
    wipes_input = b"""<html><body><input id=p type=password><script>
    window.focuses = 0;
    window.renders = 0;
    const field = document.querySelector('#p');
    field.addEventListener('focus', () => ++window.focuses);
    field.addEventListener('input', () => {
        if (++window.renders === 1) queueMicrotask(() => { field.value = ''; });
    });
    </script></body></html>"""
    # A password input in a CLOSED shadow root ahead of its open-root twin.
    # Page script sees only the open one; the engine sees both. The page keeps
    # the closed input in a variable only so the test can read it back.
    closed_twin = b"""<html><body><script>
    customElements.define('x-sealed', class extends HTMLElement {
        constructor() {
            super();
            const root = this.attachShadow({mode: 'closed'});
            root.innerHTML = '<input id=pw type=password>';
            window.sealedInput = root.querySelector('input');
        }
    });
    customElements.define('x-open', class extends HTMLElement {
        constructor() {
            super();
            this.attachShadow({mode: 'open'}).innerHTML = '<input id=pw type=password>';
        }
    });
    </script><x-sealed id=sealed></x-sealed><x-open id=host></x-open></body></html>"""
    pages = {"/vuetify": vuetify, "/wipes-first-input": wipes_input, "/closed-twin": closed_twin}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(pages.get(self.path, body))

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.mark.e2e
async def test_the_engine_writes_on_the_expected_origin_and_nowhere_else(login_page):
    from invisible_playwright.async_api import InvisiblePlaywright

    async with InvisiblePlaywright(seed=1, headless=True) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()

        class _S:
            def page(self):
                return page

        s = _S()
        await page.goto(login_page + "/login")
        assert await page.eval_on_selector(
            "#p", actions._GUARDED_STATE_JS, {"origin": login_page, "text": "hunter2"}
        ) == {"kept": False, "empty": True}
        with pytest.raises(Exception) as refused:
            await actions.type_text(s, "#p", "hunter2", "https://login.example.com")
        assert "hunter2" not in str(refused.value)
        assert await page.input_value("#p") == ""

        with pytest.raises(Exception) as wrong_type:
            await actions.type_text(s, "#p", "hunter2", login_page, "text")
        assert "nothing was written" in str(wrong_type.value)
        assert await page.input_value("#p") == ""

        assert await actions.type_text(s, "#p", "hunter2", login_page, "password") == "typed into #p"
        assert await page.input_value("#p") == "hunter2"
        await ctx.close()


@pytest.mark.e2e
@pytest.mark.parametrize("path,writes,counts", [
    # firefox-35 commits through setUserInput without focusing, as autofill
    # does: Vuetify's first-focus render never runs, so one write is kept.
    ("/vuetify", 1, [0, 0]),
    # A page that empties the field after its first input still gets a
    # second guarded write, and only because the field read back empty.
    ("/wipes-first-input", 2, [0, 2]),
], ids=["vuetify-first-focus", "wipes-first-input"])
async def test_vuetify_first_focus_render_keeps_the_fillers_guarded_delivery(
        login_page, monkeypatch, path, writes, counts):
    from invisible_playwright.async_api import InvisiblePlaywright
    from invisible_playwright_mcp.mcp.work import Work
    from test_owners import call, success, text

    async with InvisiblePlaywright(seed=1, headless=True) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(login_page + path)
        assert await page.evaluate("() => document.activeElement.id") != "p"
        fills = []
        fill = page.fill

        async def counted_fill(selector, value, **kwargs):
            fills.append((selector, value, kwargs))
            return await fill(selector, value, **kwargs)

        monkeypatch.setattr(page, "fill", counted_fill)

        class Session:
            def page(self):
                return page

            def is_usable(self):
                return True

        work = Work("credential-fixture")
        work._open["main"] = Session()
        monkeypatch.setattr(server, "work", work)
        monkeypatch.setattr(server, "owners", None)
        for delivery in range(3):
            result = await call("filler", "browser_type", {
                "browser": "main", "selector": "#p", "text": "hunter2",
                "expect_origin": login_page, "expect_input_type": "password"})
            assert not result.isError, "the filler would clear and fail: " + text(result)
            assert success(result) == "typed into #p"
            origin = json.loads(success(await call("filler", "browser_evaluate", {
                "browser": "main", "expression": "() => ({origin: location.origin})"})))
            assert origin == {"origin": login_page}
            state = json.loads(success(await call("filler", "browser_evaluate", {
                "browser": "main", "expression": """() => ({
                    origin: location.origin, found: !!document.querySelector('#p'),
                    empty: document.querySelector('#p').value === ''})"""})))
            assert state["origin"] == login_page and state["found"]
            if not state["empty"]:
                break
        else:
            pytest.fail("the filler exhausted its three empty-field deliveries")
        assert delivery == 0
        assert len(fills) == writes
        assert all(kw == {"timeout": 15_000, "expect_origin": login_page,
                          "expect_input_type": "password"} for _, _, kw in fills)
        assert await page.evaluate("() => [window.focuses, window.renders]") == counts
        snapshot = json.loads(success(await call("filler", "browser_snapshot")))
        password = next(el for el in snapshot["interactive_elements"] if el["selector"] == "#p")
        assert password["text"] == "\u2022" * 8
        assert "hunter2" not in json.dumps(snapshot)
        await ctx.close()


@pytest.mark.e2e
async def test_strict_refuses_a_twin_in_a_closed_shadow_root(login_page):
    """What page script resolves and what the guarded write resolves differ by
    closed shadow roots. Non-strict, the write takes whichever the engine meets
    first; strict refuses before writing, and a chain through the open host
    still reaches the one input page script checked."""
    from invisible_playwright.async_api import InvisiblePlaywright

    pinned = 'css=input:is(#pw):visible:enabled:not([readonly])[type="password" i]'
    chain = 'css=:is(#host) >> ' + pinned
    values = """() => ({sealed: window.sealedInput.value,
        open: document.querySelector('#host').shadowRoot.querySelector('input').value})"""
    async with InvisiblePlaywright(seed=1, headless=True) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()

        class _S:
            def page(self):
                return page

        s = _S()
        await page.goto(login_page + "/closed-twin")
        # Page script: one match, the open twin. (Closed roots are invisible to it.)
        assert await page.evaluate("""() => [...document.querySelectorAll('*')]
            .flatMap(e => e.shadowRoot ? [...e.shadowRoot.querySelectorAll('#pw')] : []).length""") == 1

        with pytest.raises(RuntimeError) as refused:
            await actions.type_text(s, pinned, "hunter2", login_page, "password", strict=True)
        assert "nothing was written" in str(refused.value) and "hunter2" not in str(refused.value)
        assert await page.evaluate(values) == {"sealed": "", "open": ""}

        assert await actions.type_text(s, chain, "hunter2", login_page, "password", strict=True) == \
            "typed into " + chain
        assert await page.evaluate(values) == {"sealed": "", "open": "hunter2"}

        # The defect strict closes: non-strict, the same pinned selector writes
        # into the closed twin, which page script never checked.
        await page.goto(login_page + "/closed-twin")
        assert await actions.type_text(s, pinned, "hunter2", login_page, "password") == "typed into " + pinned
        assert await page.evaluate(values) == {"sealed": "hunter2", "open": ""}
        await ctx.close()
