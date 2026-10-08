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
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from invisible_playwright.async_api import TargetClosedError

from . import DEFAULT_BROWSER_ID, GONE, actions, masked, plan
from .session import StealthSession
from .owner_instances import Instances
from .work import Work
from ..engine import Engine

logger = logging.getLogger(__name__)
HANDLE_KEY = "stealthfox/browser_handle"
HANDLE_TOOLS = frozenset({
    "browser_status", "browser_evaluate", "browser_snapshot", "browser_read_html",
    "browser_read_text", "browser_type", "browser_press_key",
    "browser_take_screenshot",
})
_HANDLE = re.compile(r"bh_[A-Za-z0-9_-]{43}")
IDENTITY_ERROR = "Owner mode requires a valid mcpd/identity sessionId."
HANDLE_ERROR = "Invalid or revoked browser fill handle."
MAX_ENDED_SESSIONS = 4096
EXIT_CLOSE_SECONDS = 3.0
#: How long the sweep waits for a browser it believes has exited to say why.
EXITED_ROUND_TRIP_SECONDS = 5.0
_delegated_call: ContextVar[bool] = ContextVar("browser_fill_delegated", default=False)


class CapacityExhausted(RuntimeError):
    pass


async def _close_all(*calls: Awaitable[None]) -> None:
    results = await asyncio.gather(*calls, return_exceptions=True)
    errors = []
    for result in results:
        # gather returns CancelledError as a value; it is NOT an Exception.
        if isinstance(result, asyncio.CancelledError):
            error = RuntimeError("Browser close was cancelled")
            error.__cause__ = result
            errors.append(error)
        elif isinstance(result, Exception):
            errors.append(result)
    if errors:
        raise ExceptionGroup("Could not close all owner browsers", errors)


def _remove_directory(directory: Path) -> None:
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        shutil.rmtree(directory)


