"""browser_download keeps the file a page hands over.

Measured 2026-10-01: a bank's statement link was clicked and nothing came back.
The engine's Firefox saved what it downloaded into ~/Downloads, where no tool
looked; Juggler's download events never fire in it, so `expect_download` waits
forever; and a PDF the browser can show is not saved at all, it opens in the
viewer, often in a tab of its own. The unit tests hold the write guard and the
arbitration between a saved file and a shown document with doubles; the e2e
ones hold every shape against a real engine.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from invisible_playwright_mcp.mcp import actions
from invisible_playwright_mcp.mcp import session as session_mod
from test_a_file_is_uploaded_through_its_chooser import _link


@pytest.fixture
def root(tmp_path):
    out = tmp_path / "down"
    out.mkdir()
    return out


def _env(*dirs):
    return {actions.DOWNLOAD_DIRS_ENV: os.pathsep.join(str(d) for d in dirs)}


# --- where a file may be written ------------------------------------------------

def test_with_no_directory_named_downloads_are_off(root):
    with pytest.raises(RuntimeError, match="downloads are off"):
        actions.download_target(None, env={})


def test_with_no_save_to_the_first_directory_is_used(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert actions.download_target(None, env=_env(root, other)) == str(root)


def test_a_missing_directory_inside_a_root_is_made_owner_only(root):
    want = root / "a" / "b"
    assert actions.download_target(str(want), env=_env(root)) == str(want)
    assert want.is_dir()
    if os.name == "posix":
        assert (want.stat().st_mode & 0o777) == 0o700


def test_outside_every_root_is_refused_and_nothing_is_made(root, tmp_path):
    with pytest.raises(PermissionError, match="not inside"):
        actions.download_target(str(tmp_path / "elsewhere"), env=_env(root))
    with pytest.raises(PermissionError):
        actions.download_target(str(root / ".." / "elsewhere"), env=_env(root))
    assert not (tmp_path / "elsewhere").exists()


def test_a_link_inside_a_root_does_not_lead_out(root, tmp_path):
    away = tmp_path / "away"
    away.mkdir()
    _link(root / "link", away)
    with pytest.raises(PermissionError):
        actions.download_target(str(root / "link"), env=_env(root))
    with pytest.raises(PermissionError):
        actions.download_target(str(root / "link" / "deeper"), env=_env(root))
    assert not (away / "deeper").exists()


def test_hidden_and_relative_directories_are_refused(root):
    with pytest.raises(PermissionError, match="hidden"):
        actions.download_target(str(root / ".ssh"), env=_env(root))
    with pytest.raises(ValueError, match="absolute"):
        actions.download_target("down", env=_env(root))


def test_a_root_that_is_a_symlink_turns_downloads_off(root, tmp_path):
    link = tmp_path / "staging"
    _link(link, root)
    with pytest.raises(RuntimeError, match="downloads are off"):
        actions.download_target(None, env=_env(link))


# --- what a file is called and what it is ------------------------------------

@pytest.mark.parametrize("given,want", [
    ("statement.pdf", "statement.pdf"),
    ("../../etc/passwd", "passwd"),
    ("..\\..\\boot.ini", "boot.ini"),
    (".bashrc", "bashrc"),
    ("a\x00b\nc.pdf", "abc.pdf"),
    ('re:port?"1".pdf', "report1.pdf"),
    ("", "download"),
    ("...", "download"),
    ("CON.pdf", "_CON.pdf"),
    ("report.pdf. ", "report.pdf"),
])
def test_a_suggested_name_is_made_safe(given, want):
    assert actions.safe_filename(given) == want


def test_a_long_name_keeps_its_extension():
    name = actions.safe_filename("x" * 400 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode()) <= 200


@pytest.mark.parametrize("header,want", [
    ('attachment; filename="Aug 2026.pdf"', "Aug 2026.pdf"),
    ("attachment; filename=plain.csv", "plain.csv"),
    ("attachment; filename=\"x.pdf\"; filename*=UTF-8''%E2%82%AC%20rates.pdf", "€ rates.pdf"),
    ("inline", None),
    ("", None),
])
def test_content_disposition_names_the_file(header, want):
    assert actions._disposition_name(header) == want


def test_the_bytes_say_what_they_are_before_the_name_does():
    assert actions.sniff_mime(b"%PDF-1.7", "x.bin") == "application/pdf"
    assert actions.sniff_mime(b"a,b\n", "x.csv") == "text/csv"
    assert actions.sniff_mime(b"??", "noext", "text/plain; charset=utf-8") == "text/plain"
    assert actions.sniff_mime(b"??", "noext") == "application/octet-stream"


# --- writing it -------------------------------------------------------------------

def test_nothing_is_overwritten(root):
    (root / "s.pdf").write_bytes(b"old")
    first = actions.save_download(str(root), "s.pdf", [b"%PDF-1"])
    second = actions.save_download(str(root), "s.pdf", [b"%PDF-2"])
    assert (root / "s.pdf").read_bytes() == b"old"
    assert first["filename"] == "s (1).pdf" and second["filename"] == "s (2).pdf"
    if os.name == "posix":
        assert (root / "s (1).pdf").stat().st_mode & 0o777 == 0o600
    assert first["size"] == 6 and len(first["sha256"]) == 64


def test_a_file_over_the_limit_leaves_nothing_behind(root, monkeypatch):
    monkeypatch.setattr(actions, "DOWNLOAD_MAX_BYTES", 4)
    with pytest.raises(ValueError, match="over the"):
        actions.save_download(str(root), "big.bin", [b"abc", b"def"])
    assert list(root.iterdir()) == []


def test_a_file_still_being_written_has_not_landed(tmp_path):
    (tmp_path / "old.pdf").write_bytes(b"x")
    before = {"old.pdf"}
    (tmp_path / "s.pdf").write_bytes(b"")
    (tmp_path / "s.pdf.part").write_bytes(b"half")
    assert actions.landed(str(tmp_path), before) == []
    (tmp_path / "s.pdf.part").unlink()
    (tmp_path / "s.pdf").write_bytes(b"done")
    assert actions.landed(str(tmp_path), before) == [(str(tmp_path / "s.pdf"), 4)]


# --- a browser saves into a directory of its own ---------------------------------

def test_a_browser_saves_downloads_privately_and_removes_them_with_it(monkeypatch):
    seen = {}

    class _IPW:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return _Ctx()

        async def __aexit__(self, *a):
            return False

    class _Ctx:
        pages = []

        async def close(self):
            pass

    monkeypatch.setattr(session_mod, "InvisiblePlaywright", _IPW)
    s = session_mod.StealthSession(seed=1, extra_prefs={"keep.me": 1})

    async def run():
        await s.start()
        where = s.downloads
        assert Path(where).is_dir()
        if os.name == "posix":
            assert (Path(where).stat().st_mode & 0o777) == 0o700
        prefs = seen["extra_prefs"]
        assert prefs["keep.me"] == 1
        assert prefs["browser.download.dir"] == where
        assert prefs["browser.download.folderList"] == 2
        await s.close()
        return where

    where = asyncio.run(run())
    assert not os.path.exists(where) and s.downloads is None


# --- the action, against a double -----------------------------------------------

class _Response:
    def __init__(self, url, headers, body=b"%PDF-1.4 shown", fails=False, navigation=True):
        self.url, self.headers, self._body, self._fails = url, headers, body, fails
        self.request = SimpleNamespace(is_navigation_request=lambda: navigation)
        self.complete = True

    async def finished(self):
        assert self.complete, "body was read before request completion"

    async def body(self):
        if self._fails:
            raise RuntimeError("no body for a download")
        return self._body


class _Context:
    def __init__(self):
        self.listeners = []
        self._registered = {}
        self.page = None

    def on(self, event, fn):
        assert event == "response"
        def emit(response):
            if not hasattr(response, "frame"):
                response.frame = SimpleNamespace(page=self.page)
            fn(response)
        self._registered[fn] = emit
        self.listeners.append(emit)

    def remove_listener(self, event, fn):
        self.listeners.remove(self._registered.pop(fn))


class _Page:
    def __init__(self, session, effect):
        self.session, self.effect = session, effect
        self.context = _Context()
        self.context.page = self
        self.url = "https://bank.example/statements"
        self.clicks = []
        self.mouse = self

    async def wait_for_selector(self, selector, **kw):
        return True

    async def click(self, selector, timeout=None):
        self.clicks.append(selector)
        await self.effect(self)

    async def move(self, x, y, steps=1):
        self.clicks.append((x, y))

    async def down(self):
        pass

    async def up(self):
        await self.effect(self)


class _Session:
    def __init__(self, landing, effect):
        self.downloads = str(landing)
        self._page = _Page(self, effect)
        self.extra = []

    def page(self):
        return self._page

    def pages(self):
        return [self._page] + self.extra


@pytest.fixture
def env(root, monkeypatch, tmp_path):
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))
    monkeypatch.setattr(actions, "SAVED_COPY_GRACE_S", 0.3)
    monkeypatch.setattr(actions, "_POLL_S", 0.05)
    landing = tmp_path / "landing"
    landing.mkdir()
    return root, landing


def _download(session, **kw):
    return json.loads(asyncio.run(actions.download(session, **kw)))


def test_a_file_the_browser_saved_is_moved_out_of_its_directory(env):
    root, landing = env

    async def saves(page):
        (landing / "Statement Aug.pdf").write_bytes(b"%PDF-1.4 saved")

    s = _Session(landing, saves)
    out = _download(s, selector="#aug")
    assert out["saved"] == str(root / "Statement Aug.pdf")
    assert out["mime"] == "application/pdf" and out["size"] == 14
    assert out["from"] == "the browser's download"
    assert list(landing.iterdir()) == []
    assert s.page().context.listeners == []


def test_a_saved_copy_wins_over_the_tab_that_shows_it(env):
    root, landing = env

    async def both(page):
        for fn in page.context.listeners:
            fn(_Response("https://bank.example/s.pdf",
                         {"content-type": "application/pdf",
                          "content-disposition": 'attachment; filename="s.pdf"'}))
        (landing / "s.pdf").write_bytes(b"%PDF-1.4 saved")

    out = _download(_Session(landing, both), selector="#aug")
    assert out["from"] == "the browser's download"
    assert (root / "s.pdf").read_bytes() == b"%PDF-1.4 saved"


def test_a_document_only_shown_is_taken_from_the_response(env):
    root, landing = env

    async def shows(page):
        for fn in page.context.listeners:
            fn(_Response("https://cdn.example/v1/doc?sig=abc",
                         {"content-type": "application/pdf"}))
            fn(_Response("https://bank.example/app.js",
                         {"content-type": "text/javascript"}))

    out = _download(_Session(landing, shows), x=10, y=20)
    assert out["from"] == "the document the page showed"
    assert out["filename"] == "doc.pdf" and out["url"].startswith("https://cdn.example/")
    assert (root / "doc.pdf").read_bytes() == b"%PDF-1.4 shown"


def test_a_response_with_no_body_waits_for_its_saved_copy(env):
    root, landing = env

    async def diverted(page):
        for fn in page.context.listeners:
            fn(_Response("https://bank.example/x.csv",
                         {"content-type": "text/csv",
                          "content-disposition": "attachment; filename=x.csv"},
                         fails=True))

        async def later():
            await asyncio.sleep(0.6)
            (landing / "x.csv").write_bytes(b"a,b\n")
        asyncio.get_running_loop().create_task(later())

    out = _download(_Session(landing, diverted), selector="#csv")
    assert out["filename"] == "x.csv" and out["mime"] == "text/csv"


def test_a_click_that_hands_over_nothing_saves_nothing(env):
    root, landing = env

    async def nothing(page):
        pass

    with pytest.raises(RuntimeError, match="neither saved a file nor showed one"):
        _download(_Session(landing, nothing), selector="#x", timeout_seconds=0.3)
    assert list(root.iterdir()) == []


def test_a_refused_destination_clicks_nothing(env, tmp_path):
    root, landing = env

    async def saves(page):
        raise AssertionError("clicked")

    s = _Session(landing, saves)
    with pytest.raises(PermissionError):
        _download(s, selector="#aug", save_to=str(tmp_path / "elsewhere"))
    assert s.page().clicks == []


@pytest.mark.parametrize("kw", [{}, {"selector": "#a", "x": 1, "y": 2}, {"x": 1},
                                {"selector": "#a", "timeout_seconds": 0},
                                {"selector": "#a", "timeout_seconds": 10_000}])
def test_the_target_is_a_selector_or_a_point(env, kw):
    root, landing = env
    with pytest.raises(ValueError):
        _download(_Session(landing, None), **kw)


# --- against a real engine ---------------------------------------------------------

PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages"
       b"/Kids[3 0 R]/Count 1>>endobj 3 0 obj<</Type/Page/Parent 2 0 R/MediaBox"
       b"[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")

PAGE = b"""<!doctype html><html><body>
<a id="attachment" href="/attachment.pdf">attachment</a>
<a id="csv" href="/rows.csv">csv</a>
<a id="named" href="/inline.pdf" download="named.pdf">download attribute</a>
<a id="tab" href="/inline.pdf" target="_blank">new tab</a>
<button id="opener" onclick="window.open('/inline.pdf')">window.open</button>
<a id="same" href="/inline.pdf">same tab</a>
<button id="inert">nothing</button>
<button id="fetch" onclick="fetch('/binary')">fetch data</button>
<button id="xhr" onclick="let r=new XMLHttpRequest();r.open('GET','/binary');r.send()">XHR data</button>
<button id="blob" onclick="
 const url=window.URL.createObjectURL(new Blob(['blob-'.repeat(262144)], {type:'text/plain'}));
 const a=document.createElement('a');a.href=url;a.download='blob.txt';a.click();
 setTimeout(() => window.URL.revokeObjectURL(url), 0)">blob</button>
