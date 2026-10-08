"""An owner's browser keeps who it was across close, crash and reopen - in
memory only, for as long as the owner lives."""
import asyncio
import os

import pytest

from invisible_playwright_mcp.mcp import identity, store
from invisible_playwright_mcp.mcp.owners import Owners
from invisible_playwright_mcp.mcp.session import StealthSession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="owner mode needs POSIX locks")


class FakeContext:
    def __init__(self):
        self.pages = []

    async def close(self):
        pass

    async def cookies(self):
        return []


class FakeSession(StealthSession):
    async def start(self):
        await self._attach(FakeContext())


@pytest.fixture
async def registry(monkeypatch, tmp_path):
    for name in ("DISPLAY", "STEALTHFOX_SEED", "STEALTHFOX_PROXY", "STEALTHFOX_PROFILE_DIR",
                 "INVISIBLE_MCP_UPLOAD_DIRS", "INVISIBLE_MCP_DOWNLOAD_DIRS"):
        monkeypatch.delenv(name, raising=False)
    identity.display_size.cache_clear()
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    now = [0.0]
    registry = Owners(factory=FakeSession, clock=lambda: now[0])
    registry.now = now
    yield registry
    await registry.close_all()
    assert registry.capacity.used == 0
    identity.display_size.cache_clear()


@pytest.mark.parametrize("role", ["main", "support"])
@pytest.mark.parametrize("ending", ["close", "crash", "reopen"])
async def test_owner_reopens_same_drawn_seed_without_persisting_it(
        registry, monkeypatch, tmp_path, role, ending):
    draws = iter([101, 202, 303])
    monkeypatch.setattr(identity.random, "randrange", lambda *a: next(draws))
    work = registry.caller("owner-a").work
    await work.open(role)
    first = work._open[role]
    old_profile = work.profiles[role]
    if ending == "close":
        await work.close(role)
    elif ending == "crash":
        work.gone(role)
        await work.reap_dead()
    answer = await work.open(role)
    assert work._open[role].seed == first.seed == 101
    assert "the person this session already was" in answer
    assert work.profiles[role] != old_profile and not old_profile.exists()
    assert not list(tmp_path.rglob(identity.IDENTITY_FILE))
    assert store.load("") is None


async def test_memory_is_per_owner_role_and_explicit_proxy_still_draws(registry, monkeypatch):
    draws = iter([101, 202, 303, 404, 505])
    monkeypatch.setattr(identity.random, "randrange", lambda *a: next(draws))
    a = registry.caller("A").work
    await a.open("main")
    await a.close("main")
    await a.open("support")
    await a.close("support")
    await a.open("main")
    assert a._open["main"].seed == 101
    await a.open("main", proxy="")
    assert a._open["main"].seed == 303
    await a.open("main", seed=777)
    await a.close("main")
    await a.open("main")
    assert a._open["main"].seed == 777
    with pytest.raises(ValueError, match="Persistent profile"):
        await a.open("main", profile="")
    b = registry.caller("B").work
    await b.open("main")
    assert b._open["main"].seed == 404
    await a.close("main")
    registry.now[0] = 900
    await registry.reap_idle()
    assert "A" not in registry.entries
    fresh = registry.caller("A").work
    await fresh.open("main")
    assert fresh._open["main"].seed == 505
