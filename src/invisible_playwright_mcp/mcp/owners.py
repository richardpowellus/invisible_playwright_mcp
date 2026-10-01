"""Trusted mcpd callers, ephemeral browsers, and generation-bound fill capabilities."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from . import DEFAULT_BROWSER_ID, GONE
from .session import StealthSession
from .work import Work
from ..engine import Engine

logger = logging.getLogger(__name__)
HANDLE_KEY = "stealthfox/browser_handle"
HANDLE_TOOLS = frozenset({
    "browser_status", "browser_evaluate", "browser_snapshot", "browser_read_html",
    "browser_read_text", "browser_type", "browser_press_key",
})
_HANDLE = re.compile(r"bh_[A-Za-z0-9_-]{43}")
IDENTITY_ERROR = "Owner mode requires a valid mcpd/identity sessionId."
HANDLE_ERROR = "Invalid or revoked browser fill handle."


class CapacityExhausted(RuntimeError):
    pass


def owner_id(meta: dict) -> str:
    identity = meta.get("mcpd/identity")
    value = identity.get("sessionId") if isinstance(identity, dict) else None
    if (not isinstance(value, str) or not value or value != value.strip()
            or not value.isprintable()):
        raise ValueError(IDENTITY_ERROR)
    return value


class Capacity:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("STEALTHFOX_MAX_BROWSERS must be a positive integer.")
        self.limit = limit
        self.used = 0
        self.lock = asyncio.Lock()

    async def reserve(self) -> None:
        async with self.lock:
            if self.used >= self.limit:
                raise CapacityExhausted(
                    "Browser capacity exhausted (%d of %d browsers in use across all "
                    "sessions). Close one of yours with browser_close, or retry later. "
                    "No browser was opened." % (self.limit, self.limit))
            self.used += 1

    async def release(self) -> None:
        async with self.lock:
            self.used -= 1


class OwnerWork(Work):
    def __init__(self, registry: Owners, *, factory: Callable[..., StealthSession],
                 engine: Engine | None) -> None:
        super().__init__("", factory=factory, engine=engine)
        self.registry = registry
        self.profiles: dict[str, Path] = {}
        self.dead: set[str] = set()
        self.fill_handle: str | None = None

    def remembered(self) -> None:
        return None

    def remember(self) -> None:
        # Owner profiles and identities never enter the process-global store.
        return None

    async def open(self, role: str, *, seed: int | None = None,
                   proxy: str | None = None, profile: str | None = None) -> str:
        if profile is not None:
            raise ValueError("Persistent profile arguments are refused in owner mode; "
                             "leave profile out for an ephemeral browser.")
        result = await super().open(role, seed=seed, proxy=proxy, profile="")
        if role == DEFAULT_BROWSER_ID and role in self._open:
            if self.fill_handle is None:
                self.fill_handle = "bh_" + secrets.token_urlsafe(32)
                self.registry.handles[self.registry.handle_hash(self.fill_handle)] = self
            result += "\nfill handle: " + self.fill_handle
        return result

    async def _start(self, role: str, settings: dict) -> StealthSession:
        await self.registry.capacity.reserve()
        try:
            directory = Path(tempfile.mkdtemp(prefix="stealthfox-owner-"))
        except BaseException:
            await self.registry.capacity.release()
            raise
        self.profiles[role] = directory
        try:
            directory.chmod(0o700)
            settings["profile_dir"] = str(directory)
            session = self._factory(**settings)
            # Retain even a failed launch until its close has succeeded.
            self._open[role] = session
            await session.start()
        except BaseException:
            self.dead.add(role)
            with anyio.CancelScope(shield=True):
                await self._drop(role)
            raise
        return session

    def revoke(self) -> None:
        if self.fill_handle is not None:
            self.registry.handles.pop(self.registry.handle_hash(self.fill_handle), None)
            self.fill_handle = None

    async def _drop(self, role: str) -> None:
        self.dead.add(role)
        if role == DEFAULT_BROWSER_ID:
            self.revoke()
        session = self._open.get(role)
        if session is not None:
            await session.close()
            self._open.pop(role, None)
        self._launched.pop(role, None)
        directory = self.profiles.get(role)
        if directory is not None:
            # Only directories made by this Work are eligible for removal.
            shutil.rmtree(directory)
            self.profiles.pop(role)
            await self.registry.capacity.release()
        self.dead.discard(role)

    def gone(self, role: str) -> RuntimeError:
        self.dead.add(role)
        if role == DEFAULT_BROWSER_ID:
            self.revoke()
        return RuntimeError(GONE % role)

    def session(self, role: str) -> StealthSession:
        if role in self.dead:
            raise RuntimeError(GONE % role)
        return super().session(role)

    async def reap_dead(self) -> None:
        for role in list(self.dead):
            await self._drop(role)

    async def close_all(self) -> None:
        errors = []
        for role in set(self._open) | set(self.profiles):
            try:
                await self._drop(role)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Could not close all owner browsers", errors)

    async def status(self, role: str) -> str:
        result = await super().status(role)
        if role == DEFAULT_BROWSER_ID and self.fill_handle is not None:
            result += "\nfill handle: " + self.fill_handle
        return result


@dataclass
class Owner:
    work: OwnerWork
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ended: bool = False


class Owners:
    def __init__(self, *, limit: int = 2, idle_seconds: float = 900.0,
                 factory: Callable[..., StealthSession] = StealthSession,
                 engine: Engine | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise ValueError("STEALTHFOX_OWNER_IDLE_SECONDS must be positive and finite.")
        self.capacity = Capacity(limit)
        self.idle_seconds = idle_seconds
        self.factory = factory
        self.engine = engine
        self.clock = clock
        self.entries: dict[str, Owner] = {}
        self.handles: dict[bytes, OwnerWork] = {}
        self.ended: set[str] = set()
        self.stopping = asyncio.Event()

    @classmethod
    def from_env(cls, *, engine: Engine | None = None) -> Owners | None:
        mode = os.environ.get("STEALTHFOX_OWNER_MODE", "")
        if not mode:
            return None
        if mode != "mcpd":
            raise ValueError("STEALTHFOX_OWNER_MODE must be mcpd or unset.")
        return cls(limit=int(os.environ.get("STEALTHFOX_MAX_BROWSERS", "2")),
                   idle_seconds=float(os.environ.get("STEALTHFOX_OWNER_IDLE_SECONDS", "900")),
                   engine=engine)

    @staticmethod
    def handle_hash(handle: str) -> bytes:
        return hashlib.sha256(handle.encode("ascii")).digest()

    def resolve_handle(self, handle: object) -> OwnerWork:
        if not isinstance(handle, str) or _HANDLE.fullmatch(handle) is None:
            raise ValueError(HANDLE_ERROR)
        work = self.handles.get(self.handle_hash(handle))
        if work is None or not hmac.compare_digest(work.fill_handle or "", handle):
            raise ValueError(HANDLE_ERROR)
        return work

    def caller(self, identity: str) -> Owner:
        if self.stopping.is_set() or identity in self.ended:
            raise ValueError("This browser owner session has ended.")
        if identity not in self.entries:
            self.entries[identity] = Owner(
                OwnerWork(self, factory=self.factory, engine=self.engine), self.clock())
        return self.entries[identity]

    @asynccontextmanager
    async def target(self, meta: dict, tool: str, arguments: dict) -> AsyncIterator[OwnerWork]:
        caller = self.caller(owner_id(meta))
        caller.last_used = self.clock()
        target = caller
        handle = meta.get(HANDLE_KEY)
        if HANDLE_KEY in meta:
            if tool not in HANDLE_TOOLS or arguments.get("browser") not in (None, "main"):
                raise ValueError("This tool or browser is not permitted with a fill handle.")
            work = self.resolve_handle(handle)
            target = next(entry for entry in self.entries.values() if entry.work is work)
        async with target.lock:
            if self.stopping.is_set() or target.ended or caller.ended:
                raise ValueError("This browser owner session has ended.")
            # Closing/reopening may have happened while this call queued.
            if HANDLE_KEY in meta and self.resolve_handle(handle) is not target.work:
                raise ValueError(HANDLE_ERROR)
            target.last_used = self.clock()
            if tool == "browser_navigate":
                url = arguments.get("url")
                if isinstance(url, str) and urlsplit(url).scheme.lower() == "file":
                    raise ValueError("file: URLs are refused in owner mode.")
            try:
                yield target.work
            finally:
                with anyio.CancelScope(shield=True):
                    await target.work.reap_dead()
                target.last_used = self.clock()

    async def session_ended(self, identity: str) -> None:
        entry = self.entries.get(identity)
        if entry is None:
            return
        self.ended.add(identity)
        entry.ended = True
        entry.work.revoke()
        async with entry.lock:
            await entry.work.close_all()
            self.entries.pop(identity, None)

    async def reap_idle(self) -> None:
        errors = []
        for identity, entry in list(self.entries.items()):
            if entry.lock.locked():
                continue
            async with entry.lock:
                if entry.ended or self.clock() - entry.last_used >= self.idle_seconds:
                    try:
                        await entry.work.close_all()
                    except Exception as exc:
                        errors.append(exc)
                        continue
                    # Idle sessions may open again; retain the same lock for queued calls.
                    if entry.ended:
                        self.entries.pop(identity, None)
        if errors:
            raise ExceptionGroup("Could not close all idle browser owners", errors)

    async def idle_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), min(30, self.idle_seconds / 2))
            except TimeoutError:
                try:
                    await self.reap_idle()
                except Exception:
                    logger.exception("Failed to close idle owner browsers; capacity retained")

    async def close_all(self) -> None:
        self.stopping.set()
        results = await asyncio.gather(
            *(self.session_ended(identity) for identity in list(self.entries)),
            return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Could not close all browser owners", errors)
