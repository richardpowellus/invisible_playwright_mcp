"""Masked text stays private at the owner result boundary, not in page markup."""
from __future__ import annotations

import ast
import asyncio
import html
import inspect
import json
import logging
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from invisible_playwright_mcp.mcp import actions, clean, server
from test_owners import call, handle, owners as owners, success, text
from test_a_credential_is_typed_only_on_its_origin import login_page as login_page

VALUE = 'Q7secret<&"\'\\\u00e9 X9answer-' * 4
ORIGIN = "https://login.example.com"
REFUSAL = "screenshot refused: a masked value is on the page; nothing was captured"
PIXEL_TOOLS = [
    ("browser_take_screenshot", {}),
    ("browser_watch", {}),
    ("browser_click_at", {"x": 10, "y": 10}),
]


class Page:
    def __init__(self):
        self.value = ""
        self.captures = 0
        self.error = False

    async def fill(self, selector, value, **kwargs):
        self.value = value

    async def eval_on_selector(self, selector, expression, arg=None):
        if arg is not None:
            return {"kept": self.value == arg["text"], "empty": not self.value}
        if self.error:
            raise RuntimeError(VALUE)
        return [self.value]

    async def screenshot(self):
        self.captures += 1
        return b"png"


@pytest.fixture
async def filled(owners, monkeypatch):
    monkeypatch.setattr(actions, "GUARD_SETTLE_S", 0)
    monkeypatch.setattr(actions, "GUARD_SINCE_START_S", 0)
    success(await call("A", "browser_open"))
    session = owners.entries["A"].work._open["main"]
    page = Page()
    monkeypatch.setattr(session, "page", lambda: page)
    success(await call("A", "browser_type", {
        "selector": "#p", "text": VALUE, "expect_origin": ORIGIN,
        "expect_input_type": "text", "mask_value": True}))
    return session, page


async def test_catalogue_declares_a_boolean_default_false():
    tool = next(t for t in await server.mcp.list_tools() if t.name == "browser_type")
    assert tool.inputSchema["properties"]["mask_value"] == {
        "default": False, "title": "Mask Value", "type": "boolean"}


@pytest.mark.parametrize("value", ["true", 1, None])
async def test_mask_value_is_a_json_boolean_not_a_coerced_flag(owners, value):
    result = await call("A", "browser_type", {
        "selector": "#p", "text": VALUE, "expect_origin": ORIGIN, "mask_value": value})
    assert result.isError and "valid boolean" in text(result)
    assert not owners.entries["A"].work._open


@pytest.mark.parametrize("value,origin,reason", [
    (VALUE, None, "requires expect_origin"),
    ("short", ORIGIN, "text must be at least 8 characters"),
    ("1234567", ORIGIN, "text must be at least 8 characters"),
    ("", None, "requires expect_origin"),
])
async def test_mask_refusals_write_nothing(owners, monkeypatch, value, origin, reason):
    success(await call("A", "browser_open"))
    page = Page()
    monkeypatch.setattr(owners.entries["A"].work._open["main"], "page", lambda: page)
    args = {"selector": "#p", "text": value, "mask_value": True}
    if origin is not None:
        args["expect_origin"] = origin
    result = await call("A", "browser_type", args)
    assert result.isError
    assert text(result) == "mask_value refused: " + reason + "; nothing was written"
    assert page.value == ""


async def test_registration_precedes_the_write_and_an_empty_clear_registers_nothing(
        filled, owners, monkeypatch):
    from invisible_playwright_mcp.mcp import masked

    session, page = filled
    registry = masked.registry(session)
    assert registry.redact(VALUE) == clean.MASKED_PASSWORD
    second = "Another-private-answer"

    async def fill(selector, value, **kwargs):
        if value:
            assert registry.redact(value) == clean.MASKED_PASSWORD
        page.value = value

    monkeypatch.setattr(page, "fill", fill)
    for value in (second, ""):
        result = await call("A", "browser_type", {
            "selector": "#p", "text": value, "expect_origin": ORIGIN, "mask_value": True})
        assert success(result) == "typed into #p"
    assert registry.redact(VALUE + second) == clean.MASKED_PASSWORD
    assert registry.redact("ordinary text") == "ordinary text"


@pytest.mark.parametrize("form", [
    lambda v: v, lambda v: html.escape(v, quote=True), lambda v: html.escape(v, quote=False),
    lambda v: json.dumps(v, ensure_ascii=True)[1:-1],
    lambda v: json.dumps(v, ensure_ascii=False)[1:-1],
    lambda v: quote(v, safe=""), quote_plus,
], ids=["raw", "html-quotes", "html", "json-ascii", "json-unicode", "url", "url-plus"])
async def test_raw_escaped_and_truncated_forms_are_one_mask(filled, form):
    from invisible_playwright_mcp.mcp import masked

    registry = masked.registry(filled[0])
    value = form(VALUE)
    for fragment in (value, value[:60], value[5:13], value[-8:]):
        assert registry.redact("before|" + fragment + "|after") == (
            "before|" + clean.MASKED_PASSWORD + "|after")
    assert registry.redact(value[:7]) == value[:7]


