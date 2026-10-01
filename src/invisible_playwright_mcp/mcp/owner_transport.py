"""mcpd's notification extension on the SDK's already-decoded stdio stream."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import traceback
from contextlib import asynccontextmanager
from typing import Literal

import anyio
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from .owners import HANDLE_KEY, owner_id

SESSION_ENDED = "notifications/mcpd/session_ended"


class SessionEndedParams(types.NotificationParams):
    sessionId: str
    reason: str


class SessionEnded(types.Notification[SessionEndedParams, Literal[
        "notifications/mcpd/session_ended"]]):
    pass


class CapabilityRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + "".join(traceback.format_exception(*record.exc_info))
        if HANDLE_KEY in text:
            text = "MCP message containing a browser fill capability (redacted)"
        else:
            text = re.sub(r"bh_[A-Za-z0-9_-]{43}", "[browser fill capability]", text)
        record.msg, record.args = text, ()
        record.exc_info = record.exc_text = None
        return True


def redact_sdk_logs() -> None:
    # These SDK loggers print entire requests, including _meta, at debug level.
    root = logging.getLogger()
    for target in (root, logging.getLogger("mcp.server.lowlevel.server"), *root.handlers):
        if not any(isinstance(f, CapabilityRedactor) for f in target.filters):
            target.addFilter(CapabilityRedactor())


@asynccontextmanager
async def shutdown_on_sigterm():
    if os.name == "nt":
        yield
        return
    async with anyio.create_task_group() as tasks:
        with anyio.open_signal_receiver(signal.SIGTERM) as signals:
            async def stop():
                async for _ in signals:
                    tasks.cancel_scope.cancel()
                    return

            tasks.start_soon(stop)
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()


class _PipeInput(anyio.AsyncFile[str]):
    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__(sys.stdin)
        self.reader = reader

    async def readline(self) -> str:
        return (await self.reader.readline()).decode("utf-8", errors="replace")


@asynccontextmanager
async def owner_stdio():
    if os.name == "nt":
        async with stdio_server() as streams:
            yield streams
        return
    # The SDK's default threaded readline ignores cancellation until another
    # line arrives. A Unix pipe read must be interruptible for SIGTERM cleanup.
    # JSON-RPC parsing remains entirely in the SDK's stdio_server.
    reader = asyncio.StreamReader(limit=sys.maxsize)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        async with stdio_server(stdin=_PipeInput(reader)) as streams:
            yield streams
    finally:
        transport.close()


@asynccontextmanager
async def notifications(read_stream, server):
    """Route only our extension to the registered SDK notification handler.

    ServerSession validates against a closed ClientNotification union and drops
    unknown methods. Intercept after stdio_server's JSON-RPC decoding, before
    that union validation; ordinary messages still use the unmodified SDK.
    """
    send, receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)

    async def forward():
        async with send:
            async for message in read_stream:
                root = None if isinstance(message, Exception) else message.message.root
                if isinstance(root, types.JSONRPCNotification) and root.method == SESSION_ENDED:
                    try:
                        notification = SessionEnded.model_validate(
                            root.model_dump(by_alias=True))
                        owner_id({"mcpd/identity": {"sessionId": notification.params.sessionId}})
                    except (ValidationError, ValueError):
                        logging.getLogger(__name__).warning("Malformed mcpd session-ended notification")
                        continue
                    tasks.start_soon(server._handle_notification, notification)
                else:
                    await send.send(message)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(forward)
        try:
            async with receive:
                yield receive
        finally:
            tasks.cancel_scope.cancel()
