"""Draw whole identities that fit X11; never rewrite a fingerprint field."""
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from invisible_playwright import async_api

from invisible_playwright_mcp.mcp import identity
from invisible_playwright_mcp.mcp.work import Work


@pytest.fixture
def display(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":123.1")
    monkeypatch.setenv("XAUTHORITY", "/test/private/Xauthority")
    for name in ("STEALTHFOX_SEED", "STEALTHFOX_PROFILE_DIR", "STEALTHFOX_PROXY"):
        monkeypatch.delenv(name, raising=False)
    probe = Mock(return_value=SimpleNamespace(
        returncode=0, stdout="screen #0:\n  dimensions:    1600x900 pixels (1x1 millimeters)\n"))
    monkeypatch.setattr(subprocess, "run", probe)
    cached = getattr(identity, "display_size", None)
    if cached is not None:
        cached.cache_clear()
    yield probe
    if cached is not None:
        cached.cache_clear()


def sampler(monkeypatch, screens):
    sampled = []

    def generate(seed):
        sampled.append(seed)
        w, h, dpr = screens[seed]
        return SimpleNamespace(screen=SimpleNamespace(width=w, height=h, dpr=dpr))

    monkeypatch.setattr(async_api, "generate_profile", generate)
    return sampled


def test_reject_whole_draws_including_dpr_and_accept_exact_fit(display, monkeypatch):
    draws = iter([11, 12, 13, 14])
    monkeypatch.setattr(identity.random, "randrange", lambda *a: next(draws))
    sampled = sampler(monkeypatch, {
        11: (2560, 1440, 1), 12: (1000, 600, 2),
        13: (1600, 901, 1), 14: (1600, 900, 1)})
    seed, source = identity.resolve_seed(None, None, {})
    assert seed == 14
    assert sampled == [11, 12, 13, 14]
    assert source == "drawn for this session"
    assert display.call_count == 1


def test_x_probe_is_cached_and_uses_display_and_authority(display, monkeypatch):
    sampler(monkeypatch, {7: (1600, 900, 1)})
    monkeypatch.setattr(identity.random, "randrange", lambda *a: 7)
    for _ in range(3):
        assert identity.resolve_seed(None, None, {})[0] == 7
    assert display.call_count == 1
    args, kwargs = display.call_args
    assert args[0] == ["xdpyinfo", "-display", ":123.1"]
    assert kwargs["env"]["XAUTHORITY"] == "/test/private/Xauthority"
    assert kwargs["env"]["LC_ALL"] == "C"
    assert 0 < kwargs["timeout"] <= 5


@pytest.mark.parametrize("failure", [
    FileNotFoundError("xdpyinfo"),
    subprocess.TimeoutExpired("xdpyinfo", 2),
    SimpleNamespace(returncode=1, stdout=""),
    SimpleNamespace(returncode=0, stdout="dimensions: not available"),
])
def test_unreadable_display_is_unconstrained_and_logged_once(
        display, monkeypatch, caplog, failure):
    if isinstance(failure, Exception):
        display.side_effect = failure
    else:
        display.return_value = failure
    monkeypatch.setattr(identity.random, "randrange", lambda *a: 42)
    with caplog.at_level(logging.WARNING):
        assert identity.resolve_seed(None, None, {})[0] == 42
        assert identity.resolve_seed(None, None, {})[0] == 42
    assert display.call_count == 1
    assert len(caplog.records) == 1
    assert "unconstrained" in caplog.text


def test_rejection_has_a_finite_budget_and_does_not_write_identity(display, monkeypatch, tmp_path):
    sampler(monkeypatch, {7: (5120, 2880, 2)})
    draw = Mock(return_value=7)
    monkeypatch.setattr(identity.random, "randrange", draw)
    with pytest.raises(ValueError, match=r"No drawn.*1600x900.*attempts"):
        identity.resolve_seed(None, str(tmp_path / "profile"), {})
    assert 1 < draw.call_count <= 1024
    assert not (tmp_path / "profile" / identity.IDENTITY_FILE).exists()


@pytest.mark.parametrize("source", ["explicit", "environment", "remembered"])
async def test_fixed_oversized_seed_is_honoured_with_a_warning(display, monkeypatch, source):
    from test_owner_identity import FakeSession

    sampler(monkeypatch, {7: (2560, 1440, 2)})
    work = Work("display-test", factory=FakeSession)
    if source == "environment":
        monkeypatch.setenv("STEALTHFOX_SEED", "7")
        answer = await work.open("main")
    else:
        answer = await work.open("main", seed=7)
        if source == "remembered":
            await work.close("main")
            answer = await work.open("main")
    try:
        assert work._open["main"].seed == 7
        assert "window is larger than the display" in answer
        assert "5120x2880" in answer and "1600x900" in answer
    finally:
        await work.close_all()