async def test_overlapping_and_adjacent_windows_merge_without_cascading(filled):
    from invisible_playwright_mcp.mcp import masked

    registry = masked.registry(filled[0])
    assert registry.redact(VALUE[:8] + VALUE[-8:]) == clean.MASKED_PASSWORD
    assert registry.redact(VALUE[:8] + "|" + VALUE[-8:]) == (
        clean.MASKED_PASSWORD + "|" + clean.MASKED_PASSWORD)


@pytest.mark.parametrize("fails", [False, True])
async def test_every_registered_tool_and_a_new_tool_pass_the_result_boundary(
        filled, monkeypatch, fails):
    """Walk the actual registry, not a hand-maintained list of tool names."""
    from invisible_playwright_mcp.mcp import masked

    seen = []
    redact = masked.redact_result

    def record(result, registries):
        seen.append(result)
        return redact(result, registries)

    monkeypatch.setattr(masked, "redact_result", record)

    async def answer() -> str:
        if fails:
            raise ValueError(VALUE)
        return VALUE

    async def future_tool() -> str:
        return VALUE

    server.mcp.add_tool(future_tool)
    try:
        from mcp.server.fastmcp.tools.base import Tool

        for tool in server.mcp._tool_manager.list_tools():
            # Keep the SDK's normalization/error path; only replace the operation.
            stub = Tool.from_function(answer, name=tool.name)
            monkeypatch.setattr(tool, "fn_metadata", stub.fn_metadata)
            monkeypatch.setattr(tool, "fn", stub.fn)
            monkeypatch.setattr(tool, "parameters", stub.parameters)
            before = len(seen)
            result = await call("A", tool.name)
            assert len(seen) == before + 1, tool.name
            assert result.isError == fails
            assert VALUE not in result.model_dump_json()
            assert clean.MASKED_PASSWORD in text(result)
    finally:
        server.mcp.remove_tool("future_tool")


async def test_errors_validation_unknown_tools_and_structured_content_are_redacted(
        filled, monkeypatch):
    result = await call("A", "missing-" + VALUE)
    assert result.isError and VALUE not in text(result)
    result = await call("A", "browser_click_at", {"x": VALUE, "y": 0})
    assert result.isError and VALUE not in text(result)

    async def structured() -> dict[str, str | list[str]]:
        return {"value": VALUE, VALUE: [VALUE]}

    server.mcp.add_tool(structured)
    try:
        result = await call("A", "structured")
        assert not result.isError
        assert VALUE not in result.model_dump_json()
        assert result.structuredContent == {
            "value": clean.MASKED_PASSWORD, clean.MASKED_PASSWORD: [clean.MASKED_PASSWORD]}
    finally:
        server.mcp.remove_tool("structured")


async def test_policy_errors_and_a_close_result_are_scrubbed(filled, owners):
    from invisible_playwright_mcp.mcp import masked

    registry = masked.registry(filled[0])
    registry.register("refused in owner mode")
    result = await call("A", "browser_navigate", {"url": "file:///tmp/not-read"})
    assert result.isError and clean.MASKED_PASSWORD in text(result)
    assert "refused in owner mode" not in text(result)

    async def close_and_echo() -> str:
        await owners.entries["A"].work.close("main")
        return VALUE

    server.mcp.add_tool(close_and_echo)
    try:
        assert success(await call("A", "close_and_echo")) == clean.MASKED_PASSWORD
        assert registry.redact(VALUE) == VALUE
    finally:
        server.mcp.remove_tool("close_and_echo")


@pytest.mark.parametrize("tool,args", PIXEL_TOOLS)
async def test_all_pixel_tools_refuse_before_capture(filled, tool, args):
    result = await call("A", tool, args)
    assert result.isError and text(result) == REFUSAL
    assert filled[1].captures == 0


async def test_presence_check_errors_fail_closed(filled):
    filled[1].value = ""
    filled[1].error = True
    result = await call("A", "browser_take_screenshot")
    assert result.isError and text(result) == REFUSAL
    assert filled[1].captures == 0


