"""Upload staging belongs to a caller, not to the shared child process."""
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

import pytest

from invisible_playwright_mcp.mcp import actions, server
from invisible_playwright_mcp.mcp.owners import Owners
from invisible_playwright_mcp.mcp.work import Work
from test_a_file_is_uploaded_through_its_chooser import _Page
from test_open_first import _Recording
from test_owners import HANDLE_KEY, call, handle, identity, success, text


class _Uploading(_Recording):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.upload_page = _Page({"file": True, "multiple": True, "shown": False})

    def page(self):
        return self.upload_page


@pytest.fixture
async def owners(monkeypatch, tmp_path):
    monkeypatch.delenv(actions.DOWNLOAD_DIRS_ENV, raising=False)
    staging, second, profiles = (
        tmp_path / "staging", tmp_path / "staging-other", tmp_path / "profiles")
    staging.mkdir()
    second.mkdir()
    profiles.mkdir()
    monkeypatch.setenv(actions.UPLOAD_DIRS_ENV, os.pathsep.join([str(staging), str(second)]))
    monkeypatch.setattr("tempfile.tempdir", str(profiles))
    for name in ("STEALTHFOX_PROFILE_DIR", "STEALTHFOX_PROXY", "STEALTHFOX_SEED"):
        monkeypatch.delenv(name, raising=False)
    registry = Owners(factory=_Uploading)
    monkeypatch.setattr(server, "owners", registry)
    yield registry
    await registry.close_all()
    assert not list(staging.glob("stealthfox-proc-*"))
    assert not list(second.glob("owner-*"))
    assert not list(profiles.glob("stealthfox-proc-*"))


def upload_dir(result):
    lines = [line for line in success(result).splitlines() if line.startswith("upload dir: ")]
    assert len(lines) == 1
    return Path(lines[0].removeprefix("upload dir: "))


async def upload(owner, path, **kwargs):
    return await call(owner, "browser_upload_files", {"selector": "#f", "paths": [str(path)]},
                      **kwargs)


async def test_owner_upload_dir_is_private_and_persists_across_browser_reopen(owners):
    assert not list(Path(owners.upload_roots[0]).iterdir())
    opened = await call("A", "browser_open")
    directory = upload_dir(opened)
    assert directory.parent.parent == Path(owners.upload_roots[0])
    assert directory.parent.name.startswith("stealthfox-proc-")
    assert re.fullmatch(r"owner-[A-Za-z0-9_-]{32}", directory.name)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert success(opened).splitlines()[-1] == "fill handle: " + handle(opened)
    file = directory / "document.pdf"
    file.write_bytes(b"owner A document")
    for _ in range(2):
        success(await call("A", "browser_close"))
        assert file.read_bytes() == b"owner A document"
        assert upload_dir(await call("A", "browser_open")) == directory
    assert upload_dir(await call("A", "browser_open", {"browser": "support"})) == directory
    assert upload_dir(await call("A", "browser_status", {"browser": "support"})) == directory


async def test_a_staged_file_uploads_and_b_cannot_upload_a_file(owners):
    a = upload_dir(await call("A", "browser_open"))
    b = upload_dir(await call("B", "browser_open"))
    assert a != b
    file = a / "private.pdf"
    file.write_bytes(b"only A may upload this")
    success(await upload("A", file))
    page_a = owners.entries["A"].work._open["main"].upload_page
    sent = page_a.calls[-1][1][0]
    assert Path(sent).read_bytes() == file.read_bytes()
    refused = await upload("B", file)
    assert refused.isError, "B uploaded A's file through B's own browser"
    assert "Copy the file into your upload dir: " + str(b) in text(refused)
    assert str(a) not in text(refused)
    assert owners.entries["B"].work._open["main"].upload_page.calls == []


async def test_shared_root_and_other_configured_roots_are_not_uploadable(owners, monkeypatch, tmp_path):
    directory = upload_dir(await call("B", "browser_open"))
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(actions.UPLOAD_DIRS_ENV, os.pathsep.join([str(directory.parent), str(other)]))
    for root in (directory.parent, Path(owners.upload_roots[1]), other):
        file = root / "shared.pdf"
        file.write_bytes(b"not staged for B")
        refused = await upload("B", file)
        assert refused.isError and "Copy the file into your upload dir:" in text(refused)
    assert owners.entries["B"].work._open["main"].upload_page.calls == []


