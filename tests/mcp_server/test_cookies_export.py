"""Credential export at the real SDK boundary, with engine-only cookie doubles."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from invisible_playwright.async_api import TargetClosedError

from invisible_playwright_mcp.mcp import cookies, masked, server
from invisible_playwright_mcp.mcp.owners import HANDLE_ERROR, HANDLE_KEY
from test_network import Context, Request, finish
from test_owners import call, handle, identity, owners as owners, success, text

pytestmark = pytest.mark.skipif(os.name != "posix", reason="mcpd owner mode requires POSIX flock")
CANARY = "CookieCanary9-private<&-'value"
ORIGIN = "https://sub.intuit.com"


def cookie(**changes):
    return dict({"name": "session", "value": CANARY, "domain": ".intuit.com",
                 "path": "/", "expires": -1, "httpOnly": True, "secure": True,
                 "sameSite": "Lax"}, **changes)


@pytest.fixture
async def opened(owners, monkeypatch):
    monkeypatch.setenv(cookies.DOMAINS_ENV, "intuit.com")
    capability = handle(await call("A", "browser_open"))
    session = owners.entries["A"].work._open["main"]
    context = Context()
    context.cookies = AsyncMock(return_value=[cookie()])
    session._context = context
    session.network.attach(context)
    return capability, session, context


async def export(opened, **changes):
    return await call("A", cookies.TOOL, dict(
        {"browser_handle": opened[0], "origins": [ORIGIN]}, **changes))


async def test_schema_and_exact_single_text_result(opened):
    tool = next(tool for tool in await server.mcp.list_tools() if tool.name == cookies.TOOL)
    assert tool.inputSchema["additionalProperties"] is False
    assert set(tool.inputSchema["properties"]) == {"browser_handle", "origins"}
    assert set(tool.inputSchema["required"]) == {"browser_handle", "origins"}
    assert tool.inputSchema["properties"]["origins"]["minItems"] == 1
    assert tool.inputSchema["properties"]["origins"]["maxItems"] == 8
    assert tool.outputSchema is None
    before = datetime.now(timezone.utc)
    result = await export(opened)
    payload = json.loads(success(result))
    assert result.structuredContent is None
    assert len(result.content) == 1 and result.content[0].type == "text"
    assert set(payload) == {"cookies", "count", "origins", "exported_at"}
    assert payload["count"] == len(payload["cookies"]) == 1
    assert payload["origins"] == [ORIGIN]
    assert payload["cookies"] == [{
        "name": "session", "value": CANARY, "domain": ".intuit.com", "path": "/",
        "expires": -1, "http_only": True, "secure": True, "same_site": "Lax"}]
    stamp = datetime.fromisoformat(payload["exported_at"].replace("Z", "+00:00"))
    assert before.replace(microsecond=0) <= stamp <= datetime.now(timezone.utc)
    opened[2].cookies.assert_awaited_once_with([ORIGIN])


@pytest.mark.parametrize("setting", [None, "", " ", ", ,", "\t\n, "])
async def test_disabled_without_an_allowlist(opened, monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv(cookies.DOMAINS_ENV)
    else:
        monkeypatch.setenv(cookies.DOMAINS_ENV, setting)
    result = await export(opened)
    assert result.isError and text(result) == cookies.DISABLED
    opened[2].cookies.assert_not_awaited()


@pytest.mark.parametrize("setting", ["*.intuit.com", ".intuit.com", "com", "127.0.0.1",
                                     "https://intuit.com", "intuit.com,"])
async def test_malformed_allowlist_fails_closed(opened, monkeypatch, setting):
    monkeypatch.setenv(cookies.DOMAINS_ENV, setting)
    result = await export(opened)
    assert result.isError and "allowlist is invalid" in text(result)
    opened[2].cookies.assert_not_awaited()


@pytest.mark.parametrize("value", [None, "", "bad", "bh_" + "a" * 43, 1, True, []])
async def test_invalid_handle_uses_existing_error(opened, value):
    result = await export(opened, browser_handle=value)
    assert result.isError and text(result) == HANDLE_ERROR
    opened[2].cookies.assert_not_awaited()


async def test_handle_is_required_in_arguments_not_metadata(opened):
    for meta in (identity("A"), {**identity("A"), HANDLE_KEY: opened[0]}):
        result = await call("A", cookies.TOOL, {"origins": [ORIGIN]}, meta=meta)
        assert result.isError
        assert text(result) == (HANDLE_ERROR if HANDLE_KEY not in meta else
                                "This tool or browser is not permitted with a fill handle.")
    opened[2].cookies.assert_not_awaited()


@pytest.mark.parametrize("ending", ["close", "reopen", "end", "idle"])
async def test_revoked_handle_never_exports(opened, owners, ending):
    if ending == "end":
        await owners.session_ended("A")
        caller = "B"
    elif ending == "idle":
        now = owners.entries["A"].last_used + owners.idle_seconds + 1
        owners.clock = lambda: now
        await owners.reap_idle()
        caller = "A"
    else:
        success(await call("A", "browser_close" if ending == "close" else "browser_open"))
        caller = "A"
    result = await call(caller, cookies.TOOL, {
        "browser_handle": opened[0], "origins": [ORIGIN]})
    assert result.isError and text(result) == HANDLE_ERROR
    opened[2].cookies.assert_not_awaited()


async def test_other_owner_and_delegated_handles_are_refused(opened):
    success(await call("B", "browser_open"))
    for meta in (identity("B"), {**identity("B"), HANDLE_KEY: opened[0]}):
        result = await call("B", cookies.TOOL, {
            "browser_handle": opened[0], "origins": [ORIGIN]}, meta=meta)
        assert result.isError
    opened[2].cookies.assert_not_awaited()


async def test_support_browser_cannot_be_selected_or_supply_a_handle(opened, owners):
    support = success(await call("A", "browser_open", {"browser": "support"}))
    assert "fill handle:" not in support
    other = owners.entries["A"].work._open["support"]
    other._context = SimpleNamespace(cookies=AsyncMock(return_value=[cookie(value="support-secret")]))
    for browser in ("main", "support", None):
        result = await export(opened, browser=browser)
        assert result.isError
    assert CANARY in success(await export(opened))
    other._context.cookies.assert_not_awaited()


async def test_non_owner_mode_never_falls_back(opened, monkeypatch):
    monkeypatch.setattr(server, "owners", None)
    result = await export(opened)
    assert result.isError and text(result) == HANDLE_ERROR
    opened[2].cookies.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"origins": []}, {"origins": [ORIGIN] * 9}, {"origins": ORIGIN},
    {"origins": json.dumps([ORIGIN])}, {"origins": [1]}, {"origins": [None]},
    {"origins": None}, {"origins": True}, {"other": "unrecognized"},
])
async def test_strict_arguments_do_not_coerce_or_ignore(opened, changes):
    result = await export(opened, **changes)
    assert result.isError and "requires only browser_handle" in text(result)
    opened[2].cookies.assert_not_awaited()


async def test_origins_are_required(opened):
    result = await call("A", cookies.TOOL, {"browser_handle": opened[0]})
    assert result.isError
    opened[2].cookies.assert_not_awaited()


@pytest.mark.parametrize("origin", [
    "http://sub.intuit.com", "https://other.com", "https://evilintuit.com",
    "https://intuit.com.evil.com", "https://sub.intuit.com/",
    "https://sub.intuit.com/private", "https://sub.intuit.com:443",
    "https://sub.intuit.com?x", "https://sub.intuit.com#x",
    "https://user@sub.intuit.com", "https://sub.intuit.com.",
    "https://sub.intuit.com\n", " https://sub.intuit.com", "https://sub.intuit.com\\evil",
])
async def test_unapproved_or_non_origin_urls_are_refused(opened, origin):
    result = await export(opened, origins=[origin])
    assert result.isError and CANARY not in text(result)
    opened[2].cookies.assert_not_awaited()


def test_suffix_matching_requires_a_label_boundary():
    assert cookies.matches_suffix("intuit.com", "intuit.com")
    assert cookies.matches_suffix("sub.intuit.com", "intuit.com")
    assert not cookies.matches_suffix("evilintuit.com", "intuit.com")
    assert not cookies.matches_suffix("intuit.com.evil.com", "intuit.com")


async def test_engine_results_are_filtered_by_domain_origin_path_and_expiry(opened):
    opened[2].cookies.return_value = [
        cookie(name="domain"), cookie(name="host", domain="sub.intuit.com", secure=False),
        cookie(name="host-parent", domain="intuit.com"),
        cookie(name="sibling", domain=".other.intuit.com"),
        cookie(name="evil", domain=".evilintuit.com"),
        cookie(name="outside", domain=".example.com"),
        cookie(name="ancestor", domain=".com"),
        cookie(name="path", path="/private"), cookie(name="expired", expires=1),
        cookie(name="persistent", expires=4102444800, httpOnly=False, sameSite="Strict"),
    ]
    payload = json.loads(success(await export(opened)))
    assert [c["name"] for c in payload["cookies"]] == ["domain", "host", "persistent"]
    assert payload["cookies"][1]["secure"] is False
    assert payload["cookies"][2]["expires"] == 4102444800
    assert payload["cookies"][2]["http_only"] is False
    assert payload["cookies"][2]["same_site"] == "Strict"
    opened[2].cookies.assert_awaited_once_with([ORIGIN])


async def test_eight_origins_and_multiple_suffixes(opened, monkeypatch):
    monkeypatch.setenv(cookies.DOMAINS_ENV, " Intuit.COM, example.com ")
    origins = [ORIGIN, "https://EXAMPLE.COM"] + [f"https://s{i}.intuit.com" for i in range(6)]
    opened[2].cookies.return_value = [cookie(), cookie(domain="example.com", sameSite="None")]
    payload = json.loads(success(await export(opened, origins=origins)))
    assert payload["count"] == 2 and payload["origins"] == origins
    opened[2].cookies.assert_awaited_once_with(origins)


@pytest.mark.parametrize("count", [0, 200, 201])
async def test_cookie_count_cap_is_not_truncation(opened, count):
    opened[2].cookies.return_value = [cookie(name=f"c{i}") for i in range(count)]
    result = await export(opened)
    if count <= 200:
        payload = json.loads(success(result))
        assert payload["count"] == len(payload["cookies"]) == count
    else:
        assert result.isError and text(result) == "cookie export exceeds the 200-cookie limit"
        assert CANARY not in text(result)


async def test_byte_cap_includes_json_envelope_and_escaping(opened):
    opened[2].cookies.return_value = [cookie(value="")]
    base_size = len(success(await export(opened)).encode("utf-8"))
    room = cookies.MAX_BYTES - base_size
    for extra in (0, 1):
        # A quote is one input byte but two encoded JSON bytes.
        value = '"' * (room // 2) + "x" * (room % 2 + extra)
        opened[2].cookies.return_value = [cookie(value=value)]
        result = await export(opened)
        if extra == 0:
            assert len(success(result).encode("utf-8")) == 256 * 1024
            assert json.loads(text(result))["cookies"][0]["value"] == value
        else:
            assert result.isError and "256 KiB limit" in text(result)


@pytest.mark.parametrize("failure", ["engine", "closed", "malformed"])
async def test_failures_are_value_free_in_results_and_logs(opened, caplog, failure):
    caplog.set_level(logging.DEBUG)
    async def failing(origins):
        logging.getLogger("invisible_playwright_mcp").error("engine jar %s", CANARY)
        if failure == "closed":
            raise TargetClosedError(CANARY)
        if failure == "engine":
            raise RuntimeError(CANARY)
        return [cookie(expires=CANARY)]
    opened[2].cookies.side_effect = failing
    result = await export(opened)
    assert result.isError and CANARY not in result.model_dump_json()
    assert CANARY not in caplog.text
    assert not cookies.active.get()
    assert cookies.result.get() is None


@pytest.mark.parametrize("value", [CANARY, "0123456789abcdef" * 2 + "CANARY40"])
async def test_canary_only_leaves_through_export_not_logs_other_tools_or_capture(
        opened, monkeypatch, caplog, value):
    caplog.set_level(logging.DEBUG)
    session, context = opened[1:]
    context.cookies.return_value = [
        cookie(name="locale", value="en"), cookie(name="ivid", value="1"),
        cookie(name="x", value="true"), cookie(value=value),
    ]
    visible = "en enabled 1 invoice true"
    page_text = visible + " " + value
    expected = visible + " " + masked.MASKED_PASSWORD
    request = Request(url="https://sub.intuit.com/" + value, body=page_text.encode())
    context.emit("request", request)
    request.reply.raw = page_text.encode()
    await finish(session.network, context, request)
    before = json.loads(success(await call("A", "browser_network", {"include_bodies": True})))
    assert value in str(before)
    result = await export(opened)
    assert [c["value"] for c in json.loads(success(result))["cookies"]] == ["en", "1", "true", value]
    logging.getLogger("mcp.server.lowlevel.server").debug("result %s", result)
    logging.getLogger("invisible_playwright_mcp").info("echo %s", page_text)
    assert value not in caplog.text
    assert visible in caplog.text
    assert value not in str([row.data for row in session.network._rows.values()])
    request = Request(url="https://sub.intuit.com/" + value, body=page_text.encode())
    request.reply.raw = page_text.encode()
    context.emit("request", request)
    await finish(session.network, context, request)
    assert value not in str([row.data for row in session.network._rows.values()])
    for row in session.network._rows.values():
        assert row.data["post_data"] == row.data["response_body"] == expected
    network = success(await call("A", "browser_network", {"include_bodies": True}))
    assert value not in network and visible in network

    async def echo(*args, **kwargs):
        return page_text
    for name in ("read_text", "read_html", "snapshot", "evaluate"):
        monkeypatch.setattr(server.actions, name, echo)
    for tool, args in [
        ("browser_read_text", {}), ("browser_read_html", {}), ("browser_snapshot", {}),
        ("browser_evaluate", {"expression": "document.cookie"}),
    ]:
        answer = await call("A", tool, args)
        assert answer.content[0].type == "text"
        assert value not in success(answer)
        assert expected in text(answer)
    pixels = SimpleNamespace(eval_on_selector=AsyncMock(return_value=[value]),
                             screenshot=AsyncMock(return_value=b"private pixels"))
    monkeypatch.setattr(session, "page", lambda: pixels)
    answer = await call("A", "browser_take_screenshot")
    assert answer.isError and text(answer) == masked.SCREENSHOT_REFUSED
    pixels.screenshot.assert_not_awaited()
    success(await call("A", "browser_close"))
    logging.getLogger("mcp.server.lowlevel.server").debug("late result %s", result)
    assert value not in caplog.text


async def test_masked_fill_values_are_exported_only_in_the_authorized_result(opened):
    masked.registry(opened[1]).register(CANARY)
    assert json.loads(success(await export(opened)))["cookies"][0]["value"] == CANARY


@pytest.mark.parametrize("value", ["en", "1", "true", "text", "pending", "fetch", "json"])
async def test_short_cookies_do_not_corrupt_protocol_or_capture_state(opened, monkeypatch, value):
    session, context = opened[1:]
    context.cookies.return_value = [cookie(value=value)]
    success(await export(opened))
    async def echo(*args, **kwargs):
        return value
    monkeypatch.setattr(server.actions, "read_text", echo)
    result = await call("A", "browser_read_text")
    assert result.content[0].type == "text"
    assert success(result) == value
    assert not masked.registry(session).raw
    request = Request(body=value.encode())
    request.reply.raw = value.encode()
    context.emit("request", request)
    await finish(session.network, context, request)
    row = next(iter(session.network._rows.values())).data
    assert row["resource_type"] == "fetch"
    assert row["post_data"] == row["response_body"] == value
    assert row["response_body_state"] == "captured"


@pytest.mark.parametrize("length", [0, 1, 7, 8, 15, 16, 40])
async def test_only_cookie_values_of_at_least_16_characters_are_registered(opened, length):
    value = "V" * length
    opened[2].cookies.return_value = [cookie(value=value)]
    assert json.loads(success(await export(opened)))["cookies"][0]["value"] == value
    values = masked.registry(opened[1])
    assert values.redact(value) == (masked.MASKED_PASSWORD if length >= 16 else value)
    assert values.raw == ({value} if length >= 16 else set())


async def test_url_valued_cookies_do_not_mask_the_pages_own_origin(opened):
    # A return-URL cookie used to register every 8-character window of
    # "qbo.intuit.com", so browser_evaluate read location.origin as bullets.
    value = "https%3A%2F%2Fqbo.intuit.com%2Fapp%2Fhomepage"
    opened[2].cookies.return_value = [cookie(value=value)]
    success(await export(opened))
    values = masked.registry(opened[1])
    assert values.redact("https://qbo.intuit.com") == "https://qbo.intuit.com"
    assert values.redact("next=" + value + ";") == "next=" + masked.MASKED_PASSWORD + ";"
    assert values.redact("next=" + value[:-1]) == "next=" + value[:-1]


async def test_short_cookie_values_leave_screenshots_available(opened, monkeypatch):
    opened[2].cookies.return_value = [
        cookie(name="locale", value="en"), cookie(name="ivid", value="1"),
        cookie(name="x", value="true"),
    ]
    pixels = SimpleNamespace(eval_on_selector=AsyncMock(return_value=["en 1 true"]),
                             screenshot=AsyncMock(return_value=b"public pixels"))
    monkeypatch.setattr(opened[1], "page", lambda: pixels)
    success(await export(opened))
    result = await call("A", "browser_take_screenshot")
    assert not result.isError, text(result)
    assert result.content[0].type == "image"
    pixels.screenshot.assert_awaited_once()
    pixels.eval_on_selector.assert_not_awaited()


async def test_export_leaves_the_callers_watch_running(opened):
    session = opened[1]
    session.stop_watching = AsyncMock(side_effect=RuntimeError(CANARY))
    assert json.loads(success(await export(opened)))["count"] == 1
    session.stop_watching.assert_not_awaited()


async def test_cancelled_export_resets_log_context_and_returns_no_payload(opened):
    entered = asyncio.Event()
    async def read(origins):
        entered.set()
        await asyncio.Event().wait()
    opened[2].cookies.side_effect = read
    async def cancelled():
        try:
            await export(opened)
        finally:
            assert not cookies.active.get()
            assert cookies.result.get() is None
    task = asyncio.create_task(cancelled())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_handle_revoked_during_cookie_read_cannot_return_credentials(opened, owners):
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(origins):
        entered.set()
        await release.wait()
        return [cookie()]
    opened[2].cookies.side_effect = read
    task = asyncio.create_task(export(opened))
    await asyncio.wait_for(entered.wait(), 2)
    owners.entries["A"].work.revoke()
    release.set()
    result = await task
    assert result.isError and text(result) == HANDLE_ERROR
    assert CANARY not in text(result)
