"""LAN-only collection and Firefox's atomic, exact-certificate override store."""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from invisible_playwright_mcp.mcp import certificates, lan


def pin(host="192.168.2.1", port=443, der=b"abc"):
    return certificates.Pin(lan.Endpoint(host, port), certificates.fingerprint(der),
                            "device", "factory")


def answers(*addresses):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
             6, "", (ip, 443)) for ip in addresses]


@pytest.mark.parametrize("entry", ["8.8.8.8", "127.0.0.1", "example.com",
                                   "http://192.168.1.1/"])
async def test_out_of_scope_entries_are_refused_without_connecting(monkeypatch, entry):
    fetch = MagicMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(certificates, "_fetch", fetch)
    with pytest.raises(ValueError, match="^refused: certificate entry") as caught:
        await certificates.prepare([entry])
    assert repr(entry) in str(caught.value)
    fetch.assert_not_called()


@pytest.mark.parametrize("addresses,allowed", [
    (["192.168.2.1"], ["192.168.2.1"]),
    (["10.1.1.1", "fd00::1", "::ffff:172.16.1.1"], ["10.1.1.1", "fd00::1", "::ffff:172.16.1.1"]),
    (["8.8.8.8"], None),
    (["192.168.2.1", "8.8.8.8"], None),
    # dual-stack device: its global IPv6 address is dropped, never fetched from
    (["192.168.0.1", "192.168.20.1", "2601:600:8f01:e4c0::1"], ["192.168.0.1", "192.168.20.1"]),
    (["2601:600:8f01:e4c0::1"], None),
    (["192.168.2.1", "::ffff:8.8.8.8"], None),
    (["127.0.0.1"], None),
    (["::1"], None),
    ([], None),
])
async def test_scope_checks_every_dns_answer(monkeypatch, addresses, allowed):
    resolver = AsyncMock(return_value=answers(*addresses))
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    endpoint = lan.parse_entry("router.powellhouse.net")
    if allowed:
        assert await certificates.resolve(endpoint, ("powellhouse.net",)) == allowed
    else:
        with pytest.raises(ValueError, match="DNS must resolve to LAN addresses"):
            await certificates.resolve(endpoint, ("powellhouse.net",))
    resolver.assert_awaited_once_with("router.powellhouse.net", 443, type=socket.SOCK_STREAM)


@pytest.mark.parametrize("error", [socket.gaierror("no host"), TimeoutError("DNS timed out")])
async def test_unresolvable_names_fail_closed(monkeypatch, error):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=error))
    with pytest.raises(ValueError, match="refused:.*missing.local.*DNS lookup failed"):
        await certificates.prepare(["missing.local"])