async def test_status_keeps_fill_handle_last_and_delegation_hides_upload_dir(owners):
    opened = await call("A", "browser_open")
    directory, secret = upload_dir(opened), handle(opened)
    status = await call("A", "browser_status")
    assert upload_dir(status) == directory
    assert success(status).splitlines()[-1] == "fill handle: " + secret
    delegated = await call("fill", "browser_status",
                           meta={**identity("fill"), HANDLE_KEY: secret})
    assert success(delegated).splitlines()[-1] == "fill handle: " + secret
    assert "upload dir:" not in success(delegated) and str(directory) not in success(delegated)
    assert upload_dir(await call("A", "browser_status")) == directory
    refused = await upload("fill", directory / "a.pdf",
                           meta={**identity("fill"), HANDLE_KEY: secret})
    assert refused.isError and str(directory) not in text(refused)


@pytest.mark.parametrize("ending", ["session_ended", "idle", "exit"])
async def test_owner_upload_directory_is_removed_on_owner_end(owners, ending):
    now = [0.0]
    owners.clock = lambda: now[0]
    directory = upload_dir(await call("A", "browser_open"))
    (directory / "document.pdf").write_bytes(b"staged")
    if ending == "session_ended":
        other = upload_dir(await call("B", "browser_open"))
        await owners.session_ended("A")
        assert other.is_dir()
    elif ending == "idle":
        success(await call("A", "browser_close"))
        now[0] = 900
        await owners.reap_idle()
    else:
        await owners.close_all()
    assert not directory.exists()
    assert "A" not in owners.entries


@pytest.mark.parametrize("ending", ["session_ended", "idle"])
async def test_failed_upload_cleanup_keeps_owner_record_for_retry(owners, monkeypatch, ending):
    now = [0.0]
    owners.clock = lambda: now[0]
    directory = upload_dir(await call("A", "browser_open"))
    remove = actions.shutil.rmtree

    def fail_upload_cleanup(path, *args, **kwargs):
        if Path(path) == directory:
            raise PermissionError("staged files are still held")
        return remove(path, *args, **kwargs)

    async def end():
        if ending == "session_ended":
            await owners.session_ended("A")
        else:
            now[0] = 900
            await owners.reap_idle()

    with monkeypatch.context() as patch:
        patch.setattr(actions.shutil, "rmtree", fail_upload_cleanup)
        with pytest.raises(PermissionError if ending == "session_ended" else ExceptionGroup):
            await end()
        assert "A" in owners.entries and directory.is_dir()
        assert owners.capacity.used == 0
    await end()
    assert "A" not in owners.entries and not directory.exists()


async def test_support_first_allocates_the_owners_upload_directory(owners):
    opened = await call("A", "browser_open", {"browser": "support"})
    directory = upload_dir(opened)
    assert "fill handle:" not in success(opened)
    assert upload_dir(await call("A", "browser_open")) == directory


async def test_idle_cleanup_preserves_uploads_for_a_queued_call(owners):
    now = [0.0]
    owners.clock = lambda: now[0]
    directory = upload_dir(await call("A", "browser_open"))
    file = directory / "keep.pdf"
    file.write_bytes(b"still staged")
    session = owners.entries["A"].work._open["main"]
    close = session.close
    closing, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        closing.set()
        await release.wait()
        await close()

    session.close = slow_close
    now[0] = 900
    reaping = asyncio.create_task(owners.reap_idle())
    try:
        await asyncio.wait_for(closing.wait(), 2)
        opening = asyncio.create_task(call("A", "browser_open"))
        await asyncio.sleep(0)
    finally:
        release.set()
    await reaping
    assert upload_dir(await opening) == directory
    assert file.read_bytes() == b"still staged"
    success(await upload("A", file))


async def test_symlinks_outside_owner_dir_and_hidden_files_are_refused(owners):
    a = upload_dir(await call("A", "browser_open"))
    b = upload_dir(await call("B", "browser_open"))
    secret = a / "private.pdf"
    secret.write_bytes(b"private")
    (b / "link.pdf").symlink_to(secret)
    (b / ".secret").write_bytes(b"hidden")
    for path in (b / "link.pdf", b / ".secret", b / ".." / a.name / secret.name):
        assert (await upload("B", path)).isError
    assert owners.entries["B"].work._open["main"].upload_page.calls == []


async def test_snapshot_rechecks_owner_boundary_after_page_await(owners):
    a = upload_dir(await call("A", "browser_open"))
    b = upload_dir(await call("B", "browser_open"))
    secret = a / "private.pdf"
    secret.write_bytes(b"private")
    file = b / "document.pdf"
    file.write_bytes(b"original")
    page = owners.entries["B"].work._open["main"].upload_page
    original = page.eval_on_selector

    async def swap(selector, js):
        file.unlink()
        file.symlink_to(secret)
        return await original(selector, js)

    page.eval_on_selector = swap
    refused = await upload("B", file)
    assert refused.isError and str(a) not in text(refused)
    assert page.calls == []


