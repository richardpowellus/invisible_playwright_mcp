"""Real stdio and real SDK dispatch, with only Firefox replaced by a fake."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from _stdio_helpers import subprocess_env
from test_owners import HANDLE_KEY, identity


@pytest.mark.parametrize("shutdown", [
    "stdin",
    pytest.param("sigterm", marks=pytest.mark.skipif(os.name == "nt", reason="Unix signal")),
])
async def test_stdio_metadata_notification_shutdown_and_log_redaction(tmp_path, shutdown):
    code = """
import logging
from invisible_playwright_mcp.mcp import server
from test_open_first import _Recording
server.owners.factory = _Recording
server.owners.engine = None
server.engine.start = lambda: None
logging.basicConfig(level=logging.DEBUG)
logging.getLogger().setLevel(logging.DEBUG)
server.main()
"""
    env = subprocess_env({
        "STEALTHFOX_OWNER_MODE": "mcpd",
        "STEALTHFOX_MAX_BROWSERS": "1",
        "STEALTHFOX_OWNER_IDLE_SECONDS": "900",
        "TMPDIR": str(tmp_path),
    })
    env["PYTHONPATH"] += os.pathsep + str(Path(__file__).parent)
    log = tmp_path / "stderr.log"
    with log.open("w") as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr)
        sequence = 0

        async def send(method, params=None, *, notification=False, expect_error=False):
            nonlocal sequence
            message = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            if not notification:
                sequence += 1
                message["id"] = sequence
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            if notification:
                return None
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 10)
                assert line, log.read_text()
                result = json.loads(line)
                if result.get("id") == sequence:
                    if expect_error:
                        assert "error" in result, result
                        return result["error"]
                    assert "result" in result, result
                    return result["result"]

        async def call(owner, name, *, meta=None, arguments=None):
            return await send("tools/call", {
                "name": name, "arguments": arguments or {},
                "_meta": identity(owner) if meta is None else meta,
            })

        def text(result):
            return "\n".join(item["text"] for item in result["content"] if item["type"] == "text")

        try:
            init = await send("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "owner-isolation-test", "version": "1"},
            })
            # A credential filler trusts this, never tool output, as proof
            # that callers are isolated.
            assert init["capabilities"]["experimental"]["stealthfox/owner-isolation"] == {"version": 1}
            await send("notifications/initialized", notification=True)
            missing = await call(None, "browser_open", meta={})
            assert missing["isError"]
            a = await call("A", "browser_open")
            assert not a["isError"], a
            handle = text(a).split("fill handle: ")[1].strip()
            profiles_a = set(tmp_path.glob("stealthfox-owner-*"))
            assert len(profiles_a) == 1
            assert json.loads(text(await call("B", "browser_list")))["browsers"] == []
            assert (await call("B", "browser_status"))["isError"]
            delegated = await call("B", "browser_status", meta={**identity("B"), HANDLE_KEY: handle})
            assert not delegated["isError"]
            assert text(delegated).rstrip().splitlines()[-1] == "fill handle: " + handle
            malformed = await send("tools/call", {
                "name": "browser_status", "arguments": 123,
                "_meta": {**identity("B"), HANDLE_KEY: handle},
            }, expect_error=True)
            assert malformed["code"] == -32602
            assert (await call("B", "browser_open"))["isError"]

            await send("notifications/mcpd/session_ended",
                       {"sessionId": 123, "reason": "delete"}, notification=True)
            await send("notifications/mcpd/session_ended",
                       {"sessionId": "unknown", "reason": "delete"}, notification=True)
            assert not (await call("A", "browser_status"))["isError"]
            await send("notifications/mcpd/session_ended",
                       {"sessionId": "A", "reason": "delete"}, notification=True)
            b = await call("B", "browser_open")
            assert not b["isError"], b
            assert not any(path.exists() for path in profiles_a)
            stale = await call("B", "browser_status", meta={**identity("B"), HANDLE_KEY: handle})
            assert stale["isError"] and handle not in text(stale)
            profiles_b = set(tmp_path.glob("stealthfox-owner-*"))
            assert len(profiles_b) == 1
            if shutdown == "stdin":
                process.stdin.close()
            else:
                process.terminate()
            assert await asyncio.wait_for(process.wait(), 10) == 0, log.read_text()
            assert not any(path.exists() for path in profiles_b)
            logged = log.read_text()
            assert handle not in logged
            assert "containing a browser fill capability (redacted)" in logged
            assert "Failed to validate notification" not in logged
            assert "Malformed mcpd session-ended notification" in logged
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
