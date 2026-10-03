"""Owner downloads use private destinations and private browser landing areas."""
import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="mcpd owner mode requires POSIX flock")

from invisible_playwright_mcp.mcp import actions, server, session as session_mod
from invisible_playwright_mcp.mcp.owners import Owners
from invisible_playwright_mcp.mcp.work import Work
from test_a_downloaded_file_is_kept import _Context, _Page, _Response
from test_owners import HANDLE_KEY, call, handle, identity, success, text


class _BrowserContext(_Context):
    def __init__(self):
        super().__init__()
        self.pages = []

    async def close(self):
        pass


class _Engine:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return _BrowserContext()

    async def __aexit__(self, *args):
        pass


class _Downloading(session_mod.StealthSession):
    async def start(self):
        await super().start()

        async def saves(page):
            (Path(self.downloads) / "statement.pdf").write_bytes(b"%PDF-1.4 private")

        page = _Page(self, saves)
        page.context = self._context
        self._context.page = page
        self._context.pages.append(page)


@pytest.fixture
async def owners(monkeypatch, tmp_path):
    for name in ("STEALTHFOX_PROFILE_DIR", "STEALTHFOX_PROXY", "STEALTHFOX_SEED",
                 "STEALTHFOX_BINARY", actions.UPLOAD_DIRS_ENV):
        monkeypatch.delenv(name, raising=False)
    roots = [tmp_path / "downloads", tmp_path / "second", tmp_path / "profiles"]
    for root in roots:
        root.mkdir()
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, os.pathsep.join(map(str, roots[:2])))
    monkeypatch.setattr(tempfile, "tempdir", str(roots[2]))
    monkeypatch.setattr(session_mod, "InvisiblePlaywright", _Engine)
    monkeypatch.setattr(actions, "_POLL_S", 0.01)
    monkeypatch.setattr(actions, "SAVED_COPY_GRACE_S", 0)
    registry = Owners(factory=_Downloading)
    monkeypatch.setattr(server, "owners", registry)
    yield registry
    await registry.close_all()
    assert not list(roots[0].glob("stealthfox-proc-*"))
    assert not list(roots[2].glob("stealthfox-proc-*"))


def download_dir(result):
    line = next(line for line in success(result).splitlines() if line.startswith("download dir: "))
    return Path(line.removeprefix("download dir: "))


async def download(owner, **kwargs):
    return await call(owner, "browser_download", {"selector": "#statement", **kwargs})


async def test_downloads_are_private_and_b_cannot_write_into_as_directory(owners):
    a = download_dir(await call("A", "browser_open"))
    b = download_dir(await call("B", "browser_open"))
    saved = json.loads(success(await download("A", save_to=str(a))))
    file = Path(saved["saved"])
    assert file.parent == a and file.read_bytes() == b"%PDF-1.4 private"
    refused = await download("B", save_to=str(a))
    assert refused.isError, "B saved into A's download directory"
    assert str(a) not in text(refused) and str(b) in text(refused)
    assert owners.entries["B"].work.session("main").page().clicks == []
    assert str(a) not in success(await call("B", "browser_status"))
    assert str(a) not in success(await call("B", "browser_list"))
    own = json.loads(success(await download("B")))
    assert Path(own["saved"]).parent == b
    assert file.read_bytes() == b"%PDF-1.4 private"


async def test_b_cannot_list_or_read_as_downloads_through_a_landing_symlink(owners):
    a = download_dir(await call("A", "browser_open"))
    b = download_dir(await call("B", "browser_open"))
    session_a = owners.entries["A"].work.session("main")
    session_b = owners.entries["B"].work.session("main")
    landing_b = Path(session_b.downloads)
    landing_b.rmdir()
    landing_b.symlink_to(session_a.downloads, target_is_directory=True)

    async def a_finishes_downloading(page):
        (Path(session_a.downloads) / "A-only.pdf").write_bytes(b"%PDF-1.4 A's private statement")

    session_b.page().effect = a_finishes_downloading
    refused = await download("B")
    assert refused.isError, "B read A's downloads through a replaced landing directory"
    assert str(a) not in text(refused)
    assert session_b.page().clicks == []
    assert not (b / "statement.pdf").exists()


