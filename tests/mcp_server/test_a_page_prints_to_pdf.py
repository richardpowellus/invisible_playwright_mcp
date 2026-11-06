"""window.print() saves a PDF; it never opens a dialog that wedges the browser.

2026-11-06: a Print button on the Texas Comptroller's account-status page called
window.print(). Firefox opened its print preview, the click failed with "the
page did not handle mouse event", and every later click, navigation and resize
hung until browser_close. The e2e test below failed that way before
`print_prefs` existed (click refused after 6 s, print() never returned).
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading

import pytest

from invisible_playwright_mcp.mcp import actions, server
from invisible_playwright_mcp.mcp.session import PRINT_FILE, StealthSession, print_prefs
from invisible_playwright_mcp.mcp.work import Work


def test_the_prefs_print_silently_to_a_file_in_the_given_directory(tmp_path):
    prefs = print_prefs(str(tmp_path))
    assert prefs["print.always_print_silent"] is True
    assert prefs["print_printer"] == "Mozilla Save to PDF"
    assert prefs["print.printer_Mozilla_Save_to_PDF.print_to_file"] is True
    assert prefs["print.printer_Mozilla_Save_to_PDF.print_to_filename"] == str(tmp_path / PRINT_FILE)


def test_a_pdf_still_being_written_is_not_collected(tmp_path):
    path = tmp_path / PRINT_FILE
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 2000)
    assert actions._whole_pdf(str(path)) == 0
    path.write_bytes(path.read_bytes() + b"\n%%EOF\n")
    assert actions._whole_pdf(str(path)) == path.stat().st_size


class _Session:
    def __init__(self, prints):
        self.prints = prints

    def page(self):
        class Page:
            async def title(self):
                return "Account Status"
        return Page()


async def test_a_page_print_is_kept_and_said(tmp_path, monkeypatch):
    prints, keep = tmp_path / "prints", tmp_path / "keep"
    prints.mkdir()
    keep.mkdir()
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(keep))
    session = _Session(str(prints))
    assert await actions.collect_print(session) == ""
    (prints / PRINT_FILE).write_bytes(b"%PDF-1.4\nbody\n%%EOF\n")
    said = await actions.collect_print(session)
    assert "no dialog opened" in said and str(keep / "Account Status.pdf") in said
    assert not (prints / PRINT_FILE).exists()


async def test_a_print_with_downloads_off_is_discarded_and_said(tmp_path):
    (tmp_path / PRINT_FILE).write_bytes(b"%PDF-1.4\nbody\n%%EOF\n")

    def off():
        raise RuntimeError("downloads are off")
    said = await actions.collect_print(_Session(str(tmp_path)), off)
    assert "was not kept: downloads are off" in said
    assert not (tmp_path / PRINT_FILE).exists()


PAGE = b"""<!doctype html><title>Account Status</title>
<h1>Franchise tax account status</h1>
<button id="print" onclick="window.print(); document.getElementById('out').textContent = 'printed'">Print</button>
<div id="out">waiting</div>"""


@pytest.fixture
def print_site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *_args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/" % httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.e2e
@pytest.mark.skipif(sys.platform not in ("linux", "win32"),
                    reason="the engine supports Linux and Windows, not darwin")
async def test_a_print_button_saves_a_pdf_and_the_browser_keeps_working(
        print_site, tmp_path, monkeypatch):
    options = {"headless": True}
    if os.environ.get("STEALTHFOX_BINARY"):
        options["binary_path"] = os.environ["STEALTHFOX_BINARY"]
    monkeypatch.setenv(actions.DOWNLOAD_DIRS_ENV, str(tmp_path))
    session = StealthSession(**options)
    work = Work("print-e2e")
    monkeypatch.setattr(server, "owners", None)
    monkeypatch.setattr(server, "work", work)
    await session.start()
    work._open["main"] = session
    try:
        async with asyncio.timeout(120):
            await server.browser_navigate(print_site)
            # Before print_prefs this click failed after 6 s and print() never
            # returned, so #out stayed "waiting".
            assert "clicked #print" in await server.browser_click("#print")
            assert await session.page().locator("#out").inner_text() == "printed"
            # The PDF lands about 0.1 s after print() returns, so it is the
            # NEXT text result that says so (here browser_read_text), as with
            # dialog news held back by an image result.
            for _ in range(50):
                said = await server.browser_read_text()
                if "no dialog opened" in said:
                    break
                await asyncio.sleep(0.2)
            assert "Saved the page as a PDF" in said, said
            kept = tmp_path / "Account Status.pdf"
            assert kept.read_bytes().startswith(b"%PDF-")
            # Not wedged: a navigation after the print still lands.
            assert "HTTP 200" in await server.browser_navigate(print_site)
            answer = json.loads(await server.browser_print_pdf(filename="status"))
            assert answer["saved"] == str(tmp_path / "status.pdf")
            assert answer["mime"] == "application/pdf" and answer["size"] > 1000
            assert (tmp_path / "status.pdf").read_bytes().startswith(b"%PDF-")
    finally:
        await session.close()