@pytest.mark.parametrize("replace_directory", [False, True])
async def test_upload_cleanup_does_not_follow_symlinks(owners, tmp_path, replace_directory):
    directory = upload_dir(await call("A", "browser_open"))
    outside = tmp_path / "untouched"
    outside.mkdir()
    file = outside / "keep.pdf"
    file.write_bytes(b"keep")
    if replace_directory:
        directory.rmdir()
        directory.symlink_to(outside, target_is_directory=True)
    else:
        (directory / "link").symlink_to(outside, target_is_directory=True)
    await owners.session_ended("A")
    assert file.read_bytes() == b"keep"
    assert not directory.exists() and not directory.is_symlink()


async def test_replaced_owner_upload_directory_cannot_authorize_another_owner(owners):
    a = upload_dir(await call("A", "browser_open"))
    b = upload_dir(await call("B", "browser_open"))
    file = a / "private.pdf"
    file.write_bytes(b"private")
    b.rmdir()
    b.symlink_to(a, target_is_directory=True)
    refused = await upload("B", b / "private.pdf")
    assert refused.isError and str(a) not in text(refused)
    assert owners.entries["B"].work._open["main"].upload_page.calls == []


@pytest.mark.parametrize("root_kind", ["equal", "parent", "second_parent"])
def test_owner_startup_refuses_any_upload_root_containing_profiles(owners, monkeypatch, root_kind):
    profile_root = Path(tempfile.gettempdir())
    if root_kind == "equal":
        roots = str(profile_root)
    elif root_kind == "parent":
        roots = str(profile_root.parent)
    else:
        roots = os.pathsep.join([owners.upload_roots[0], str(profile_root.parent)])
    monkeypatch.setenv(actions.UPLOAD_DIRS_ENV, roots)
    monkeypatch.setenv("STEALTHFOX_OWNER_MODE", "mcpd")
    with pytest.raises(ValueError, match="profile temp directory"):
        Owners.from_env()


@pytest.mark.parametrize("root_kind", ["missing", "relative", "symlink"])
def test_invalid_upload_roots_are_refused_in_owner_mode(owners, monkeypatch, tmp_path, root_kind):
    root = tmp_path / "bad-root"
    if root_kind == "symlink":
        root.symlink_to(owners.upload_roots[0], target_is_directory=True)
    monkeypatch.setenv(actions.UPLOAD_DIRS_ENV, "relative" if root_kind == "relative" else str(root))
    with pytest.raises(actions.UploadDirectoryError):
        Owners()


async def test_unconfigured_owner_uploads_are_off(owners, monkeypatch, tmp_path):
    monkeypatch.delenv(actions.UPLOAD_DIRS_ENV)
    registry = Owners(factory=_Uploading)
    monkeypatch.setattr(server, "owners", registry)
    try:
        assert "upload dir:" not in success(await call("A", "browser_open"))
        assert "upload dir:" not in success(await call("A", "browser_status"))
        assert (await upload("A", tmp_path / "file.pdf")).isError
    finally:
        await registry.close_all()


async def test_non_owner_uploads_still_accept_shared_staging_files(owners, monkeypatch):
    monkeypatch.setattr(server, "owners", None)
    work = Work("default", factory=_Uploading)
    monkeypatch.setattr(server, "work", work)
    file = Path(owners.upload_roots[0]) / "shared.pdf"
    file.write_bytes(b"shared staging")
    try:
        assert "upload dir:" not in success(await call(None, "browser_open", meta={}))
        assert "upload dir:" not in success(await call(None, "browser_status", meta={}))
        success(await upload(None, file, meta={}))
    finally:
        await work.close_all()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Unix filesystem ownership")