async def test_download_directory_lifetime_preferences_and_handle_disclosure(owners):
    opened = await call("A", "browser_open")
    directory = download_dir(opened)
    secret = handle(opened)
    assert directory.parent.parent == Path(owners.download_roots[0])
    assert directory.parent.name.startswith("stealthfox-proc-")
    assert re.fullmatch(r"owner-[A-Za-z0-9_-]{32}", directory.name)
    assert directory.stat().st_mode & 0o777 == 0o700
    session = owners.entries["A"].work.session("main")
    landing = Path(session.downloads)
    assert landing.parent == directory and landing.stat().st_mode & 0o777 == 0o700
    assert session._ipw.kwargs["extra_prefs"]["browser.download.dir"] == str(landing)
    assert "download_root" not in session._ipw.kwargs
    saved = Path(json.loads(success(await download("A")))["saved"])
    success(await call("A", "browser_close"))
    assert not landing.exists() and saved.exists()
    assert download_dir(await call("A", "browser_open")) == directory
    assert download_dir(await call("A", "browser_open", {"browser": "support"})) == directory
    work = owners.entries["A"].work
    assert work.session("main").downloads != work.session("support").downloads
    status = await call("A", "browser_status")
    assert download_dir(status) == directory
    assert success(status).splitlines()[-1] == "fill handle: " + handle(status)
    assert handle(status) != secret
    meta = {**identity("fill"), HANDLE_KEY: handle(status)}
    delegated = await call("fill", "browser_status", meta=meta)
    assert success(delegated).splitlines()[-1] == "fill handle: " + handle(status)
    assert "download dir:" not in text(delegated) and str(directory) not in text(delegated)
    assert (await call("fill", "browser_download", {"selector": "#statement"}, meta=meta)).isError


async def test_support_first_and_shown_documents_use_the_same_owner_boundary(owners):
    directory = download_dir(await call("A", "browser_open", {"browser": "support"}))
    session = owners.entries["A"].work.session("support")

    async def shown(page):
        for listener in page.context.listeners:
            listener(_Response("https://example.test/shown.pdf", {"content-type": "application/pdf"}))

    session.page().effect = shown
    result = await call("A", "browser_download",
                        {"selector": "#statement", "browser": "support",
                         "save_to": str(directory / "statements")})
    saved = json.loads(success(result))
    assert Path(saved["saved"]).parent == directory / "statements"
    assert Path(saved["saved"]).read_bytes() == b"%PDF-1.4 shown"


@pytest.mark.parametrize("where", ["shared", "second", "hidden", "symlink"])
async def test_refused_destinations_never_click(owners, where):
    directory = download_dir(await call("A", "browser_open"))
    targets = {"shared": directory.parent, "second": Path(owners.download_roots[1]),
               "hidden": directory / ".hidden", "symlink": directory / "link"}
    targets["symlink"].symlink_to(owners.download_roots[1], target_is_directory=True)
    assert (await download("A", save_to=str(targets[where]))).isError
    assert owners.entries["A"].work.session("main").page().clicks == []


@pytest.mark.parametrize("ending", ["session_ended", "idle", "exit", "failed_exit"])
async def test_download_directory_removed_on_owner_end_and_failed_shutdown(owners, ending):
    directory = download_dir(await call("A", "browser_open"))
    success(await download("A"))
    session = owners.entries["A"].work.session("main")
    close = session.close
    if ending == "session_ended":
        b = download_dir(await call("B", "browser_open"))
        await owners.session_ended("A")
        assert b.exists()
    elif ending == "idle":
        owners.clock = lambda: owners.entries["A"].last_used + 901
        await owners.reap_idle()
    elif ending == "exit":
        await owners.close_all()
    else:
        async def failed():
            raise asyncio.CancelledError("engine stopped during shutdown")

        session.close = failed
        try:
            with pytest.raises(ExceptionGroup, match="Owner browser shutdown"):
                await owners.close_all()
        finally:
            session.close = close
    assert not directory.exists()


