"""Owner isolation at SDK dispatch, without launching Firefox."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from mcp.shared.message import SessionMessage

from invisible_playwright_mcp.mcp import NOT_OPEN, actions, server, store
from invisible_playwright_mcp.mcp.owner_transport import (
    CapabilityRedactor, SessionEnded, notifications,
)
from invisible_playwright_mcp.mcp.owners import (
    HANDLE_KEY, HANDLE_TOOLS, IDENTITY_ERROR, Owners,
)
from test_open_first import EVERY_TOOL, _Recording


def identity(owner):
    return {"mcpd/identity": {"sessionId": owner}}


async def call(owner, name, arguments=None, *, meta=None):
    request = types.CallToolRequest.model_validate({
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {},
                   "_meta": identity(owner) if meta is None else meta},
    })
    token = request_ctx.set(RequestContext(
        request_id="test", meta=request.params.meta, session=None, lifespan_context={}))
    try:
        result = await server.mcp._mcp_server.request_handlers[types.CallToolRequest](request)
        return result.root
    finally:
        request_ctx.reset(token)


def text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


def success(result):
    assert not result.isError, text(result)
    return text(result)


def handle(result):
    return re.search(r"fill handle: (bh_[A-Za-z0-9_-]{43})", success(result))[1]


@pytest.fixture
async def owners(monkeypatch, tmp_path):
    for name in ("STEALTHFOX_SEED", "STEALTHFOX_PROXY", "STEALTHFOX_PROFILE_DIR",
                 "STEALTHFOX_HEADLESS", "STEALTHFOX_BINARY", "STEALTHFOX_NO_PROXY",
                 actions.UPLOAD_DIRS_ENV, actions.DOWNLOAD_DIRS_ENV):
        monkeypatch.delenv(name, raising=False)
    registry = Owners(factory=_Recording)
    monkeypatch.setattr(server, "owners", registry)
    # Keep all test profiles in pytest's own temporary tree.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    yield registry
    await registry.close_all()
    assert registry.capacity.used == 0
    assert not list(tmp_path.glob("stealthfox-proc-*"))


async def test_a_b_negative_isolation(owners):
    success(await call("A", "browser_open", {"seed": 123}))
    a = owners.entries["A"].work._open["main"]
    a.urls = ["https://owner-a.invalid/private"]
    listing = json.loads(success(await call("B", "browser_list")))
    assert listing["browsers"] == []
    assert listing["focus"] == ""
    status = await call("B", "browser_status")
    assert status.isError
    assert text(status) == "Error executing tool browser_status: " + NOT_OPEN % "main"
    assert "owner-a" not in text(status)
    success(await call("B", "browser_close"))
    assert not a.closed
    assert "owner-a.invalid" in success(await call("A", "browser_status"))


@pytest.mark.parametrize("name,args", EVERY_TOOL, ids=[name for name, _ in EVERY_TOOL])
@pytest.mark.parametrize("role", ["main", "support"])
async def test_every_b_tool_is_owner_scoped(owners, name, args, role):
    success(await call("A", "browser_open", {"browser": role}))
    a = owners.entries["A"].work._open[role]
    a.urls = ["https://owner-a.invalid/private"]
    result = await call("B", name, dict(args, browser=role))
    assert result.isError
    assert text(result) == "Error executing tool %s: " % name + NOT_OPEN % role
    assert not a.closed
    assert a.urls == ["https://owner-a.invalid/private"]


async def test_b_open_and_close_never_replace_a(owners):
    success(await call("A", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    success(await call("B", "browser_open"))
    b = owners.entries["B"].work._open["main"]
    assert a is not b
    assert a.kwargs["profile_dir"] != b.kwargs["profile_dir"]
    success(await call("B", "browser_close"))
    assert b.closed and not a.closed


@pytest.mark.parametrize("meta", [
    {}, {"sessionId": "A"}, {"mcpd/identity": None}, {"mcpd/identity": "A"},
    {"mcpd/identity": {}}, {"mcpd/identity": {"sessionId": ""}},
    {"mcpd/identity": {"sessionId": " "}}, {"mcpd/identity": {"sessionId": 1}},
    {"mcpd/identity": {"sessionId": ["A"]}},
    {"mcpd/identity": {"sessionId": " A"}},
    {"mcpd/identity": {"sessionId": "A\nB"}},
])
async def test_missing_or_malformed_identity_never_falls_back(owners, meta):
    success(await call("A", "browser_open"))
    result = await call(None, "browser_list", meta=meta)
    assert result.isError and text(result) == IDENTITY_ERROR
    assert set(owners.entries) == {"A"}


async def test_identity_cannot_be_forged_in_tool_arguments(owners):
    success(await call("A", "browser_open"))
    result = await call("B", "browser_list", {
        "_meta": identity("A"), "sessionId": "A", "owner": "A"})
    assert json.loads(success(result))["browsers"] == []
    result = await call(None, "browser_list", {"_meta": identity("A")}, meta={})
    assert result.isError and text(result) == IDENTITY_ERROR
    with pytest.raises(ValueError, match="mcpd/identity"):
        await server.browser_list()


async def test_session_ended_closes_only_owner_and_frees_capacity(owners):
    success(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    b = owners.entries["B"].work._open["main"]
    await server.mcp._mcp_server._handle_notification(SessionEnded.model_validate({
        "method": "notifications/mcpd/session_ended",
        "params": {"sessionId": "A", "reason": "delete"},
    }))
    assert a.closed and not b.closed
    assert not Path(a.kwargs["profile_dir"]).exists()
    assert owners.capacity.used == 1
    success(await call("B", "browser_open", {"browser": "support"}))
    before = set(owners.entries)
    await owners.session_ended("unknown")
    assert set(owners.entries) == before
    assert "unknown" not in owners.ended


async def test_ended_session_bookkeeping_is_capped_and_evicts_oldest(owners):
    for index in range(4096 + 32):
        session = f"ended-{index}"
        owners.caller(session)
        await owners.session_ended(session)
        assert len(owners.ended) <= 4096
        assert not owners.entries
    assert set(owners.ended) == {f"ended-{index}" for index in range(32, 4096 + 32)}
    with pytest.raises(ValueError, match="session has ended"):
        owners.caller("ended-4127")


async def test_session_end_notification_does_not_block_other_callers(owners):
    success(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    close = a.close
    closing, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        closing.set()
        await release.wait()
        await close()

    a.close = slow_close
    send, receive = anyio.create_memory_object_stream(1)
    try:
        async with send, receive, notifications(receive, server.mcp._mcp_server) as filtered:
            await send.send(SessionMessage(types.JSONRPCMessage.model_validate({
                "jsonrpc": "2.0", "method": "notifications/mcpd/session_ended",
                "params": {"sessionId": "A", "reason": "delete"},
            })))
            await asyncio.wait_for(closing.wait(), 2)
            request = SessionMessage(types.JSONRPCMessage.model_validate({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "browser_list", "arguments": {}, "_meta": identity("B")},
            }))
            await send.send(request)
            assert await asyncio.wait_for(filtered.receive(), 2) is request
            assert owners.capacity.used == 2 and not a.closed
            success(await call("B", "browser_status"))
    finally:
        release.set()


async def test_idle_backstop_and_activity(owners):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    b = owners.entries["B"].work._open["main"]
    now[0] = 899
    await owners.reap_idle()
    assert not a.closed and not b.closed
    success(await call("B", "browser_list"))
    now[0] = 900
    await owners.reap_idle()
    assert a.closed and not b.closed
    assert owners.capacity.used == 1
    assert set(owners.entries) == {"B"}
    success(await call("A", "browser_open"))
    assert owners.entries["A"].work._open["main"] is not a


async def test_idle_empty_owner_is_removed_and_later_call_recreates_it(owners):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_list"))
    original = owners.entries["A"]
    now[0] = 900
    await owners.reap_idle()
    assert not owners.entries
    success(await call("A", "browser_open"))
    assert owners.entries["A"] is not original
    assert owners.entries["A"].work.roles() == ["main"]


async def test_idle_cleanup_retains_owner_for_queued_call(owners):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_open"))
    entry = owners.entries["A"]
    a = entry.work._open["main"]
    close = a.close
    closing, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        closing.set()
        await release.wait()
        await close()

    a.close = slow_close
    now[0] = 900
    reaping = asyncio.create_task(owners.reap_idle())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        opening = asyncio.create_task(call("A", "browser_open"))
        await asyncio.sleep(0)
    finally:
        release.set()
    await reaping
    success(await opening)
    assert owners.entries["A"] is entry
    assert owners.capacity.used == 1
    success(await call("A", "browser_status"))
    now[0] = 1800
    await owners.reap_idle()
    assert not owners.entries and owners.capacity.used == 0


async def test_idle_cleanup_retains_owner_until_profile_removal_succeeds(owners, monkeypatch):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_open"))
    entry = owners.entries["A"]
    now[0] = 900

    def refuse_cleanup(path):
        raise PermissionError("profile is still held")

    with monkeypatch.context() as patch:
        patch.setattr("invisible_playwright_mcp.mcp.owners.shutil.rmtree", refuse_cleanup)
        with pytest.raises(ExceptionGroup, match="idle browser owners"):
            await owners.reap_idle()
        assert owners.entries["A"] is entry
        assert not entry.work.roles() and entry.work.profiles
        assert owners.capacity.used == 1
    await owners.reap_idle()
    assert not owners.entries and owners.capacity.used == 0


async def test_cancelled_queued_call_does_not_prevent_idle_owner_removal(owners):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_list"))
    entry = owners.entries["A"]
    async with entry.lock:
        queued = asyncio.create_task(call("A", "browser_list"))
        await asyncio.sleep(0)
        assert entry.pending_calls == 1
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert entry.pending_calls == 0
    now[0] = 900
    await owners.reap_idle()
    assert not owners.entries


async def test_idle_cleanup_attempts_other_owners_when_one_close_fails(owners):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    b = owners.entries["B"].work._open["main"]
    close = a.close

    async def fail():
        raise RuntimeError("close failed")

    a.close = fail
    now[0] = 900
    try:
        with pytest.raises(ExceptionGroup, match="idle browser owners"):
            await owners.reap_idle()
        assert not a.closed and b.closed
        assert owners.capacity.used == 1
    finally:
        a.close = close


async def test_idle_does_not_interrupt_active_owner_or_block_other_owners(owners, monkeypatch):
    now = [0.0]
    owners.clock = lambda: now[0]
    success(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    b = owners.entries["B"].work._open["main"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def reading(*args, **kwargs):
        entered.set()
        await release.wait()
        return "finished reading"

    monkeypatch.setattr(actions, "read_text", reading)
    task = asyncio.create_task(call("A", "browser_read_text"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        now[0] = 1000
        await owners.reap_idle()
        assert b.closed and not a.closed
        success(await call("B", "browser_open"))
        assert owners.capacity.used == 2
    finally:
        release.set()
    success(await task)
    await owners.reap_idle()
    assert not a.closed


async def test_lifespan_runs_idle_backstop_and_closes_all(owners):
    owners.idle_seconds = 0.02
    async with server._lifespan(server.mcp):
        success(await call("A", "browser_open"))
        a = owners.entries["A"].work._open["main"]
        async with asyncio.timeout(2):
            while not a.closed:
                await asyncio.sleep(0.01)
        success(await call("B", "browser_open"))
        b = owners.entries["B"].work._open["main"]
    assert b.closed and owners.capacity.used == 0
    assert not owners.entries


async def test_cap_refusal_exact_and_no_eviction(owners):
    success(await call("A", "browser_open"))
    success(await call("A", "browser_open", {"browser": "support"}))
    before = list(owners.entries["A"].work._open.values())
    result = await call("B", "browser_open")
    assert result.isError
    assert text(result) == (
        "Browser capacity exhausted (2 of 2 browsers in use across all sessions). "
        "Close one of yours with browser_close, or retry later. No browser was opened.")
    assert all(not session.closed for session in before)
    assert owners.capacity.used == 2
    success(await call("A", "browser_open"))
    assert owners.capacity.used == 2


async def test_concurrent_launches_reserve_before_start(owners):
    gate = asyncio.Event()
    started = []

    class Slow(_Recording):
        async def start(self):
            started.append(self)
            await gate.wait()

    owners.factory = Slow
    tasks = [asyncio.create_task(call(str(i), "browser_open")) for i in range(12)]
    try:
        async with asyncio.timeout(2):
            while len(started) < 2:
                await asyncio.sleep(0)
        assert owners.capacity.used == 2
        await asyncio.sleep(0.01)
        assert len(started) == 2
    finally:
        gate.set()
    results = await asyncio.gather(*tasks)
    assert sum(not result.isError for result in results) == 2
    assert sum(result.isError for result in results) == 10
    assert owners.capacity.used == 2


async def test_closing_slot_is_not_available_until_confirmed(owners):
    closing, release = asyncio.Event(), asyncio.Event()

    class SlowClose(_Recording):
        async def close(self):
            closing.set()
            await release.wait()
            await super().close()

    owners.factory = SlowClose
    success(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    task = asyncio.create_task(call("A", "browser_close"))
    try:
        await asyncio.wait_for(closing.wait(), 2)
        assert (await call("C", "browser_open")).isError
        assert owners.capacity.used == 2
    finally:
        release.set()
    success(await task)
    success(await call("C", "browser_open"))


async def test_same_owner_serializes_open_and_session_end(owners):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(_Recording):
        async def start(self):
            started.set()
            await release.wait()

    owners.factory = Slow
    opening = asyncio.create_task(call("A", "browser_open"))
    await asyncio.wait_for(started.wait(), 2)
    queued = asyncio.create_task(call("A", "browser_open"))
    ending = asyncio.create_task(owners.session_ended("A"))
    await asyncio.sleep(0)
    release.set()
    await opening
    await ending
    assert (await queued).isError
    assert owners.capacity.used == 0
    assert "A" not in owners.entries


@pytest.mark.parametrize("failure", ["start", "constructor", "cancel"])
async def test_failed_launch_cleanup(owners, failure):
    started = asyncio.Event()
    sessions = []

    class Fails(_Recording):
        def __init__(self, **kwargs):
            if failure == "constructor":
                raise RuntimeError("constructor failed")
            super().__init__(**kwargs)
            sessions.append(self)

        async def start(self):
            started.set()
            if failure == "cancel":
                await asyncio.Event().wait()
            raise RuntimeError("start failed")

    owners.factory = Fails
    if failure == "cancel":
        task = asyncio.create_task(call("A", "browser_open"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await call("A", "browser_open")).isError
    assert all(session.closed for session in sessions)
    assert owners.capacity.used == 0
    assert not owners.entries["A"].work.profiles


async def test_failed_close_retains_capacity_and_revokes_handle(owners):
    fail = True

    class FailsClose(_Recording):
        async def close(self):
            if fail:
                raise RuntimeError("close failed")
            await super().close()

    owners.factory = FailsClose
    old = handle(await call("A", "browser_open"))
    assert (await call("A", "browser_close")).isError
    assert owners.capacity.used == 1
    with pytest.raises(ValueError, match="revoked"):
        owners.resolve_handle(old)
    fail = False
    success(await call("A", "browser_close"))
    assert owners.capacity.used == 0


async def test_dead_browser_releases_profile_capacity_and_handle(owners):
    old = handle(await call("A", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    a.dead = True
    assert (await call("A", "browser_status")).isError
    assert a.closed and owners.capacity.used == 0
    with pytest.raises(ValueError, match="revoked"):
        owners.resolve_handle(old)


@pytest.mark.parametrize("name,args", [(n, a) for n, a in EVERY_TOOL if n in HANDLE_TOOLS])
async def test_allowed_handle_tools_reach_only_main(owners, monkeypatch, name, args):
    old = handle(await call("A", "browser_open"))
    success(await call("A", "browser_open", {"browser": "support"}))
    a = owners.entries["A"].work._open["main"]
    touched = []

    async def record(session, *args, **kwargs):
        touched.append(session)
        return "read or typed"

    for action in ("evaluate", "snapshot", "read_html", "read_text", "type_text", "press_key"):
        monkeypatch.setattr(actions, action, record)
    result = await call("B", name, args, meta={**identity("B"), HANDLE_KEY: old})
    assert not result.isError, text(result)
    if name == "browser_status":
        assert [line for line in text(result).splitlines() if line.startswith("fill handle:")] == [
            "fill handle: " + old]
    else:
        assert touched == [a]
    result = await call("B", name, dict(args, browser="support"),
                        meta={**identity("B"), HANDLE_KEY: old})
    assert result.isError and old not in text(result)


@pytest.mark.parametrize("name,valid_handle", [
    ("browser_status", True), ("browser_status", False), ("browser_list", True),
])
async def test_fill_transports_never_allocate_owner_entries(owners, name, valid_handle):
    secret = handle(await call("A", "browser_open"))
    supplied = secret if valid_handle else "bh_" + "X" * 43
    before = dict(owners.entries)
    for index in range(512):
        result = await call(f"fill-{index}", name,
                            meta={**identity(f"fill-{index}"), HANDLE_KEY: supplied})
        assert result.isError == (name != "browser_status" or not valid_handle)
    assert len(owners.entries) == len(before)
    assert owners.entries == before
    assert not owners.ended
    assert owners.capacity.used == 1


@pytest.mark.parametrize("name,args", [
    ("browser_open", {}), ("browser_close", {}), ("browser_list", {}),
    *[(n, a) for n, a in EVERY_TOOL if n not in HANDLE_TOOLS],
])
async def test_handle_disallowed_tools(owners, name, args):
    old = handle(await call("A", "browser_open"))
    result = await call("B", name, args, meta={**identity("B"), HANDLE_KEY: old})
    assert result.isError and old not in text(result)
    assert not owners.entries["A"].work._open["main"].closed


@pytest.mark.parametrize("wrong", [None, "", 123, {}, "bh_" + "X" * 43, "bh_" + "\u00e9" * 43])
async def test_wrong_handle_does_not_echo_or_fall_back(owners, wrong):
    handle(await call("A", "browser_open"))
    success(await call("B", "browser_open"))
    result = await call("B", "browser_status", meta={**identity("B"), HANDLE_KEY: wrong})
    assert result.isError and text(result) == "Invalid or revoked browser fill handle."


async def test_handle_rotates_only_for_main_and_is_revoked_on_close(owners):
    old = handle(await call("A", "browser_open"))
    assert handle(await call("A", "browser_status")) == old
    assert "fill handle" not in success(await call("A", "browser_open", {"browser": "support"}))
    assert "fill handle" not in success(await call("A", "browser_status", {"browser": "support"}))
    new = handle(await call("A", "browser_open"))
    assert new != old
    assert (await call("B", "browser_status", meta={**identity("B"), HANDLE_KEY: old})).isError
    success(await call("A", "browser_close"))
    assert (await call("B", "browser_status", meta={**identity("B"), HANDLE_KEY: new})).isError
    assert len(owners.handles) == 0


async def test_handle_requires_caller_identity_too(owners):
    old = handle(await call("A", "browser_open"))
    result = await call(None, "browser_status", meta={HANDLE_KEY: old})
    assert result.isError and text(result) == IDENTITY_ERROR


async def test_handle_revalidated_after_waiting_on_owner_lock(owners):
    old = handle(await call("A", "browser_open"))
    entry = owners.entries["A"]
    async with entry.lock:
        queued = asyncio.create_task(call(
            "B", "browser_status", meta={**identity("B"), HANDLE_KEY: old}))
        await asyncio.sleep(0)
        await entry.work.open("main")
    result = await queued
    assert result.isError and old not in text(result)


@pytest.mark.parametrize("profile", ["", "named-profile", "/tmp/persistent"])
async def test_persistent_profile_arguments_refused_without_replacing(owners, profile):
    old = handle(await call("A", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    result = await call("A", "browser_open", {"profile": profile})
    assert result.isError and "profile" in text(result)
    assert not a.closed and handle(await call("A", "browser_status")) == old


async def test_profile_is_private_ephemeral_and_never_saved(owners, monkeypatch, tmp_path):
    persistent = tmp_path / "must-not-touch"
    monkeypatch.setenv("STEALTHFOX_PROFILE_DIR", str(persistent))
    store.save("default", {"main": {"profile_dir": str(persistent), "seed": 42}}, focus="main")
    success(await call("A", "browser_open"))
    a = owners.entries["A"].work._open["main"]
    path = Path(a.kwargs["profile_dir"])
    assert path.is_dir() and path.stat().st_mode & 0o777 == 0o700
    assert not persistent.exists()
    assert a.kwargs["seed"] != 42
    (path / "cookies.sqlite").write_text("private")
    success(await call("A", "browser_close"))
    assert not path.exists()
    assert store.load("default")["browsers"]["main"]["profile_dir"] == str(persistent)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "FILE:/tmp/private", " \tfile:///tmp/a",
                                "fi\nle:///tmp/a"])
async def test_file_urls_refused_before_touching_browser(owners, monkeypatch, url):
    success(await call("A", "browser_open"))

    async def forbidden(*args, **kwargs):
        pytest.fail("file URL reached the browser")

    monkeypatch.setattr(actions, "navigate", forbidden)
    result = await call("A", "browser_navigate", {"url": url})
    assert result.isError and "file: URLs are refused" in text(result)


def test_environment_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("STEALTHFOX_OWNER_MODE", raising=False)
    assert Owners.from_env() is None
    monkeypatch.setenv("STEALTHFOX_OWNER_MODE", "mcpd")
    monkeypatch.delenv("STEALTHFOX_MAX_BROWSERS", raising=False)
    monkeypatch.delenv("STEALTHFOX_OWNER_IDLE_SECONDS", raising=False)
    registry = Owners.from_env()
    assert registry.capacity.limit == 2 and registry.idle_seconds == 900
    for name, bad in (("STEALTHFOX_OWNER_MODE", "unknown"),
                      ("STEALTHFOX_MAX_BROWSERS", "0"),
                      ("STEALTHFOX_OWNER_IDLE_SECONDS", "nan")):
        with monkeypatch.context() as patch:
            patch.setenv(name, bad)
            with pytest.raises(ValueError):
                Owners.from_env()


def test_sdk_logging_redacts_handles():
    secret = "bh_" + "x" * 43
    for message in ("Received message: {'stealthfox/browser_handle': '%s'}",
                    "fill handle: %s"):
        record = logging.LogRecord("mcp", logging.DEBUG, "", 0, message, (secret,), None)
        assert CapabilityRedactor().filter(record)
        assert secret not in record.getMessage()


async def test_handle_lookup_uses_constant_time_comparison(owners, monkeypatch):
    import hmac
    secret = handle(await call("A", "browser_open"))
    compared = []
    original = hmac.compare_digest

    def compare(left, right):
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    assert owners.resolve_handle(secret) is owners.entries["A"].work
    assert compared == [(secret, secret)]
    assert list(owners.handles) == [owners.handle_hash(secret)]


async def test_non_owner_mode_ignores_metadata_and_issues_no_handle(owners, monkeypatch):
    from invisible_playwright_mcp.mcp.work import Work
    monkeypatch.setattr(server, "owners", None)
    single = Work("default", factory=_Recording)
    monkeypatch.setattr(server, "work", single)
    try:
        result = await call(None, "browser_open", meta={HANDLE_KEY: "not-a-handle"})
        assert "fill handle" not in success(result)
        assert json.loads(success(await call("B", "browser_list")))["browsers"][0]["id"] == "main"
        success(await call(None, "browser_open", {"browser": "support"}, meta={}))
        for role in ("main", "support"):
            for meta in ({}, {HANDLE_KEY: "bh_" + "X" * 43}):
                status = success(await call(None, "browser_status", {"browser": role}, meta=meta))
                assert not any(line.startswith("fill handle:") for line in status.splitlines())
    finally:
        await single.close_all()


async def test_owner_mode_refuses_direct_http(owners, monkeypatch):
    monkeypatch.setenv("STEALTHFOX_MCP_TRANSPORT", "http")
    with pytest.raises(ValueError, match="trusted stdio"):
        server.main()
