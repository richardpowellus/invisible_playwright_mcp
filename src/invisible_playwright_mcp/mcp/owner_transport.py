"""mcpd's notification extension on the SDK's already-decoded stdio stream."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import select
import signal
import sys
import threading
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
HARD_EXIT_SECONDS = 3.5
HARD_EXIT_CLEANUP_SECONDS = 1.0


class ExitWatchdog:
    """Last-resort process exit, independent of the browser's asyncio loop."""

    def __init__(self, registry) -> None:
        self.registry = registry
        self.loop = asyncio.get_running_loop()
        self.done = threading.Event()
        self.thread: threading.Thread | None = None
        self.input_thread: threading.Thread | None = None
        self.lock = threading.RLock()

    def arm(self) -> None:
        with self.lock:
            if self.thread is not None:
                return
            self.loop.call_soon_threadsafe(self.registry.stopping.set)
            self.thread = threading.Thread(target=self._wait, name="owner-exit-watchdog", daemon=True)
            self.thread.start()

    def watch_stdin(self) -> None:
        # Observe hangup without reading or parsing any protocol bytes. This
        # still runs if an idle browser close has blocked the asyncio thread.
        fd = sys.stdin.fileno()

        def watch():
            poll = select.poll()
            poll.register(fd, select.POLLHUP | select.POLLERR)
            while not self.done.is_set():
                if poll.poll(50):
                    self.arm()
                    return

        self.input_thread = threading.Thread(target=watch, name="owner-stdin-watchdog", daemon=True)
        self.input_thread.start()

    def _wait(self) -> None:
        if self.done.wait(HARD_EXIT_SECONDS):
            return
        # Async timeouts cannot interrupt synchronous engine process reaping.
        # A to_thread close would use loop-bound objects from the wrong loop
        # and default-executor shutdown would still wait for blocked threads.
        # Neither a broken or full stderr pipe nor a cleanup stuck behind the
        # instances lock may keep the process alive: cleanup runs on its own
        # daemon thread with a bounded join, and exit follows unconditionally.
        # Anything left is removed by the next start's sweep once this
        # process's instance locks are released by its death.
        try:
            cleanup = threading.Thread(target=self._last_cleanup, name="owner-exit-cleanup",
                                       daemon=True)
            cleanup.start()
            cleanup.join(HARD_EXIT_CLEANUP_SECONDS)
        finally:
            os._exit(1)

    def _last_cleanup(self) -> None:
        # The diagnostic is written here, inside the bounded join, so a full
        # stderr pipe cannot hold the exit and a broken one cannot skip it.
        try:
            os.write(2, b"Owner browser shutdown exceeded deadline; removing instances and exiting\n")
        except BaseException:
            pass
        try:
            self.registry.instances.close()
        except BaseException:
            pass

    def finish(self) -> None:
        self.done.set()
        if self.input_thread is not None:
            self.input_thread.join()
        if self.thread is not None:
            self.thread.join()


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
async def shutdown_on_sigterm(on_shutdown=lambda: None):
    if os.name == "nt":
        yield
        return
    async with anyio.create_task_group() as tasks:
        def stop(signum, frame):
            # A Python signal handler can arm the independent deadline while
            # synchronous teardown is blocking the event loop.
            on_shutdown()
            tasks.cancel_scope.cancel()

        previous = signal.signal(signal.SIGTERM, stop)
        try:
            yield
        finally:
            signal.signal(signal.SIGTERM, previous)
            tasks.cancel_scope.cancel()


class _PipeInput(anyio.AsyncFile[str]):
    def __init__(self, reader: asyncio.StreamReader, on_shutdown) -> None:
        super().__init__(sys.stdin)
        self.reader = reader
        self.on_shutdown = on_shutdown

    async def readline(self) -> str:
        line = await self.reader.readline()
        if not line:
            self.on_shutdown()
        return line.decode("utf-8", errors="replace")


@asynccontextmanager
async def owner_stdio(on_shutdown=lambda: None):
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
        async with stdio_server(stdin=_PipeInput(reader, on_shutdown)) as streams:
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
