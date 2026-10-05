"""Collect exact LAN certificates, then install Firefox's per-address overrides."""
from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import lan


TIMEOUT = 5.0
SHA256_OID = "OID.2.16.840.1.101.3.4.2.1"


@dataclass(frozen=True)
class Pin:
    endpoint: lan.Endpoint
    fingerprint: str
    subject_cn: str = ""
    issuer_cn: str = ""


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).digest().hex(":").upper()


async def resolve(endpoint: lan.Endpoint, domains: tuple[str, ...] = ()) -> list[str]:
    """Refuse unless the name is LAN and it resolves to LAN addresses; return those only."""
    if not lan.in_scope(endpoint.url, domains):
        raise ValueError("host is outside LAN scope")
    if lan.address(endpoint.host) is not None:
        return [endpoint.host]
    try:
        answers = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                endpoint.host, endpoint.port, type=socket.SOCK_STREAM), timeout=TIMEOUT)
    except (OSError, TimeoutError) as exc:
        raise ValueError("DNS lookup failed: %s" % exc) from exc
    addresses = list(dict.fromkeys(row[4][0] for row in answers))
    private = [ip for ip in addresses if lan.private_address(ip)]
    # A dual-stack LAN device also publishes its ISP-delegated global IPv6
    # address. That address is skipped, never fetched from: the pin is the
    # exact certificate the private address served. Any non-LAN IPv4 answer
    # still refuses, because no LAN name should have one.
    # (an unparsable answer counts as IPv4, and so refuses).
    public_v4 = [ip for ip in addresses if ip not in private
                 and getattr(lan.address(ip), "version", 4) == 4]
    if not private or public_v4:
        raise ValueError("DNS must resolve to LAN addresses (got %s)" % ", ".join(addresses or ["nothing"]))
    return private


def _names(der: bytes) -> tuple[str, str]:
    # The stdlib decoder reads PEM from disk; no extra certificate dependency.
    pem = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    path = Path(pem.name)
    try:
        with pem:
            pem.write(ssl.DER_cert_to_PEM_cert(der))
        decoded = ssl._ssl._test_decode_cert(str(path))
    finally:
        path.unlink()
    def cn(field):
        return next((value for rdn in decoded.get(field, ())
                     for name, value in rdn if name == "commonName"), "")
    return cn("subject"), cn("issuer")


def _fetch(endpoint: lan.Endpoint, addresses: list[str]) -> Pin:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    sni = endpoint.host if lan.address(endpoint.host) is None else None
    deadline = time.monotonic() + TIMEOUT
    failure: OSError = TimeoutError("certificate connection timed out")
    for ip in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Connect to the validated IP, not a second DNS answer; SNI still
            # names the requested device when it was addressed by hostname.
            with socket.create_connection((ip, endpoint.port), timeout=remaining) as raw:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("certificate handshake timed out")
                raw.settimeout(remaining)
                with context.wrap_socket(raw, server_hostname=sni) as tls:
                    der = tls.getpeercert(binary_form=True)
                    if not der:
                        raise ssl.SSLError("the server returned no leaf certificate")
        except OSError as exc:
            failure = exc
            continue
        subject, issuer = _names(der)
        return Pin(endpoint, fingerprint(der), subject, issuer)
    raise failure


async def prepare(entries: list[str], domains: tuple[str, ...] = ()) -> tuple[Pin, ...]:
    pins: dict[lan.Endpoint, Pin] = {}
    for entry in entries:
        try:
            endpoint = lan.parse_entry(entry)
            addresses = await resolve(endpoint, domains)
            if endpoint not in pins:
                pins[endpoint] = await asyncio.to_thread(_fetch, endpoint, addresses)
        except (ValueError, OSError) as exc:
            raise ValueError("refused: certificate entry %r: %s" % (entry, exc)) from exc
    return tuple(pins.values())


def write_overrides(profile_dir: str, pins: tuple[Pin, ...]) -> None:
    # ⛔ ignore_https_errors cannot work on this stealth build: Firefox's
    # security/manager/ssl/nsCertOverrideService.cpp gates it on IsDebugger()
    # or XPCSHELL_TEST_PROFILE_DIR, otherwise NS_ERROR_NOT_AVAILABLE. Pin only
    # each exact certificate/address using Firefox's own "Accept the Risk" file.
    path = Path(profile_dir) / "cert_override.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open(encoding="utf-8", newline="") as existing:
            lines = existing.readlines()
    except FileNotFoundError:
        lines = ["# PSM Certificate Override Settings file\n",
                 "# This is a generated file!  Do not edit.\n"]
    keys = {pin.endpoint.authority + ":" for pin in pins}
    kept = [line for line in lines if line.startswith("#") or line.split("\t", 1)[0] not in keys]
    content = "".join(kept)
    if content and not content.endswith(("\n", "\r")):
        content += "\n"
    content += "".join("%s:\t%s\t%s\t\n" % (
        pin.endpoint.authority, SHA256_OID, pin.fingerprint) for pin in pins)
    fd, temporary = tempfile.mkstemp(prefix=".cert_override-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            os.chmod(temporary, 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
