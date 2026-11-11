"""A download the ENGINE took is kept: by browser_download, and by a
browser_navigate to a url that answers with a file.

Since invisible-playwright 20f8a43 (2026-10-30) Juggler takes every download
itself, into the engine's own directory, and raises a page `download` event;
Firefox's landing area, which browser_download watched, stays empty. Measured
2026-11-11 on Indeed for Employers: "Download resume" (a fetch, a blob URL, a
synthetic click on a zero-size anchor) answered "neither saved a file nor
showed one", and a navigation to the resume API raised "Page.goto: Download is
starting". Both files were in the engine's `invpw-downloads-*`. The unit tests
hold the arbitration with doubles; the e2e ones the real engine.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import threading

import pytest

from invisible_playwright_mcp.mcp import actions
from invisible_playwright.async_api import Error

from test_a_downloaded_file_is_kept import (PDF, _Response, _Session, _download,
                                            _with_browser)


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "down"
    out.mkdir()
    return out


@pytest.fixture
def env(root, monkeypatch, tmp_path):
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))
    monkeypatch.setattr(actions, "SAVED_COPY_GRACE_S", 0.3)
    monkeypatch.setattr(actions, "_POLL_S", 0.05)
    landing = tmp_path / "landing"
    landing.mkdir()
    return root, landing


class _EngineDownload:
    def __init__(self, path, *, name="Resume.pdf", url="blob:https://employers.example/1",
                 failure=None, delay=0.0):
        self._path, self.suggested_filename, self.url = path, name, url
        self._failure, self._delay = failure, delay
        self.deleted = False

    async def path(self):
        await asyncio.sleep(self._delay)
        return self._path

    async def failure(self):
        return self._failure

    async def delete(self):
        self.deleted = True
        os.unlink(self._path)


class _HearingSession(_Session):
    """A session that hears the engine's downloads, as StealthSession does."""

    def __init__(self, landing, effect):
        super().__init__(landing, effect)
        self.download_count = 0
        self.events = []

    def take(self, download):
        self.download_count += 1
        self.events.append((self.download_count, download))

    def downloads_since(self, count):
        return [d for n, d in self.events if n > count]


def _artifact(tmp_path, body=b"%PDF-1.4 engine", name="6cc31745-uuid"):
    engine_dir = tmp_path / "invpw-downloads-x" / "default"
    engine_dir.mkdir(parents=True, exist_ok=True)
    path = engine_dir / name
    path.write_bytes(body)
    return path


def test_a_download_the_engine_took_is_kept_and_its_copy_dropped(env, tmp_path):
    root, landing = env
    made = []

    async def blob_click(page):
        d = _EngineDownload(str(_artifact(tmp_path)), delay=0.1)
        made.append(d)
        page.session.take(d)

    s = _HearingSession(landing, blob_click)
    out = _download(s, x=1490, y=572)
    assert out["saved"] == str(root / "Resume.pdf")
    assert out["from"] == "the browser's download"
    assert out["url"] == "blob:https://employers.example/1"
    assert (root / "Resume.pdf").read_bytes() == b"%PDF-1.4 engine"
    assert made[0].deleted


def test_a_download_from_before_the_click_is_not_taken(env, tmp_path):
    root, landing = env

    async def nothing(page):
        pass

    s = _HearingSession(landing, nothing)
    s.take(_EngineDownload(str(_artifact(tmp_path))))
    with pytest.raises(RuntimeError, match="neither saved a file nor showed one"):
        _download(s, selector="#x", timeout_seconds=0.3)
    assert list(root.iterdir()) == []


def test_an_unnamed_engine_download_gets_an_extension_from_its_bytes(env, tmp_path):
    root, landing = env

    async def click(page):
        page.session.take(_EngineDownload(str(_artifact(tmp_path)), name=""))

    out = _download(_HearingSession(landing, click), selector="#dl")
    assert out["filename"] == "download.pdf" and out["mime"] == "application/pdf"


def test_the_engines_download_wins_over_the_response_beside_it(env, tmp_path):
    root, landing = env

    async def both(page):
        for fn in page.context.listeners:
            fn(_Response("https://bank.example/s.pdf",
                         {"content-type": "application/pdf",
                          "content-disposition": 'attachment; filename="s.pdf"'},
                         fails=True))
        page.session.take(_EngineDownload(str(_artifact(tmp_path)), name="s.pdf",
                                          url="https://bank.example/s.pdf"))

    out = _download(_HearingSession(landing, both), selector="#s")
    assert out["from"] == "the browser's download"
    assert (root / "s.pdf").read_bytes() == b"%PDF-1.4 engine"