async def test_download_cleanup_is_attempted_even_if_upload_cleanup_fails(owners, monkeypatch, tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    owners.upload_roots = [str(uploads)]
    directory = download_dir(await call("A", "browser_open"))
    work = owners.entries["A"].work
    remove = work.remove_upload_dir

    def fail():
        raise PermissionError("uploads still held")

    monkeypatch.setattr(work, "remove_upload_dir", fail)
    with pytest.raises(PermissionError, match="uploads still held"):
        await owners.session_ended("A")
    assert not directory.exists() and work.upload_dir.exists()
    monkeypatch.setattr(work, "remove_upload_dir", remove)


@pytest.mark.parametrize("swap", ["destination", "landing"])
async def test_paths_are_rechecked_after_the_page_await(owners, swap):
    a = download_dir(await call("A", "browser_open"))
    b = download_dir(await call("B", "browser_open"))
    session = owners.entries["B"].work.session("main")
    target = b / "statements"
    target.mkdir()
    landing = Path(session.downloads)

    async def replace(page):
        if swap == "destination":
            target.rmdir()
            target.symlink_to(a, target_is_directory=True)
        else:
            landing.rmdir()
            landing.symlink_to(a, target_is_directory=True)
        for listener in page.context.listeners:
            listener(_Response("https://example.test/document.pdf", {"content-type": "application/pdf"}))

    session.page().effect = replace
    refused = await download("B", save_to=str(target))
    assert refused.isError
    assert str(a) not in text(refused)
    assert not (a / "document.pdf").exists()


async def test_the_open_source_descriptor_must_still_belong_to_the_owner(owners, monkeypatch):
    a = download_dir(await call("A", "browser_open"))
    b = download_dir(await call("B", "browser_open"))
    secret = a / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 A secret")
    open_file = os.open

    def redirected(path, flags, *args, **kwargs):
        if path == "statement.pdf" and flags & os.O_ACCMODE == os.O_RDONLY:
            return open_file(secret, flags, *args)
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", redirected)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {redirected})
    refused = await download("B")
    assert refused.isError
    assert secret.read_bytes() == b"%PDF-1.4 A secret" and not (b / "statement.pdf").exists()


@pytest.mark.parametrize("kind", ["equal", "parent", "second_parent", "missing", "relative", "link"])
def test_download_roots_fail_closed_at_startup(owners, monkeypatch, tmp_path, kind):
    profiles = Path(tempfile.gettempdir())
    roots = {
        "equal": str(profiles), "parent": str(profiles.parent),
        "second_parent": os.pathsep.join([owners.download_roots[0], str(profiles.parent)]),
        "missing": str(tmp_path / "missing"), "relative": "relative",
        "link": str(tmp_path / "link"),
    }
    (tmp_path / "link").symlink_to(owners.download_roots[0], target_is_directory=True)
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, roots[kind])
    with pytest.raises((ValueError, actions.DownloadDirectoryError)):
        Owners()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Unix UID checks")
def test_startup_sweeps_only_owned_direct_download_directories(owners, monkeypatch, caplog):
    root = Path(owners.download_roots[0])
    stale, foreign, target = root / "stealthfox-proc-stale", root / "stealthfox-proc-foreign", root / "keep"
    other = Path(owners.download_roots[1]) / "owner-other"
    for directory in (stale, foreign, target, other, target / "owner-nested"):
        directory.mkdir()
    for directory in (stale, foreign):
        (directory / ".lock").touch(mode=0o600)
    (target / "keep.pdf").write_bytes(b"keep")
    link = root / "stealthfox-proc-link"
    link.symlink_to(target, target_is_directory=True)
    (stale / "link").symlink_to(target, target_is_directory=True)
    original_stat = os.stat

    def other_uid(path, **kwargs):
        result = original_stat(path, **kwargs)
        if str(path) == foreign.name:
            values = list(result)
            values[4] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "stat", other_uid)
    with caplog.at_level(logging.INFO):
        owners.remove_stale_dirs()
    assert not stale.exists() and foreign.exists() and other.exists()
    assert link.is_symlink() and (target / "keep.pdf").read_bytes() == b"keep"
    assert (target / "owner-nested").exists()
    assert "Removed 1 stale owner instances" in caplog.text
    (foreign / ".lock").unlink()
    foreign.rmdir()
    link.unlink()


async def test_without_download_roots_incoming_files_stay_in_the_owner_profile(owners, monkeypatch):
    owners.download_roots = []
    opened = await call("A", "browser_open")
    assert "download dir:" not in success(opened)
    work = owners.entries["A"].work
    assert Path(work.session("main").downloads).parent == work.profiles["main"]
    assert (await download("A")).isError
    assert work.session("main").page().clicks == []


async def test_non_owner_mode_still_uses_shared_destinations(owners, monkeypatch):
    work = Work("default", factory=_Downloading)
    monkeypatch.setattr(server, "owners", None)
    monkeypatch.setattr(server, "work", work)
    try:
        opened = await call(None, "browser_open", meta={})
        assert "download dir:" not in success(opened)
        assert "fill handle:" not in success(await call(None, "browser_status", meta={}))
        landing = Path(work.session("main").downloads)
        for root in owners.download_roots:
            result = await call(None, "browser_download",
                                {"selector": "#statement", "save_to": root}, meta={})
            saved = json.loads(success(result))
            assert Path(saved["saved"]).parent == Path(root)
            assert Path(saved["saved"]).read_bytes() == b"%PDF-1.4 private"
        success(await call(None, "browser_close", meta={}))
        assert not landing.exists()
        assert all((Path(root) / "statement.pdf").exists() for root in owners.download_roots)
    finally:
        await work.close_all()