async def test_dns_timeout_is_bounded(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(60)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", slow)
    assert certificates.TIMEOUT == 5.0
    monkeypatch.setattr(certificates, "TIMEOUT", 0.01)
    with pytest.raises(ValueError, match="DNS lookup failed"):
        await certificates.resolve(lan.Endpoint("router.local"))


async def test_dns_is_not_cached(monkeypatch):
    resolver = AsyncMock(side_effect=[answers("192.168.2.1"), answers("8.8.8.8")])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    endpoint = lan.Endpoint("router.local")
    assert await certificates.resolve(endpoint) == ["192.168.2.1"]
    with pytest.raises(ValueError, match="DNS must resolve to LAN addresses"):
        await certificates.resolve(endpoint)


async def test_collection_runs_off_loop_and_deduplicates_addresses(monkeypatch):
    caller = threading.get_ident()
    fetched = []
    def fetch(endpoint, ips):
        assert threading.get_ident() != caller
        fetched.append((endpoint, ips))
        return pin(endpoint.host, endpoint.port)
    monkeypatch.setattr(certificates, "_fetch", fetch)
    result = await certificates.prepare(["192.168.2.1", "https://192.168.2.1/api"])
    assert result == (pin(),)
    assert fetched == [(lan.Endpoint("192.168.2.1"), ["192.168.2.1"])]


async def test_collection_failure_names_entry(monkeypatch):
    monkeypatch.setattr(certificates, "_fetch", MagicMock(side_effect=OSError("connection refused")))
    with pytest.raises(ValueError, match="refused:.*192.168.2.1.*connection refused"):
        await certificates.prepare(["192.168.2.1"])


def test_sha256_format():
    assert certificates.fingerprint(b"abc") == (
        "BA:78:16:BF:8F:01:CF:EA:41:41:40:DE:5D:AE:22:23:"
        "B0:03:61:A3:96:17:7A:9C:B4:10:FF:61:F2:00:15:AD")


@pytest.mark.parametrize("host,sni", [("router.local", "router.local"),
                                     ("192.168.2.1", None), ("fd00::1", None)])
def test_tls_capture_uses_validated_ip_and_sni_only_for_names(monkeypatch, host, sni):
    context = MagicMock()
    tls = context.wrap_socket.return_value.__enter__.return_value
    tls.getpeercert.return_value = b"abc"
    make_context = MagicMock(return_value=context)
    connect = MagicMock()
    monkeypatch.setattr(ssl, "SSLContext", make_context)
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(certificates, "_names", lambda der: ("device", "factory"))
    assert certificates._fetch(lan.Endpoint(host, 8443), ["10.0.0.1"]) == pin(host, 8443)
    make_context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
    assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False
    assert connect.call_args.args == (("10.0.0.1", 8443),)
    assert 0 < connect.call_args.kwargs["timeout"] <= 5
    assert context.wrap_socket.call_args.kwargs == {"server_hostname": sni}
    tls.getpeercert.assert_called_once_with(binary_form=True)


def test_cn_decoder_uses_disposable_pem(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    def decode(path):
        assert Path(path).read_text().startswith("-----BEGIN CERTIFICATE-----")
        return {"subject": ((("commonName", "device"),),),
                "issuer": ((("organizationName", "factory"),), (("commonName", "CA"),))}
    monkeypatch.setattr(ssl._ssl, "_test_decode_cert", decode)
    assert certificates._names(b"abc") == ("device", "CA")
    assert list(tmp_path.iterdir()) == []


def test_cn_decoder_failure_cleans_temporary_pem(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(ssl._ssl, "_test_decode_cert", MagicMock(side_effect=ssl.SSLError("bad cert")))
    with pytest.raises(ssl.SSLError, match="bad cert"):
        certificates._names(b"abc")
    assert list(tmp_path.iterdir()) == []


def test_override_merge_is_atomic_private_and_ipv6_aware(monkeypatch, tmp_path):
    path = tmp_path / "cert_override.txt"
    original = ("# kept header\r\n"
                "unrelated.local:443:\tOID.other\tOTHER\t\r\n"
                "192.168.2.1:443:\tOID.old\tOLD\t\n"
                "# another comment\n"
                "192.168.2.1:443:\tOID.old\tDUPLICATE\t\n"
                "[fd00::1]:8443:\tOID.old\tOLD6\t\n")
    path.write_bytes(original.encode())
    replacing = os.replace
    replacements = []
    def replace(source, target):
        assert path.read_bytes() == original.encode(), "old file changed before atomic replacement"
        assert Path(source).parent == tmp_path
        if os.name == "posix":
            assert Path(source).stat().st_mode & 0o777 == 0o600
        replacements.append(target)
        replacing(source, target)
    monkeypatch.setattr(os, "replace", replace)
    certificates.write_overrides(str(tmp_path), (pin(), pin("fd00::1", 8443)))
    data = path.read_bytes().decode()
    assert data.startswith("# kept header\r\nunrelated.local:443:\tOID.other\tOTHER\t\r\n")
    assert "# another comment\n" in data
    assert "OLD" not in data and "DUPLICATE" not in data
    assert data.count("192.168.2.1:443:") == 1
    assert ("[fd00::1]:8443:\tOID.2.16.840.1.101.3.4.2.1\t%s\t\n"
            % pin().fingerprint) in data
    assert replacements == [path]
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_new_override_file_and_missing_newline(tmp_path):
    profile = tmp_path / "new-profile"
    certificates.write_overrides(str(profile), (pin(),))
    path = profile / "cert_override.txt"
    assert path.read_text().startswith("# PSM Certificate Override Settings file\n")
    path.write_text("# no final newline")
    certificates.write_overrides(str(profile), (pin(),))
    assert path.read_text().startswith("# no final newline\n192.168.2.1:443:")


def test_failed_atomic_replace_keeps_original_and_cleans_temp(monkeypatch, tmp_path):
    path = tmp_path / "cert_override.txt"
    path.write_text("# original\n")
    monkeypatch.setattr(os, "replace", MagicMock(side_effect=PermissionError("read only")))
    with pytest.raises(PermissionError, match="read only"):
        certificates.write_overrides(str(tmp_path), (pin(),))
    assert path.read_text() == "# original\n"
    assert list(tmp_path.iterdir()) == [path]