@pytest.mark.parametrize("kind", ["failed", "empty"])
def test_a_failed_or_empty_engine_download_is_reported_not_kept(env, tmp_path, kind):
    root, landing = env

    async def click(page):
        if kind == "failed":
            d = _EngineDownload(str(_artifact(tmp_path)), failure="canceled")
        else:
            d = _EngineDownload(str(_artifact(tmp_path, body=b"")))
        page.session.take(d)

    with pytest.raises(RuntimeError, match="neither saved a file nor showed one") as exc:
        _download(_HearingSession(landing, click), selector="#x", timeout_seconds=0.4)
    assert ("canceled" if kind == "failed" else "empty") in str(exc.value)
    assert list(root.iterdir()) == []


class _GotoPage:
    def __init__(self, session, effect):
        self.session, self.effect, self.url = session, effect, "https://employers.example/c"

    async def goto(self, url, **kw):
        await self.effect(self)
        raise Error("Page.goto: Download is starting")


class _NavSession:
    def __init__(self, effect):
        self._page = _GotoPage(self, effect)
        self.download_count = 0
        self.events = []

    def pages(self):
        return [self._page]

    def page(self):
        return self._page

    def take(self, download):
        _HearingSession.take(self, download)

    def downloads_since(self, count):
        return _HearingSession.downloads_since(self, count)


def test_a_navigation_that_becomes_a_download_keeps_the_file(env, tmp_path):
    root, landing = env

    async def starts(page):
        page.session.take(_EngineDownload(
            str(_artifact(tmp_path)), name="resume.pdf",
            url="https://employers.example/api/resume/v2/download?id=1", delay=0.2))

    answer = asyncio.run(actions.navigate(
        _NavSession(starts), "https://employers.example/api/resume/v2/download?id=1"))
    head, body = answer.split("\n", 1)
    assert "answered with a file, not a page" in head
    assert "stayed on https://employers.example/c" in head
    out = json.loads(body)
    assert out["saved"] == str(root / "resume.pdf") and out["size"] == 15
    assert out["mime"] == "application/pdf"


def test_a_navigation_download_with_downloads_off_says_so(env, tmp_path):
    async def starts(page):
        page.session.take(_EngineDownload(str(_artifact(tmp_path))))

    with pytest.raises(RuntimeError, match="downloads are off"):
        asyncio.run(actions.navigate(_NavSession(starts), "https://x.example/f",
                                     env={actions.DOWNLOAD_DIRS_ENV: ""}))


def test_other_navigation_errors_still_raise(env):
    class Fails(_GotoPage):
        async def goto(self, url, **kw):
            raise Error("NS_ERROR_CONNECTION_REFUSED")

    s = _NavSession(None)
    s._page = Fails(s, None)
    with pytest.raises(Error, match="CONNECTION_REFUSED"):
        asyncio.run(actions.navigate(s, "https://x.example/"))


# --- against a real engine ---------------------------------------------------------

LATE_BLOB_PAGE = b"""<!doctype html><html><body>
<div style="position:relative;display:inline-block">
<button id="dl" style="padding:20px">Download resume</button>
<a id="hidden" download="Resume.pdf" style="position:absolute;width:0;height:0;overflow:hidden"></a>
</div>
<script>
document.getElementById('dl').addEventListener('click', async () => {
  const blob = await (await fetch('/api/download?id=1')).blob();
  const a = document.getElementById('hidden');
  a.href = URL.createObjectURL(blob);
  a.click();
});
</script></body></html>"""


@pytest.fixture(scope="module")
def site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path.startswith("/api/download"):
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="resume.pdf"')
                body = PDF
            else:
                self.send_header("Content-Type", "text/html; charset=utf-8")
                body = LATE_BLOB_PAGE
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % srv.server_port
    srv.shutdown()
    srv.server_close()


@pytest.mark.e2e
def test_a_blob_made_after_the_click_is_kept(site, root, monkeypatch):
    """FileSaver's pattern, Indeed's resume button: fetch, blob URL, synthetic
    click on a hidden anchor. It used to be the documented known limit."""
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))

    async def body(session):
        return json.loads(await actions.download(session, "#dl", timeout_seconds=20))

    out = asyncio.run(_with_browser(site, body))
    assert out["filename"] == "Resume.pdf" and out["from"] == "the browser's download"
    assert open(out["saved"], "rb").read() == PDF


@pytest.mark.e2e
def test_navigating_to_an_attachment_keeps_it(site, root, monkeypatch):
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))

    async def body(session):
        answer = await actions.navigate(session, site + "api/download?id=1")
        return answer, session.page().url

    answer, where = asyncio.run(_with_browser(site, body))
    head, out = answer.split("\n", 1)
    assert "answered with a file, not a page" in head and where == site
    out = json.loads(out)
    assert out["filename"] == "resume.pdf"
    assert open(out["saved"], "rb").read() == PDF
