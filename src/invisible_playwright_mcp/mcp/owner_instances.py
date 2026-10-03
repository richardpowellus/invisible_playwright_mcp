"""Locked filesystem namespaces for overlapping mcpd worker generations."""
from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
import shutil
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == "posix":
    import fcntl

logger = logging.getLogger(__name__)
PREFIX = "stealthfox-proc-"
LOCK = ".lock"
ROOT_LOCK = ".stealthfox-instances"


def _open_lock(name: str, parent: int, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(name, flags | (os.O_CREAT if create else 0), 0o600, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PermissionError("Owner instance lock cannot be a symlink") from exc
        raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise PermissionError("Owner instance lock must be a regular file owned by this UID")
    return fd


@contextmanager
def _root(root: Path):
    if os.name != "posix":
        raise RuntimeError("Owner mode requires POSIX filesystem locks")
    if root.resolve() != root:
        raise PermissionError("Owner instance root must be canonical")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = _open_lock(f"{ROOT_LOCK}-{os.getuid()}.lock", fd, create=True)
        try:
            # Serialize mkdir + lock acquisition with sweeps: a candidate must
            # never observe a published but not-yet-locked instance as dead.
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(lock)
    finally:
        os.close(fd)


def _remove_instance(parent: int, name: str, directory: int) -> None:
    """Remove an instance with its lock file LAST.

    A deletion interrupted part-way (the exit watchdog's bounded join, a kill)
    must leave an instance the next start can still prove dead: the sweep
    reclaims only instances whose .lock it can take, so a payload left behind
    without its lock would be retained forever as unverifiable.
    """
    for entry in os.listdir(directory):
        if entry == LOCK:
            continue
        info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(entry, dir_fd=directory)
        else:
            os.unlink(entry, dir_fd=directory)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(LOCK, dir_fd=directory)
    os.rmdir(name, dir_fd=parent)


def _same_directory(parent: int, name: str, directory: int) -> bool:
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(directory)
    return stat.S_ISDIR(named.st_mode) and os.path.samestat(named, opened)


class Instance:
    def __init__(self, root: Path) -> None:
        self.path = root / (PREFIX + secrets.token_urlsafe(24))
        with _root(root) as parent:
            os.mkdir(self.path.name, 0o700, dir_fd=parent)
            directory = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
            try:
                lock = _open_lock(LOCK, directory, create=True)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.parent = os.dup(parent)
                except BaseException:
                    os.close(lock)
                    raise
            except BaseException:
                os.close(directory)
                raise
        self.directory, self.lock = directory, lock
        self.closed = False

    def present(self) -> bool:
        """Is the directory at self.path still the one this instance created?"""
        return not self.closed and _same_directory(self.parent, self.path.name, self.directory)

    def retire(self) -> None:
        """Let go of an instance whose directory was removed or replaced.

        Something outside this process may delete a live instance (on
        2026-10-01 an hourly staging cleanup removed it as an old empty
        directory). A replacement in its place is not ours, so it is left
        alone and only this instance's descriptors are released.
        """
        if self.closed:
            return
        for fd in (self.lock, self.directory, self.parent):
            os.close(fd)
        self.closed = True

    def close(self) -> None:
        if self.closed:
            return
        if _same_directory(self.parent, self.path.name, self.directory):
            _remove_instance(self.parent, self.path.name, self.directory)
        elif os.path.lexists(self.path):
            raise PermissionError("Owner instance directory was replaced; refusing cleanup")
        for fd in (self.lock, self.directory, self.parent):
            os.close(fd)
        self.closed = True


class Instances:
    def __init__(self) -> None:
        self.entries: dict[Path, Instance] = {}
        self.lock = threading.RLock()
        self.stopping = False

    def directory(self, root: Path) -> Path:
        with self.lock:
            if self.stopping:
                raise RuntimeError("Owner instances are shutting down")
            current = self.entries.get(root)
            if current is not None and not current.present():
                # Swept or replaced underneath a live process. Every owner
                # directory below it is gone with it, so allocate a fresh
                # locked instance rather than handing out a path whose parent
                # no longer exists (ENOENT on the next owner mkdir).
                logger.warning("Owner instance %s was removed or replaced while in use; "
                               "allocating a new one", current.path)
                current.retire()
                current = None
            if current is None:
                current = self.entries[root] = Instance(root)
            return current.path

    def close(self) -> None:
        with self.lock:
            self.stopping = True
            errors = []
            for instance in self.entries.values():
                try:
                    instance.close()
                except OSError as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("Could not remove owner instances", errors)

    def sweep(self, roots: list[Path]) -> None:
        removed = legacy = unverified = 0
        for root in dict.fromkeys(roots):
            with _root(root) as parent:
                for name in os.listdir(parent):
                    if not name.startswith((PREFIX, "stealthfox-owner-", "owner-")):
                        continue
                    try:
                        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                        continue
                    if not name.startswith(PREFIX):
                        legacy += 1
                        continue
                    try:
                        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                            dir_fd=parent)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                            raise
                        unverified += 1
                        continue
                    try:
                        if not os.path.samestat(info, os.fstat(directory)):
                            unverified += 1
                            continue
                        try:
                            lock = _open_lock(LOCK, directory)
                        except (FileNotFoundError, PermissionError):
                            unverified += 1
                            continue
                        try:
                            try:
                                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                continue
                            if _same_directory(parent, name, directory):
                                _remove_instance(parent, name, directory)
                                removed += 1
                        finally:
                            os.close(lock)
                    finally:
                        os.close(directory)
        logger.info("Removed %d stale owner instances at startup; retained %d legacy "
                    "directories and %d unverifiable instances", removed, legacy, unverified)