def test_startup_removes_only_direct_owned_directories_without_following_links(owners, tmp_path, caplog):
    profiles = Path(tempfile.gettempdir())
    uploads = Path(owners.upload_roots[0])
    stale = [profiles / "stealthfox-proc-old", uploads / "stealthfox-proc-old"]
    keep = [
        profiles / "ordinary", uploads / "ordinary",
        Path(owners.upload_roots[1]) / "owner-other-root",
        profiles / "ordinary" / "stealthfox-owner-nested",
        uploads / "ordinary" / "owner-nested",
        tmp_path / "outside",
    ]
    for directory in stale + keep:
        directory.mkdir()
        (directory / "keep").write_text("private")
    for directory in stale:
        (directory / ".lock").touch(mode=0o600)
    links = [profiles / "stealthfox-proc-link", uploads / "stealthfox-proc-link"]
    for link in links:
        link.symlink_to(tmp_path / "outside", target_is_directory=True)
    for directory in stale:
        (directory / "link").symlink_to(tmp_path / "outside", target_is_directory=True)
    files = [profiles / "stealthfox-owner-file", uploads / "owner-file"]
    for file in files:
        file.write_text("not a directory")
    with caplog.at_level(logging.INFO):
        owners.remove_stale_dirs()
    assert not any(directory.exists() for directory in stale)
    assert all((directory / "keep").read_text() == "private" for directory in keep)
    assert all(link.is_symlink() for link in links)
    assert all(file.read_text() == "not a directory" for file in files)
    assert "Removed 2 stale owner instances at startup" in caplog.text
    # These intentionally preserved names are not resources of the registry.
    for path in links + files:
        path.unlink()
    (Path(owners.upload_roots[1]) / "owner-other-root" / "keep").unlink()
    (Path(owners.upload_roots[1]) / "owner-other-root").rmdir()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Unix filesystem ownership")
def test_startup_preserves_legacy_directories(owners, caplog):
    profiles = Path(tempfile.gettempdir())
    directories = [profiles / "stealthfox-owner-other", Path(owners.upload_roots[0]) / "owner-other"]
    for directory in directories:
        directory.mkdir()
    with caplog.at_level(logging.INFO):
        owners.remove_stale_dirs()
    assert all(directory.is_dir() for directory in directories)
    assert "Removed 0 stale owner instances at startup; retained 2 legacy" in caplog.text
    for directory in directories:
        directory.rmdir()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Unix filesystem ownership")
def test_startup_cleanup_failure_is_not_silently_ignored(owners, monkeypatch):
    directory = Path(tempfile.gettempdir()) / "stealthfox-proc-old"
    directory.mkdir()
    (directory / ".lock").touch(mode=0o600)
    (directory / "owner-x").mkdir()

    def refused(path, **kwargs):
        raise PermissionError("cannot delete stale credentials")

    with monkeypatch.context() as patch:
        patch.setattr("invisible_playwright_mcp.mcp.owners.shutil.rmtree", refused)
        with pytest.raises(PermissionError, match="stale credentials"):
            owners.remove_stale_dirs()
    (directory / "owner-x").rmdir()
    (directory / ".lock").unlink()
    directory.rmdir()


async def test_stale_cleanup_cannot_run_over_live_owners(owners):
    directory = upload_dir(await call("A", "browser_open"))
    with pytest.raises(RuntimeError, match="before accepting calls"):
        owners.remove_stale_dirs()
    assert directory.is_dir()


async def test_shutdown_cleanup_attempts_all_browsers_after_cancelled_close(owners):
    directory = upload_dir(await call("A", "browser_open"))
    success(await call("A", "browser_open", {"browser": "support"}))
    entry = owners.entries["A"]
    profiles = list(entry.work.profiles.values())
    session = entry.work._open["main"]
    support = entry.work._open["support"]
    close = session.close

    async def cancelled():
        raise asyncio.CancelledError("engine close was interrupted")

    session.close = cancelled
    try:
        with pytest.raises(ExceptionGroup, match="Owner browser shutdown"):
            await owners.close_all()
        assert support.closed
        assert not directory.exists() and not any(profile.exists() for profile in profiles)
        assert owners.capacity.used == 1  # an unconfirmed close never releases capacity
        assert not owners.handles
    finally:
        session.close = close


async def test_lifespan_interrupts_stalled_idle_reap_before_exit_cleanup(owners, monkeypatch):
    owners.idle_seconds = 0.01
    monkeypatch.setattr("invisible_playwright_mcp.mcp.owners.EXIT_CLOSE_SECONDS", 0.05)
    entered = asyncio.Event()

    async def stalled():
        entered.set()
        await asyncio.Event().wait()

    async with asyncio.timeout(2):
        with pytest.raises(ExceptionGroup, match="Owner browser shutdown"):
            async with server._lifespan(server.mcp):
                directory = upload_dir(await call("A", "browser_open"))
                session = owners.entries["A"].work._open["main"]
                close = session.close
                session.close = stalled
                await entered.wait()
    assert not directory.exists()
    session.close = close
