"""A browser whose Firefox exited says why in the journal, and frees its slot
without its owner calling again.

2026-10-07: a session's browser died 25 s after a reCAPTCHA click. The tool
answered only GONE, the engine's exit code and last output were discarded, and
the ephemeral profile was deleted, so the cause was unrecoverable. And because
only that owner's NEXT call released the slot, `browser_open` answered
"Browser capacity exhausted" to everybody for 16 minutes.
"""
from __future__ import annotations

import logging
import os

import pytest

from invisible_playwright.async_api import TargetClosedError
from invisible_playwright_mcp.mcp import GONE, actions, masked, server
from invisible_playwright_mcp.mcp.owners import Owners, profile_in_use
from test_open_first import _Recording
from test_owners import call, success, text

pytestmark = pytest.mark.skipif(os.name != "posix", reason="mcpd owner mode requires POSIX flock")

EXITED = ("Target page, context or browser has been closed: the pipe is closed: \n"
          "  the browser exited with code 11\n"
          "  its last output:\n"
          "    Exiting due to channel error. typed hunter2-secret-value")
SECRET = "hunter2-secret-value"


class _Exits(_Recording):
    """Dies the way a crashed Firefox does: the object still looks usable,
    and only a round trip raises, carrying the engine's reason."""

    async def describe_pages(self):
        if self.dead:
            raise TargetClosedError(EXITED)
        return await super().describe_pages()


@pytest.fixture
async def owners(monkeypatch, tmp_path):
    for name in ("STEALTHFOX_SEED", "STEALTHFOX_PROXY", "STEALTHFOX_PROFILE_DIR",
                 "STEALTHFOX_HEADLESS", "STEALTHFOX_BINARY", "STEALTHFOX_NO_PROXY",
                 actions.UPLOAD_DIRS_ENV, actions.DOWNLOAD_DIRS_ENV):
        monkeypatch.delenv(name, raising=False)
    running: set = set()
    registry = Owners(limit=1, factory=_Exits,
                      process_probe=lambda directory: directory in running)
    registry.running = running
    monkeypatch.setattr(server, "owners", registry)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    yield registry
    await registry.close_all()
    assert registry.capacity.used == 0


def _open(owners, owner):
    work = owners.entries[owner].work
    owners.running.add(work.profiles["main"])
    return work, work._open["main"]


def _kill(owners, work, session):
    session.dead = True
    owners.running.discard(work.profiles["main"])


async def test_a_dead_browser_says_its_exit_code_in_the_journal_redacted(owners, caplog):
    success(await call("A", "browser_open"))
    work, session = _open(owners, "A")
    masked.registry(session).register(SECRET)
    _kill(owners, work, session)
    with caplog.at_level(logging.WARNING):
        result = await call("A", "browser_status")
    assert result.isError and text(result).endswith(GONE % "main")
    records = [r.getMessage() for r in caplog.records if "is gone" in r.getMessage()]
    assert records, "the death was not logged"
    assert "code 11" in records[0] and "owner A" in records[0] and "main" in records[0]
    assert "Exiting due to channel error" in records[0]
    assert SECRET not in "\n".join(records), "a masked value reached the journal"


async def test_a_dead_browser_frees_its_slot_within_one_sweep(owners, caplog):
    success(await call("A", "browser_open"))
    work, session = _open(owners, "A")
    refused = await call("B", "browser_open")
    assert refused.isError and "capacity exhausted" in text(refused)

    _kill(owners, work, session)   # A never calls again
    with caplog.at_level(logging.WARNING):
        await owners.reap_exited()

    assert session.closed and owners.capacity.used == 0
    assert any("code 11" in r.getMessage() for r in caplog.records)
    success(await call("B", "browser_open"))
    # A's next call is told its browser died, not that it was never open.
    told = await call("A", "browser_status")
    assert told.isError and text(told).endswith(GONE % "main")


async def test_the_sweep_keeps_live_browsers_and_owners_mid_call(owners):
    success(await call("A", "browser_open"))
    work, session = _open(owners, "A")
    await owners.reap_exited()
    assert not session.closed and owners.capacity.used == 1

    # /proc says gone but the browser answers: kept.
    owners.running.discard(work.profiles["main"])
    await owners.reap_exited()
    assert not session.closed and owners.capacity.used == 1

    # Dead, but its owner is mid-call: that call's own reap handles it.
    session.dead = True
    async with owners.entries["A"].lock:
        await owners.reap_exited()
        assert not session.closed and owners.capacity.used == 1
    await owners.reap_exited()
    assert session.closed and owners.capacity.used == 0


def test_profile_in_use_reads_proc(tmp_path):
    if not os.path.isdir("/proc"):
        pytest.skip("needs /proc")
    assert profile_in_use(tmp_path) is False