</body></html>"""


@pytest.fixture(scope="module")
def url():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/attachment.pdf":
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'attachment; filename="Aug 2026.pdf"')
                body = PDF
            elif self.path == "/rows.csv":
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", 'attachment; filename="rows.csv"')
                body = b"a,b\n1,2\n"
            elif self.path == "/inline.pdf":
                self.send_header("Content-Type", "application/pdf")
                body = PDF
            elif self.path == "/binary":
                self.send_header("Content-Type", "application/octet-stream")
                body = b"not a download"
            else:
                self.send_header("Content-Type", "text/html; charset=utf-8")
                body = PAGE
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


async def _with_browser(url, body):
    from invisible_playwright_mcp.mcp.plan import plan_session
    from invisible_playwright_mcp.mcp.session import StealthSession
    session = StealthSession(**plan_session().kwargs)
    await session.start()
    try:
        await actions.navigate(session, url)
        return await body(session)
    finally:
        await session.close()


@pytest.mark.e2e
def test_every_shape_of_hand_over_is_kept(url, root, monkeypatch):
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))
    home = os.path.expanduser("~/Downloads")
    before_home = set(os.listdir(home)) if os.path.isdir(home) else set()

    async def body(session):
        outs = {}
        for sel in ("#attachment", "#csv", "#named", "#tab", "#opener"):
            outs[sel] = json.loads(await actions.download(session, sel, timeout_seconds=20))
            assert len(session.pages()) == 1, (sel, [p.url for p in session.pages()])
        with pytest.raises(RuntimeError, match="neither saved a file nor showed one"):
            await actions.download(session, "#inert", timeout_seconds=3)
        outs["#same"] = json.loads(await actions.download(session, "#same", timeout_seconds=20))
        return outs

    outs = asyncio.run(_with_browser(url, body))
    assert outs["#attachment"]["filename"] == "Aug 2026.pdf"
    assert outs["#csv"]["filename"] == "rows.csv" and outs["#csv"]["mime"] == "text/csv"
    assert outs["#named"]["filename"] == "named.pdf"
    for sel in ("#tab", "#opener", "#same"):
        assert outs[sel]["from"] == "the document the page showed", sel
    assert outs["#same"]["notes"] == ["this page now shows the file; browser_navigate to leave it"]
    for sel, out in outs.items():
        if out["mime"] == "application/pdf":
            assert open(out["saved"], "rb").read() == PDF, sel
            assert out["size"] == len(PDF), sel
    after_home = set(os.listdir(home)) if os.path.isdir(home) else set()
    assert after_home == before_home, "a download still went to ~/Downloads"


class _Flaky(_Response):
    def __init__(self, *a, fail_first=1, **kw):
        super().__init__(*a, **kw)
        self.left = fail_first

    async def body(self):
        if self.left:
            self.left -= 1
            raise RuntimeError('Request "17" is not found')
        return self._body


def test_a_body_that_fails_once_is_asked_again(env):
    root, landing = env

    async def settles(page):
        for fn in page.context.listeners:
            fn(_Flaky("https://cdn.example/doc.pdf", {"content-type": "application/pdf"}))

    out = _download(_Session(landing, settles), selector="#aug", timeout_seconds=5)
    assert out["from"] == "the document the page showed"


def test_a_failure_says_what_the_click_opened(env):
    root, landing = env

    class _Tab:
        url = "https://cdn.example/doc.pdf"

        async def evaluate(self, js):
            return "application/pdf"

    async def opens(page):
        page.session.extra.append(_Tab())
        for fn in page.context.listeners:
            fn(_Flaky("https://cdn.example/doc.pdf", {"content-type": "application/pdf"},
                      fail_first=99))

    with pytest.raises(RuntimeError) as err:
        _download(_Session(landing, opens), selector="#aug", timeout_seconds=1.5)
    said = str(err.value)
    assert "a new tab is on https://cdn.example/doc.pdf showing application/pdf" in said
    assert 'could not be read: https://cdn.example/doc.pdf: Request "17" is not found' in said


@pytest.mark.parametrize("kind", ["application/octet-stream", "application/pdf"])
def test_fetch_or_xhr_is_not_a_download(env, kind):
    root, landing = env

    async def fetch(page):
        for fn in page.context.listeners:
            fn(_Response("https://bank.example/data", {"content-type": kind}, navigation=False))

    with pytest.raises(RuntimeError, match="nothing was saved"):
        _download(_Session(landing, fetch), selector="#fetch", timeout_seconds=0.6)
    assert list(root.iterdir()) == []


def test_an_empty_blob_placeholder_is_never_kept(env):
    root, landing = env

    async def saves(page):
        (landing / "blob.txt").touch()

    with pytest.raises(RuntimeError, match="nothing was saved"):
        _download(_Session(landing, saves), selector="#blob", timeout_seconds=0.3)
    assert list(root.iterdir()) == []


def test_a_random_part_suffix_keeps_the_placeholder_pending(env):
    root, landing = env
    data = b"the complete blob after the delayed write"

    async def run():
        async def saves(page):
            (landing / "blob.txt").write_bytes(b"partial")
            (landing / "blob.txt.random.part").write_bytes(b"working")

        async def finish():
            await asyncio.sleep(0.3)
            (landing / "blob.txt").write_bytes(data)
            (landing / "blob.txt.random.part").unlink()

        task = asyncio.create_task(finish())
        try:
            return json.loads(await actions.download(_Session(landing, saves), "#blob"))
        finally:
            await task

    out = asyncio.run(run())
    assert Path(out["saved"]).read_bytes() == data
    assert out["size"] == len(data)
    assert out["sha256"] == hashlib.sha256(data).hexdigest()


def test_an_empty_or_truncated_response_is_not_kept(env):
    root, landing = env

    async def shows(page):
        for fn in page.context.listeners:
            fn(_Response("https://bank.example/doc.pdf",
                         {"content-type": "application/pdf", "content-length": "100"}, b"%PDF-half"))

    with pytest.raises(RuntimeError, match="nothing was saved"):
        _download(_Session(landing, shows), selector="#tab", timeout_seconds=0.6)
    assert list(root.iterdir()) == []


def test_a_destination_works_without_posix_directory_descriptors(root, monkeypatch):
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(os, "supports_fd", set())
    target = root / "PDF reports"
    assert actions.download_target(str(target), env=_env(root)) == str(target)
    out = actions.save_download(str(target), "report.pdf", [PDF], env=_env(root))
    assert Path(out["path"]).read_bytes() == PDF


def test_an_empty_file_is_never_reported_as_saved(root):
    with pytest.raises(ValueError, match="empty"):
        actions.save_download(str(root), "empty.txt", [b""])
    assert list(root.iterdir()) == []


def test_an_existing_other_tabs_navigation_is_not_kept(env):
    root, landing = env
    other = SimpleNamespace(url="https://other.example/")

    async def navigate(page):
        response = _Response("https://other.example/doc.pdf", {"content-type": "application/pdf"})
        response.frame = SimpleNamespace(page=other)
        for fn in page.context.listeners:
            fn(response)

    session = _Session(landing, navigate)
    session.extra.append(other)
    with pytest.raises(RuntimeError, match="nothing was saved"):
        _download(session, selector="#inert", timeout_seconds=0.6)
    assert list(root.iterdir()) == []


def test_a_response_is_read_only_after_the_transfer_finishes(env):
    root, landing = env
    complete = b"%PDF-complete"

    class Pending(_Response):
        async def finished(self):
            await asyncio.sleep(0.1)
            self._body = complete

    async def navigate(page):
        for fn in page.context.listeners:
            fn(Pending("https://bank.example/doc.pdf", {"content-type": "application/pdf"}, b"%PDF-half"))

    out = _download(_Session(landing, navigate), selector="#tab")
    assert Path(out["saved"]).read_bytes() == complete


def test_a_file_url_name_uses_uri_rules_not_host_path_rules(tmp_path):
    path = tmp_path / "PDF reports" / "a #1.pdf"
    response = _Response(path.as_uri(), {"content-type": "application/pdf"})
    assert actions._response_name(response) == "a #1.pdf"


@pytest.mark.e2e
@pytest.mark.parametrize("repeat", range(20))
def test_blob_download_is_complete_after_immediate_url_revocation(url, root, monkeypatch, repeat):
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))
    data = b"blob-" * 262144

    async def body(session):
        errors = []
        session.page().on("pageerror", lambda error: errors.append(str(error)))
        try:
            out = json.loads(await actions.download(session, "#blob", timeout_seconds=20))
        except RuntimeError as exc:
            files = [(p.name, p.stat().st_size) for p in Path(session.downloads).iterdir()]
            pytest.fail(f"{exc}; landing={files}; page errors={errors}")
        assert out["size"] == len(data)
        assert out["sha256"] == hashlib.sha256(data).hexdigest()
        assert Path(out["saved"]).read_bytes() == data

    asyncio.run(_with_browser(url, body))


@pytest.mark.e2e
@pytest.mark.parametrize("selector", ["#fetch", "#xhr"])
def test_real_fetch_and_xhr_never_save_a_file(url, root, monkeypatch, selector):
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(root))

    async def body(session):
        with pytest.raises(RuntimeError, match="nothing was saved"):
            await actions.download(session, selector, timeout_seconds=1)
        assert list(root.iterdir()) == []

    asyncio.run(_with_browser(url, body))
