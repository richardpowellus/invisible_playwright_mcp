"""Coordinate clicks leave pointer travel and plain-click dwell to the engine."""
import asyncio
from types import SimpleNamespace

import pytest

from invisible_playwright_mcp.mcp import actions, masked


class Mouse:
    def __init__(self, calls):
        self.calls = calls

    async def click(self, x, y, **kwargs):
        self.calls.append(("click", x, y, kwargs))

    async def move(self, x, y, **kwargs):
        self.calls.append(("move", x, y, kwargs))

    async def down(self):
        self.calls.append(("down",))

    async def up(self):
        self.calls.append(("up",))


class Page:
    def __init__(self):
        self.calls = []
        self.mouse = Mouse(self.calls)
        self.block_wait = False
        self.wait_started = asyncio.Event()

    async def wait_for_timeout(self, milliseconds):
        self.calls.append(("wait", milliseconds))
        if self.block_wait:
            self.wait_started.set()
            await asyncio.Event().wait()


@pytest.fixture
def target(monkeypatch):
    page = Page()
    session = SimpleNamespace(page=lambda: page)

    async def guard(given):
        assert given is session
        page.calls.append(("guard",))

    async def screenshot(given):
        assert given is session
        page.calls.append(("screenshot",))
        return b"png"

    monkeypatch.setattr(masked, "guard_pixels", guard)
    monkeypatch.setattr(actions, "screenshot_png", screenshot)
    return session, page


async def test_plain_click_uses_engine_click_then_waits_and_captures(target):
    session, page = target

    assert await actions.click_at(session, 123.5, 45.25) == b"png"

    assert page.calls == [
        ("guard",),
        ("click", 123.5, 45.25, {}),
        ("wait", 400),
        ("screenshot",),
    ]


async def test_hold_moves_then_presses_waits_and_releases_before_capture(target):
    session, page = target

    assert await actions.click_at(session, 123.5, 45.25, hold_seconds=1.25) == b"png"

    assert page.calls == [
        ("guard",),
        ("move", 123.5, 45.25, {}),
        ("down",),
        ("wait", 1250),
        ("up",),
        ("wait", 400),
        ("screenshot",),
    ]


async def test_cancelling_the_hold_releases_the_button_and_propagates(target):
    session, page = target
    page.block_wait = True
    task = asyncio.create_task(actions.click_at(session, 123.5, 45.25, hold_seconds=1.25))
    try:
        await asyncio.wait_for(page.wait_started.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert page.calls[-1] == ("up",)
    assert page.calls.count(("up",)) == 1
    assert ("wait", 1250) in page.calls
    assert ("wait", 400) not in page.calls
    assert ("screenshot",) not in page.calls


async def test_download_press_uses_engine_click_without_a_screenshot(target):
    _, page = target

    await actions._press_at(page, 123.5, 45.25)

    assert page.calls == [("click", 123.5, 45.25, {})]


@pytest.mark.parametrize("operation", ["plain", "hold", "download"])
async def test_coordinate_actions_never_override_engine_interpolation(target, operation):
    session, page = target

    if operation == "download":
        await actions._press_at(page, 123.5, 45.25)
    else:
        await actions.click_at(session, 123.5, 45.25,
                               hold_seconds=1.25 if operation == "hold" else 0)

    moves = [call for call in page.calls if call[0] == "move"]
    assert all("steps" not in call[3] for call in moves), moves