async def test_click_checks_again_after_the_click_reveals_a_value(filled, monkeypatch):
    from types import SimpleNamespace

    page = filled[1]
    page.value = ""

    async def nothing(*args, **kwargs):
        pass

    async def reveal(*args, **kwargs):
        page.value = VALUE

    monkeypatch.setattr(page, "mouse", SimpleNamespace(
        move=nothing, down=nothing, up=reveal, click=reveal), raising=False)
    monkeypatch.setattr(page, "wait_for_timeout", nothing, raising=False)
    result = await call("A", "browser_click_at", {"x": 10, "y": 10})
    assert result.isError and text(result) == REFUSAL
    assert page.captures == 0


async def test_live_watch_stops_before_a_fill_and_never_reuses_a_secret_frame(monkeypatch):
    from invisible_playwright_mcp.mcp.session import StealthSession
    from test_watch import _FakeContext

    session = StealthSession()
    session._context = _FakeContext()
    page = await session.new_page()
    state = Page()
    monkeypatch.setattr(page, "eval_on_selector", state.eval_on_selector, raising=False)
    monkeypatch.setattr(actions, "GUARD_SETTLE_S", 0)
    monkeypatch.setattr(actions, "GUARD_SINCE_START_S", 0)

    async def fill(selector, value, **kwargs):
        assert not session._watch and page.screencast.on_frame is None
        state.value = value

    monkeypatch.setattr(page, "fill", fill, raising=False)
    asyncio.get_running_loop().call_later(0.02, page.screencast.deliver, b"old frame")
    assert await session.watch_frame() == b"old frame"
    assert await actions.type_text(session, "#p", VALUE, ORIGIN, "text", True) == "typed into #p"
    assert not session._watch and page.screencast.stops == 1
    with pytest.raises(ValueError, match="^" + REFUSAL + "$"):
        await session.watch_frame()
    await actions.type_text(session, "#p", "", ORIGIN, "text")
    asyncio.get_running_loop().call_later(0.02, page.screencast.deliver, b"new clean frame")
    assert await session.watch_frame() == b"new clean frame"
    assert not session._watch and page.screencast.stops == 2
    await session.close()


async def test_failed_watch_stop_never_allows_a_later_fill(monkeypatch):
    from invisible_playwright_mcp.mcp.session import StealthSession
    from test_watch import _FakeContext

    session = StealthSession()
    session._context = _FakeContext()
    page = await session.new_page()
    state = Page()
    monkeypatch.setattr(page, "fill", state.fill, raising=False)

    async def failed():
        raise RuntimeError(VALUE)

    asyncio.get_running_loop().call_later(0.02, page.screencast.deliver, b"old frame")
    await session.watch_frame()
    monkeypatch.setattr(page.screencast, "stop", failed)
    for _ in range(2):
        with pytest.raises(ValueError, match="^" + REFUSAL + "$"):
            await actions.type_text(session, "#p", VALUE, ORIGIN, "text", True)
        assert state.value == ""
        assert session._watch[id(page)]["latest"] == b""
    await session.close()


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_protected_watch_is_stopped_even_without_a_frame(monkeypatch, ending):
    from invisible_playwright_mcp.mcp import masked
    from invisible_playwright_mcp.mcp.session import StealthSession
    from test_watch import _FakeContext

    session = StealthSession()
    session._context = _FakeContext()
    page = await session.new_page()
    monkeypatch.setattr(page, "eval_on_selector", Page().eval_on_selector, raising=False)
    masked.registry(session).register(VALUE)
    task = asyncio.create_task(session.watch_frame(timeout=0.03 if ending == "timeout" else 5))
    while not page.screencast.starts:
        await asyncio.sleep(0)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="no frame arrived"):
            await task
    assert not session._watch and page.screencast.stops == 1
    await session.close()


@pytest.mark.parametrize("end", ["close", "reopen", "session"])
async def test_registry_is_owner_scoped_and_dropped_with_the_browser(filled, owners, end):
    from invisible_playwright_mcp.mcp import masked

    registry = masked.registry(filled[0])
    async def echo() -> str:
        return VALUE
    server.mcp.add_tool(echo)
    try:
        assert success(await call("A", "echo")) == clean.MASKED_PASSWORD
        assert success(await call("B", "echo")) == VALUE
        if end == "session":
            await owners.session_ended("A")
        else:
            success(await call("A", "browser_close" if end == "close" else "browser_open"))
        assert registry.redact(VALUE) == VALUE
    finally:
        server.mcp.remove_tool("echo")


async def test_fill_handle_can_probe_the_screenshot_guard(filled):
    from invisible_playwright_mcp.mcp.owners import HANDLE_KEY
    from test_owners import identity

    capability = handle(await call("A", "browser_status"))
    result = await call("filler", "browser_take_screenshot",
                        meta={**identity("filler"), HANDLE_KEY: capability})
    assert result.isError and text(result) == REFUSAL