def profile_in_use(directory: Path) -> bool | None:
    """Whether a live process runs Firefox on this profile, read from /proc.

    ⛔ ASKED OF THE OPERATING SYSTEM, BECAUSE THE SESSION CANNOT ANSWER IT.
    `StealthSession.is_usable` goes on saying "connected" for seconds after the
    engine is killed, and only a round trip notices, so a browser that died
    while its owner was not calling held a capacity slot until that owner
    called again or the idle reap ran: 2026-10-07, 16 minutes of "Browser
    capacity exhausted". Firefox is launched with `-profile <dir>` and every
    owner profile is private, so the argv names the browser exactly. A dead
    child that has not been reaped yet is a zombie with an empty cmdline, and
    counts as gone. None means /proc could not be read: nothing is dropped on
    an answer nobody could give.
    """
    proc = Path("/proc")
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    wanted = {os.fsencode(str(directory))}
    try:
        wanted.add(os.fsencode(str(directory.resolve())))
    except OSError:
        pass
    for name in names:
        if not name.isdigit():
            continue
        try:
            argv = (proc / name / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for flag, value in zip(argv, argv[1:]):
            if flag == b"-profile" and value in wanted:
                return True
    return False


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
        self._identities: dict[str, dict] = {}
        #: Browsers the sweep dropped because their Firefox exited: the
        #: owner's next call is told GONE once, not "not open".
        self.lost: set[str] = set()
        self.fill_handle: str | None = None
        self.upload_dir: Path | None = None
        self.download_dir: Path | None = None
        self.owner = ""

    def owner_label(self) -> str:
        return self.owner or "(unknown)"

    def remembered(self, role: str = DEFAULT_BROWSER_ID) -> dict | None:
        # Owner identities are kept in memory per role until the owner is
        # reaped, never in the process-global store or on disk.
        return self._identities.get(role)

    def remember(self, role: str = DEFAULT_BROWSER_ID) -> None:
        launched = self._launched.get(role)
        if launched is not None:
            self._identities[role] = {
                key: launched[key] for key in ("seed", "proxy") if key in launched}

    def _plan_session(self, **kwargs) -> plan.SessionPlan:
        # Suppress the environment's profile AFTER deciding whether the caller
        # asked for a new identity; the private profile is allocated in _start.
        return super()._plan_session(**dict(kwargs, profile=""))

    async def open(self, role: str, *, seed: int | None = None,
                   proxy: str | None = None, profile: str | None = None,
                   accept_lan_certs: list[str] | None = None) -> str:
        if profile is not None:
            raise ValueError("Persistent profile arguments are refused in owner mode; "
                             "leave profile out for an ephemeral browser.")
        self.lost.discard(role)
        result = await super().open(role, seed=seed, proxy=proxy, profile=profile,
                                    accept_lan_certs=accept_lan_certs)
        if self.upload_dir is not None:
            result += "\nupload dir: " + str(self.upload_dir)
        if self.download_dir is not None:
            result += "\ndownload dir: " + str(self.download_dir)
        if role == DEFAULT_BROWSER_ID and role in self._open:
            if self.fill_handle is None:
                self.fill_handle = "bh_" + secrets.token_urlsafe(32)
                self.registry.handles[self.registry.handle_hash(self.fill_handle)] = self
            result += "\nfill handle: " + self.fill_handle
        return result

    def _check_cert_profile(self, settings: dict) -> None:
        # _start allocates the private owner profile before StealthSession.start.
        pass

    def _ensure_upload_dir(self) -> None:
        try:
            roots = actions.upload_dirs({
                actions.UPLOAD_DIRS_ENV: os.pathsep.join(self.registry.upload_roots)})
        except actions.UploadDirectoryError:
            raise actions.UploadDirectoryError(
                "uploads are off: the configured staging root is unavailable.") from None
        if not roots:
            return
        if self.upload_dir is not None and not os.path.lexists(self.upload_dir):
            # Removed from outside (its instance swept by a cleanup): allocate
            # a new private directory. A path that still exists but was
            # REPLACED is not reallocated; _upload_env refuses it below.
            self.upload_dir = None
        if self.upload_dir is None:
            directory = self.registry.instances.directory(Path(roots[0])) / (
                "owner-" + secrets.token_urlsafe(24))
            directory.mkdir(mode=0o700)
            self.upload_dir = directory
            directory.chmod(0o700)
        self._upload_env()

    def _upload_env(self) -> dict[str, str]:
        if self.upload_dir is None:
            raise RuntimeError(
                "uploads are off: no private upload directory is configured for this owner.")
        env = {actions.UPLOAD_DIRS_ENV: str(self.upload_dir)}
        try:
            actions.upload_dirs(env)
        except actions.UploadDirectoryError:
            raise RuntimeError("uploads are off: your upload directory is unavailable.") from None
        return env

    def remove_upload_dir(self) -> None:
        if self.upload_dir is not None:
            if not os.path.lexists(self.upload_dir):
                self.upload_dir = None
                return
            # Never traverse a replaced staging root or follow a directory link.
            try:
                actions.upload_dirs({actions.UPLOAD_DIRS_ENV: str(self.upload_dir.parent)})
            except actions.UploadDirectoryError:
                raise actions.UploadDirectoryError(
                    "Cannot remove the upload directory: its staging root is unavailable.") from None
            _remove_directory(self.upload_dir)
            self.upload_dir = None

    async def _upload_files(self, session, selector: str, paths) -> str:
        env = self._upload_env()
        try:
            return await actions.upload_files(session, selector, paths, env=env,
                                               snapshot_root=self.upload_dir)
        except actions.UploadDirectoryError:
            raise RuntimeError("uploads are off: your upload directory is unavailable.") from None
        except PermissionError:
            raise PermissionError(
                "Upload refused. Copy the file into your upload dir: %s" % self.upload_dir) from None

    def _ensure_download_dir(self) -> None:
        try:
            roots = actions.download_dirs({
                actions.DOWNLOAD_DIRS_ENV: os.pathsep.join(self.registry.download_roots)})
        except actions.DownloadDirectoryError:
            raise actions.DownloadDirectoryError(
                "downloads are off: the configured download root is unavailable.") from None
        if not roots:
            return
        if self.download_dir is not None and not os.path.lexists(self.download_dir):
            # Removed from outside (its instance swept by a cleanup): allocate
            # a new private directory. A path that still exists but was
            # REPLACED is not reallocated; _download_env refuses it below.
            self.download_dir = None
        if self.download_dir is None:
            directory = self.registry.instances.directory(Path(roots[0])) / (
                "owner-" + secrets.token_urlsafe(24))
            directory.mkdir(mode=0o700)
            self.download_dir = directory
            directory.chmod(0o700)
        self._download_env()

    def _download_env(self) -> dict[str, str]:
        if self.download_dir is None:
            raise RuntimeError(
                "downloads are off: no private download directory is configured for this owner.")
        env = {actions.DOWNLOAD_DIRS_ENV: str(self.download_dir)}
        try:
            actions.download_dirs(env)
        except actions.DownloadDirectoryError:
            raise RuntimeError("downloads are off: your download directory is unavailable.") from None
        return env

    def remove_download_dir(self) -> None:
        if self.download_dir is not None:
            if not os.path.lexists(self.download_dir):
                self.download_dir = None
                return
            try:
                actions.download_dirs({actions.DOWNLOAD_DIRS_ENV: str(self.download_dir.parent)})
            except actions.DownloadDirectoryError:
                raise actions.DownloadDirectoryError(
                    "Cannot remove the download directory: its root is unavailable.") from None
            _remove_directory(self.download_dir)
            self.download_dir = None

    def remove_file_dirs(self) -> None:
        errors = []
        for remove in (self.remove_upload_dir, self.remove_download_dir):
            try:
                remove()
            except (OSError, actions.UploadDirectoryError, actions.DownloadDirectoryError) as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Could not remove owner file directories", errors)

    async def _download(self, session, *args, **kwargs) -> str:
        env = self._download_env()
        try:
            return await actions.download(session, *args, **kwargs, env=env)
        except actions.DownloadDirectoryError:
            raise RuntimeError("downloads are off: your download directory is unavailable.") from None
        except PermissionError:
            raise PermissionError(
                "Download refused. Use only your download dir: %s" % self.download_dir) from None

    async def _act(self, at: str, fn, *args, **kwargs):
        if fn is actions.upload_files:
            fn = self._upload_files
        elif fn is actions.download:
            fn = self._download
        return await super()._act(at, fn, *args, **kwargs)

    async def _start(self, role: str, settings: dict) -> StealthSession:
        await self.registry.capacity.reserve()
        try:
            directory = Path(tempfile.mkdtemp(
                prefix="stealthfox-owner-",
                dir=self.registry.instances.directory(Path(tempfile.gettempdir()).resolve())))
        except BaseException:
            await self.registry.capacity.release()
            raise
        self.profiles[role] = directory
        try:
            directory.chmod(0o700)
            self._ensure_upload_dir()
            self._ensure_download_dir()
            settings["profile_dir"] = str(directory)
            # Even with browser_download disabled, spontaneous downloads stay
            # inside this owner's ephemeral profile, never a shared temp pool.
            settings["download_root"] = str(self.download_dir or directory)
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
        rec = self._typing.pop(role, None)
        if rec is not None:
            rec.task.cancel()
            try:
                await rec.task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background typing failed while its owner browser was closing")
        self.dead.add(role)
        self.lost.discard(role)
        if role == DEFAULT_BROWSER_ID:
            self.revoke()
        session = self._open.get(role)
        if session is not None:
            await session.close()
            masked.forget(session)
            self._open.pop(role, None)
        self._launched.pop(role, None)
        directory = self.profiles.get(role)
        if directory is not None:
            # Only directories made by this Work are eligible for removal.
            _remove_directory(directory)
            self.profiles.pop(role)
            await self.registry.capacity.release()
        self.dead.discard(role)

    def gone(self, role: str, cause: BaseException | None = None) -> RuntimeError:
        self.report_gone(role, cause)
        self.dead.add(role)
        if role == DEFAULT_BROWSER_ID:
            self.revoke()
        return RuntimeError(GONE % role)

    def session(self, role: str) -> StealthSession:
        if role in self.dead:
            raise RuntimeError(GONE % role)
        if role in self.lost and role not in self._open:
            self.lost.discard(role)
            raise RuntimeError(GONE % role)
        return super().session(role)

    async def exited(self, role: str) -> BaseException | None:
        """The closed-target error of a browser whose process has exited, or
        None while it is (or may be) alive.

        Two independent observations, both required: /proc shows no process
        on the private profile, AND a round trip raises a closed target. The
        second (`StealthSession.round_trip`) reads the browser's cookie jar and
        touches no page; it is what
        carries the exit code and Firefox's last output for the journal. A
        browser that answers is alive whatever /proc said, and is kept.
        """
        directory = self.profiles.get(role)
        session = self._open.get(role)
        if directory is None or session is None:
            return None
        if await asyncio.to_thread(self.registry.process_probe, directory) is not False:
            return None
        try:
            async with asyncio.timeout(EXITED_ROUND_TRIP_SECONDS):
                await session.round_trip()
        except TargetClosedError as closed:
            return closed
        except Exception:
            return None
        return None

    async def reap_dead(self) -> None:
        for role in list(self.dead):
            await self._drop(role)

    async def close_all(self) -> None:
        await _close_all(*(self._drop(role) for role in set(self._open) | set(self.profiles)))

    async def status(self, role: str) -> str:
        result = await super().status(role)
        if self.upload_dir is not None and not _delegated_call.get():
            result += "\nupload dir: " + str(self.upload_dir)
        if self.download_dir is not None and not _delegated_call.get():
            result += "\ndownload dir: " + str(self.download_dir)
        if role == DEFAULT_BROWSER_ID and self.fill_handle is not None:
            result += "\nfill handle: " + self.fill_handle
        return result


@dataclass
class Owner:
    work: OwnerWork
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ended: bool = False
    pending_calls: int = 0


class Owners:
    def __init__(self, *, limit: int = 2, idle_seconds: float = 900.0,
                 factory: Callable[..., StealthSession] = StealthSession,
                 engine: Engine | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 process_probe: Callable[[Path], bool | None] = profile_in_use) -> None:
        if os.name != "posix":
            raise RuntimeError("Owner mode requires POSIX filesystem locks")
        if not math.isfinite(idle_seconds) or idle_seconds <= 0:
            raise ValueError("STEALTHFOX_OWNER_IDLE_SECONDS must be positive and finite.")
        self.capacity = Capacity(limit)
        self.idle_seconds = idle_seconds
        self.factory = factory
        self.engine = engine
        self.clock = clock
        self.process_probe = process_probe
        self.upload_roots = actions.upload_dirs()
        self.download_roots = actions.download_dirs()
        profile_root = Path(tempfile.gettempdir()).resolve()
        if any(profile_root.is_relative_to(root) for root in self.upload_roots):
            raise ValueError(
                "Owner mode refuses upload roots containing the profile temp directory; "
                "configure a separate upload staging directory.")
        if any(profile_root.is_relative_to(root) for root in self.download_roots):
            raise ValueError(
                "Owner mode refuses download roots containing the profile temp directory; "
                "configure a separate download directory.")
        self.entries: dict[str, Owner] = {}
        self.handles: dict[bytes, OwnerWork] = {}
        self.ended: OrderedDict[str, None] = OrderedDict()
        self.stopping = asyncio.Event()
        self.instances = Instances()

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

    def remove_stale_dirs(self) -> None:
        # Hot reload initializes the candidate BEFORE stopping the old worker.
        # Locks, not prefixes or PIDs, prove that a generation can be removed.
        if self.entries:
            raise RuntimeError("Stale owner cleanup must run before accepting calls.")
        roots = actions.upload_dirs({
            actions.UPLOAD_DIRS_ENV: os.pathsep.join(self.upload_roots)})
        locations = [Path(tempfile.gettempdir()).resolve()]
        if roots:
            locations.append(Path(roots[0]))
        downloads = actions.download_dirs({
            actions.DOWNLOAD_DIRS_ENV: os.pathsep.join(self.download_roots)})
        if downloads:
            locations.append(Path(downloads[0]))
        self.instances.sweep(locations)
        for root in locations:
            self.instances.directory(root)

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
            work = OwnerWork(self, factory=self.factory, engine=self.engine)
            work.owner = identity
            self.entries[identity] = Owner(work, self.clock())
        return self.entries[identity]

    def redaction_windows(self, meta: dict) -> set[str]:
        """Include policy refusals that happen before target() can yield."""
        identity = owner_id(meta)
        if HANDLE_KEY in meta:
            try:
                return masked.result_windows(self.resolve_handle(meta[HANDLE_KEY]))
            except ValueError:
                # target() still reports the original policy/handle refusal.
                pass
        entry = self.entries.get(identity)
        return masked.result_windows(entry.work) if entry is not None else set()

    @asynccontextmanager
    async def target(self, meta: dict, tool: str, arguments: dict) -> AsyncIterator[OwnerWork]:
        identity = owner_id(meta)
        handle = meta.get(HANDLE_KEY)
        if HANDLE_KEY in meta:
            if tool not in HANDLE_TOOLS or arguments.get("browser") not in (None, "main"):
                raise ValueError("This tool or browser is not permitted with a fill handle.")
            work = self.resolve_handle(handle)
            target = next(entry for entry in self.entries.values() if entry.work is work)
        else:
            target = self.caller(identity)
        target.last_used = self.clock()
        # A released lock can still have queued callers holding this entry.
        target.pending_calls += 1
        try:
            async with target.lock:
                if self.stopping.is_set() or target.ended or identity in self.ended:
                    raise ValueError("This browser owner session has ended.")
                # Closing/reopening may have happened while this call queued.
                if HANDLE_KEY in meta and self.resolve_handle(handle) is not target.work:
                    raise ValueError(HANDLE_ERROR)
                target.last_used = self.clock()
                if tool == "browser_navigate":
                    url = arguments.get("url")
                    if isinstance(url, str) and urlsplit(url).scheme.lower() == "file":
                        raise ValueError("file: URLs are refused in owner mode.")
                token = _delegated_call.set(HANDLE_KEY in meta)
                try:
                    yield target.work
                finally:
                    _delegated_call.reset(token)
                    with anyio.CancelScope(shield=True):
                        await target.work.reap_dead()
                    target.last_used = self.clock()
        finally:
            target.pending_calls -= 1

    async def session_ended(self, identity: str) -> None:
        entry = self.entries.get(identity)
        if entry is None:
            return
        # mcpd never routes calls for ended sessions; these bounded tombstones
        # are only defence in depth, not the transport's authentication boundary.
        self.ended[identity] = None
        if len(self.ended) > MAX_ENDED_SESSIONS:
            self.ended.popitem(last=False)
        entry.ended = True
        entry.work.revoke()
        async with entry.lock:
            try:
                await entry.work.close_all()
            except Exception as exc:
                raise RuntimeError(masked.redact_text(
                    str(exc), masked.result_windows(entry.work))) from None
            finally:
                for session in entry.work._open.values():
                    masked.forget(session)
            entry.work.remove_file_dirs()
            self.entries.pop(identity, None)

    async def reap_idle(self) -> None:
        errors = []
        for identity, entry in list(self.entries.items()):
            if entry.lock.locked() or any(not rec.task.done()
                                          for rec in entry.work._typing.values()):
                continue
            async with entry.lock:
                if not entry.ended and self.clock() - entry.last_used < self.idle_seconds:
                    continue
                try:
                    await entry.work.close_all()
                except Exception as exc:
                    errors.append(exc)
                    continue
            if (not entry.lock.locked() and not entry.pending_calls
                    and not entry.work.roles() and not entry.work.profiles
                    and self.entries.get(identity) is entry):
                try:
                    entry.work.remove_file_dirs()
                except (OSError, actions.UploadDirectoryError, actions.DownloadDirectoryError,
                        ExceptionGroup) as exc:
                    errors.append(exc)
                    continue
                self.entries.pop(identity)
        if errors:
            raise ExceptionGroup("Could not close all idle browser owners", errors)

    async def reap_exited(self) -> None:
        """Release the capacity of every browser whose Firefox has exited,
        whoever owns it, without waiting for that owner to call again.

        ⛔ NEVER UNDER A CALL. An owner whose lock is held, who has a call
        queued, or who is typing in the background is skipped exactly as
        `reap_idle` skips it: that call notices the death itself and its own
        `reap_dead` releases the slot. Live browsers are never dropped, because
        `exited` needs the process gone AND a round trip that fails.
        """
        errors = []
        for identity, entry in list(self.entries.items()):
            work = entry.work
            if (entry.ended or not work.profiles or entry.lock.locked()
                    or entry.pending_calls
                    or any(not rec.task.done() for rec in work._typing.values())):
                continue
            async with entry.lock:
                if entry.ended or self.entries.get(identity) is not entry:
                    continue
                lost = []
                for role in list(work.profiles):
                    cause = await work.exited(role)
                    if cause is not None:
                        work.gone(role, cause)
                        lost.append(role)
                try:
                    with anyio.CancelScope(shield=True):
                        await work.reap_dead()
                except Exception as exc:
                    errors.append(exc)
                work.lost.update(role for role in lost if role not in work._open)
        if errors:
            raise ExceptionGroup("Could not release exited owner browsers", errors)

    async def idle_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), min(30, self.idle_seconds / 2))
            except TimeoutError:
                try:
                    await self.reap_exited()
                except Exception:
                    logger.exception("Failed to release exited owner browsers; capacity retained")
                try:
                    await self.reap_idle()
                except Exception:
                    logger.exception("Failed to close idle owner browsers; capacity retained")

    async def close_all(self) -> None:
        self.stopping.set()
        errors = []
        try:
            # Leave time for filesystem cleanup inside mcpd's five-second grace.
            async with asyncio.timeout(EXIT_CLOSE_SECONDS):
                await _close_all(*(self.session_ended(identity) for identity in list(self.entries)))
        except Exception as exc:
            errors.append(exc)
        finally:
            # Process exit cannot leave credentials on disk just because the
            # engine close failed, was cancelled, or never answered. Normal
            # session/idle closes still retain profiles until close succeeds.
            for entry in self.entries.values():
                entry.work.revoke()
                for directory in entry.work.profiles.values():
                    try:
                        _remove_directory(directory)
                    except OSError as exc:
                        errors.append(exc)
                try:
                    entry.work.remove_file_dirs()
                except (OSError, actions.UploadDirectoryError, actions.DownloadDirectoryError,
                        ExceptionGroup) as exc:
                    errors.append(exc)
            try:
                self.instances.close()
            except ExceptionGroup as exc:
                errors.append(exc)
        if errors:
            logger.error("Owner browser shutdown failed; attempted remaining directory cleanup")
            raise ExceptionGroup("Owner browser shutdown failed", errors)
