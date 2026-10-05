"""Firefox accepts only the listed host/port's exact self-signed certificate."""
from __future__ import annotations

import http.server
import ipaddress
import os
import shutil
import ssl
import subprocess
import threading

import pytest
from invisible_playwright.async_api import Error

from invisible_playwright_mcp.mcp import actions, certificates, lan
from invisible_playwright_mcp.mcp.work import Work


BINARY = os.environ.get("STEALTHFOX_BINARY")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BINARY, reason="set STEALTHFOX_BINARY to a real patched Firefox binary"),
]


def _certificate(tmp_path, name):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is needed to generate the disposable self-signed certificates")
    key, cert = tmp_path / (name + ".key"), tmp_path / (name + ".pem")
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=" + name, "-addext", "subjectAltName=IP:127.0.0.1",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True, timeout=30)
    return key, cert


class _Server:
    def __init__(self, key, cert, port=0):
        hits = self.hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                body = b"<!doctype html><title>LAN TLS</title><p>private device</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_port
        self.url = "https://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.closed = False

    def close(self):
        if not self.closed:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            assert not self.thread.is_alive()
            self.closed = True


@pytest.mark.parametrize("host", ["127.0.0.1", "[::ffff:127.0.0.1]"])
async def test_exact_certificate_pin_is_not_a_tls_bypass(tmp_path, monkeypatch, host):
    # Loopback is admitted only in this test process, never in production scope.
    monkeypatch.setattr(lan, "NETWORKS", lan.NETWORKS + (ipaddress.ip_network("127.0.0.1/32"),))
    monkeypatch.setenv("STEALTHFOX_HEADLESS", "1")
    for name in ("STEALTHFOX_PROXY", "STEALTHFOX_PROFILE_DIR", "STEALTHFOX_SEED"):
        monkeypatch.delenv(name, raising=False)
    first_cert = _certificate(tmp_path, "device")
    changed_cert = _certificate(tmp_path, "changed-device")
    first, unlisted = _Server(*first_cert), _Server(*changed_cert)
    first.url = "https://%s:%d" % (host, first.port)
    unlisted.url = "https://%s:%d" % (host, unlisted.port)
    servers = [first, unlisted]
    work = Work("lan-e2e")
    profile = tmp_path / "profile"
    try:
        await work.open("main", profile=str(profile))
        with pytest.raises(Error, match="LAN host with an untrusted certificate"):
            await actions.navigate(work.session("main"), first.url, wait_until="load")
        assert first.hits == []

        opened = await work.open("main", accept_lan_certs=[first.url])
        fingerprint = certificates.fingerprint(ssl.PEM_cert_to_DER_cert(first_cert[1].read_text()))
        assert fingerprint in opened
        assert "subject CN 'device'" in opened and "issuer CN 'device'" in opened
        assert lan.parse_entry(first.url).authority in await work.status("main")
        assert "HTTP 200" in await actions.navigate(work.session("main"), first.url, wait_until="load")
        assert "private device" in await work.session("main").page().locator("body").inner_text()
        assert first.hits

        with pytest.raises(Error, match="LAN host with an untrusted certificate") as caught:
            await actions.navigate(work.session("main"), unlisted.url, wait_until="load")
        assert "it changed" not in str(caught.value)
        assert unlisted.hits == []

        first.close()
        replacement = _Server(*changed_cert, port=first.port)
        replacement.url = first.url
        servers.append(replacement)
        with pytest.raises(Error, match=r"not the one accepted at browser_open \(it changed\)"):
            await actions.navigate(work.session("main"), replacement.url + "/changed", wait_until="load")
        assert replacement.hits == []

        reopened = await work.open("main", accept_lan_certs=[replacement.url])
        assert "subject CN 'changed-device'" in reopened
        assert "HTTP 200" in await actions.navigate(work.session("main"), replacement.url)
    finally:
        await work.close_all()
        for server in servers:
            server.close()
