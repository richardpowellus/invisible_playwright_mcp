"""Independent-review regressions: overlapping workers and raced mkdir."""
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="mcpd owner mode requires POSIX flock")

from invisible_playwright_mcp.mcp import actions
from invisible_playwright_mcp.mcp.owners import Owners
from test_open_first import _Recording
from _stdio_helpers import subprocess_env


@pytest.fixture
def roots(tmp_path, monkeypatch):
    roots = [tmp_path / name for name in ("profiles", "uploads", "downloads")]
    for root in roots:
        root.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(roots[0]))
    monkeypatch.setenv(actions.UPLOAD_DIRS_ENV, str(roots[1]))
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(roots[2]))
    return roots


async def test_replacement_sweep_preserves_the_live_generation(roots):
    old, candidate = Owners(factory=_Recording), Owners(factory=_Recording)
    try:
        work = old.caller("A").work
        await work.open("main")
        directories = [work.profiles["main"], work.upload_dir, work.download_dir]
        for directory in directories:
            (directory / "private").write_bytes(b"live credentials")
        candidate.remove_stale_dirs()
        assert all((directory / "private").exists() for directory in directories), (
            "replacement startup deleted the live worker's data")
        await candidate.close_all()  # failed candidate must not destroy the old worker
        assert all((directory / "private").exists() for directory in directories)
    finally:
        await old.close_all()
        await candidate.close_all()


async def test_dead_instances_removed_but_legacy_flat_dirs_are_retained(roots, caplog):
    dead, legacy = [], []
    for root in roots:
        instance = root / "stealthfox-proc-dead"
        instance.mkdir()
        (instance / ".lock").touch(mode=0o600)
        (instance / "private").write_bytes(b"dead credentials")
        dead.append(instance)
        flat = root / ("stealthfox-owner-legacy" if root == roots[0] else "owner-legacy")
        flat.mkdir()
        legacy.append(flat)
    registry = Owners()
    try:
        with caplog.at_level(logging.INFO):
            registry.remove_stale_dirs()
        assert not any(directory.exists() for directory in dead)
        assert all(directory.exists() for directory in legacy)
        assert "legacy" in caplog.text
    finally:
        await registry.close_all()


async def test_kernel_releases_instance_lock_after_process_death(roots):
    code = """
import sys
from pathlib import Path
from invisible_playwright_mcp.mcp.owner_instances import Instances
instances = Instances()
directory = instances.directory(Path(sys.argv[1]))
(directory / 'private').write_bytes(b'credentials')
print(directory, flush=True)
sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(roots[0])], env=subprocess_env(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    registry = Owners()
    try:
        directory = Path(child.stdout.readline().strip())
        assert directory.is_dir()
        registry.remove_stale_dirs()
        assert (directory / "private").read_bytes() == b"credentials"
        child.kill()
        child.wait(timeout=2)
        registry.remove_stale_dirs()
        assert not directory.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
        await registry.close_all()


async def test_instance_cleanup_never_follows_a_lock_or_directory_symlink(roots, caplog):
    victim = roots[0] / "victim"
    victim.mkdir()
    private = victim / "private"
    private.write_bytes(b"keep")
    directory_link = roots[0] / "stealthfox-proc-linked"
    directory_link.symlink_to(victim, target_is_directory=True)
    instance = roots[0] / "stealthfox-proc-bad-lock"
    instance.mkdir()
    (instance / ".lock").symlink_to(private)
    registry = Owners()
    try:
        with caplog.at_level(logging.INFO):
            registry.remove_stale_dirs()
        assert private.read_bytes() == b"keep"
        assert directory_link.is_symlink() and instance.exists()
        assert "1 unverifiable" in caplog.text
    finally:
        await registry.close_all()


@pytest.mark.parametrize("explicit_env", [False, True])
def test_mkdir_cannot_follow_an_ancestor_swapped_between_components(roots, monkeypatch, explicit_env):
    owner = roots[2] / "owner-b"
    victim = roots[2] / "owner-a"
    owner.mkdir()
    victim.mkdir()
    ancestor = owner / "first"
    mkdir = os.mkdir
    swapped = False

    def swap(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if Path(path).name == "nested" and not swapped:
            swapped = True
            ancestor.rename(owner / "original")
            ancestor.symlink_to(victim, target_is_directory=True)
        return mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", swap)
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(owner))
    try:
        actions.download_target(str(ancestor / "nested"),
                                env={actions.DOWNLOAD_DIRS_ENV: str(owner)} if explicit_env else None)
    except PermissionError:
        pass
    assert swapped
    assert not (victim / "nested").exists(), "mkdir followed a swapped ancestor into owner A"


def test_exit_watchdog_exits_when_cleanup_blocks_and_stderr_is_closed(monkeypatch):
    """The hard deadline must not wait on cleanup or on writing a diagnostic."""
    import threading
    import time
    from types import SimpleNamespace

    from invisible_playwright_mcp.mcp import owner_transport

    exited = threading.Event()
    release = threading.Event()

    def stuck_close():
        release.wait(30)

    monkeypatch.setattr(owner_transport, "HARD_EXIT_SECONDS", 0.05)
    monkeypatch.setattr(owner_transport, "HARD_EXIT_CLEANUP_SECONDS", 0.2)
    monkeypatch.setattr(owner_transport.os, "_exit", lambda code: exited.set())
    monkeypatch.setattr(owner_transport.os, "write", lambda *a: (_ for _ in ()).throw(BrokenPipeError()))
    watchdog = object.__new__(owner_transport.ExitWatchdog)
    watchdog.registry = SimpleNamespace(instances=SimpleNamespace(close=stuck_close))
    watchdog.done = threading.Event()
    start = time.monotonic()
    threading.Thread(target=watchdog._wait, daemon=True).start()
    try:
        assert exited.wait(2), "watchdog never exited while cleanup was blocked"
        assert time.monotonic() - start < 1.0
    finally:
        release.set()


def test_an_instance_whose_deletion_was_interrupted_is_reclaimed_next_start(tmp_path, monkeypatch):
    """Deletion stopped part-way must leave the lock, so the sweep can prove the owner dead."""
    import os

    import pytest

    from invisible_playwright_mcp.mcp import owner_instances

    root = tmp_path.resolve()
    instance = owner_instances.Instance(root)
    (instance.path / "owner-a").mkdir()
    (instance.path / "owner-a" / "cookies.sqlite").write_text("x")
    (instance.path / "owner-b").mkdir()

    real_rmtree = owner_instances.shutil.rmtree
    calls = []

    def dies_after_one(path, *args, **kwargs):
        calls.append(path)
        if len(calls) > 1:
            raise KeyboardInterrupt("killed mid-deletion")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(owner_instances.shutil, "rmtree", dies_after_one)
    with pytest.raises(KeyboardInterrupt):
        instance.close()
    monkeypatch.setattr(owner_instances.shutil, "rmtree", real_rmtree)
    assert (instance.path / owner_instances.LOCK).exists(), "lock removed before the payload"
    # The process dies: the kernel releases its flock.
    for fd in (instance.lock, instance.directory, instance.parent):
        os.close(fd)

    owner_instances.Instances().sweep([root])
    assert not instance.path.exists()