async def test_logs_do_not_disclose_registered_values_or_incoming_masked_arguments(filled):
    from invisible_playwright_mcp.mcp.owner_transport import CapabilityRedactor

    for message in (VALUE, "browser_type mask_value=True text=" + VALUE):
        record = logging.LogRecord("mcp.server.lowlevel.server", logging.DEBUG,
                                   "", 0, message, (), None)
        for filter_ in logging.getLogger("mcp.server.lowlevel.server").filters:
            if isinstance(filter_, CapabilityRedactor):
                filter_.filter(record)
        assert VALUE not in record.getMessage()


def test_pixel_producers_remain_an_explicit_inventory():
    root = Path(inspect.getfile(server)).parent
    found = set()
    for file in root.glob("*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"screenshot", "pdf", "watch_frame"}:
                    found.add((file.name, node.func.attr))
                if node.func.attr == "start" and isinstance(node.func.value, ast.Attribute):
                    if node.func.value.attr == "screencast":
                        found.add((file.name, "screencast.start"))
    assert found == {("actions.py", "screenshot"), ("server.py", "watch_frame"),
                     ("session.py", "screencast.start")}


@pytest.mark.e2e
async def test_real_masked_text_readers_pixels_and_utility_world(login_page, owners, monkeypatch):
    from invisible_playwright.async_api import InvisiblePlaywright
    from invisible_playwright_mcp.mcp.session import StealthSession

    async with InvisiblePlaywright(seed=1, headless=True) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(login_page)
        # Fixture setup is page-owned code, never production masking logic.
        await page.evaluate("""() => {
            const p = document.querySelector('#p');
            p.type = 'text';
            p.oninput = () => p.setAttribute('value', p.value);
            document.body.insertAdjacentHTML('beforeend', '<div id=copy></div><div id=host></div>');
        }""")
        session = StealthSession()
        session._context = ctx
        owner = owners.caller("A")
        owner.work._open["main"] = session
        monkeypatch.setattr(actions, "GUARD_SETTLE_S", 0)
        monkeypatch.setattr(actions, "GUARD_SINCE_START_S", 0)
        value = "Canary9-private-generated-answer-" * 3
        result = await call("A", "browser_type", {
            "selector": "#p", "text": value, "expect_origin": login_page,
            "expect_input_type": "text", "mask_value": True})
        assert success(result) == "typed into #p"
        snapshot = json.loads(success(await call("A", "browser_snapshot")))
        entry = next(e for e in snapshot["interactive_elements"] if e["selector"] == "#p")
        assert entry["text"] == clean.MASKED_PASSWORD
        markup = success(await call("A", "browser_read_html", {"mode": "form"}))
        assert f'value="{clean.MASKED_PASSWORD}"' in markup
        assert value[:8] not in markup
        result = await call("A", "browser_evaluate", {"expression": "() => document.querySelector('#p').value"})
        assert json.loads(success(result)) == clean.MASKED_PASSWORD
        # Page-world lies must not blind the utility-world presence reader.
        await page.evaluate("""() => {
            window.originalValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            Object.defineProperty(HTMLInputElement.prototype, 'value',
                {get: () => '', configurable: true});
        }""")
        for name, args in PIXEL_TOOLS:
            result = await call("A", name, args)
            assert result.isError and text(result) == REFUSAL
        await page.evaluate("() => { Object.defineProperty(HTMLInputElement.prototype, 'value', window.originalValue); }")
        success(await call("A", "browser_type", {
            "selector": "#p", "text": "", "expect_origin": login_page}))
        for location in ("body", "shadow-input", "shadow-text", "frame"):
            await page.evaluate("""where => {
                const copy = document.querySelector('#copy'), host = document.querySelector('#host');
                const value = 'Canary9-private-generated-answer-';
                copy.textContent = ''; if (host.shadowRoot) host.shadowRoot.innerHTML = '';
                if (where === 'body') copy.innerText = value;
                if (where.startsWith('shadow')) {
                    const root = host.shadowRoot || host.attachShadow({mode: 'open'});
                    root.innerHTML = where === 'shadow-input' ? '<textarea>' + value + '</textarea>' :
                        '<span>' + value + '</span>';
                }
                if (where === 'frame') copy.innerHTML = '<iframe srcdoc="<input value=' + value + '>"></iframe>';
            }""", location)
            if location == "frame":
                await page.wait_for_timeout(200)
            result = await call("A", "browser_take_screenshot")
            assert result.isError and text(result) == REFUSAL, location
        await page.evaluate("""() => {
            document.querySelector('#copy').innerHTML = '';
            document.querySelector('#host').shadowRoot.innerHTML = '';
        }""")
        result = await call("A", "browser_take_screenshot")
        assert not result.isError and result.content[0].type == "image"
        await owner.work.close_all()
