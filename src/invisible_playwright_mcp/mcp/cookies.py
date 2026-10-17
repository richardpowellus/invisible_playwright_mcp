"""Fork-only, credential-bearing export from the engine's cookie jar."""
from __future__ import annotations

import json
import os
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Literal

from invisible_playwright.async_api import TargetClosedError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import masked

TOOL = "browser_cookies_export"
DOMAINS_ENV = "STEALTHFOX_COOKIE_EXPORT_DOMAINS"
MAX_COOKIES = 200
MAX_BYTES = 256 * 1024
DISABLED = "cookie export is not enabled on this server"
FAILED = "cookie export failed; no cookies were exported"
active: ContextVar[bool] = ContextVar("cookie_export_active", default=False)
result: ContextVar[str | None] = ContextVar("cookie_export_result", default=None)
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                   r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", re.ASCII)


class ExportError(ValueError):
    """A fixed diagnostic that never includes engine data or arguments."""


class ExportArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    browser_handle: str
    origins: list[str] = Field(min_length=1, max_length=8)


class _Cookie(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True, allow_inf_nan=False)
    name: str
    value: str
    domain: str
    path: str
    expires: float
    http_only: bool = Field(alias="httpOnly")
    secure: bool
    same_site: Literal["Lax", "Strict", "None"] = Field(alias="sameSite")


def _host(value: str) -> str:
    host = value.lower()
    if len(host) > 253 or _HOST.fullmatch(host) is None:
        raise ExportError("cookie export requires DNS hosts without ports or URL components")
    try:
        ip_address(host)
    except ValueError:
        return host
    raise ExportError("cookie export requires DNS hosts, not IP addresses")


def matches_suffix(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def allowed_origins(origins: list[str]) -> tuple[list[str], list[str]]:
    configured = os.environ.get(DOMAINS_ENV, "")
    if not any(item.strip() for item in configured.split(",")):
        raise ExportError(DISABLED)
    try:
        suffixes = [_host(item.strip()) for item in configured.split(",")]
    except ExportError:
        raise ExportError("cookie export domain allowlist is invalid") from None
    hosts = []
    for origin in origins:
        if not origin.startswith("https://"):
            raise ExportError("cookie export requires https://<host> origins")
        host = _host(origin[len("https://"):])
        if not any(matches_suffix(host, suffix) for suffix in suffixes):
            raise ExportError("cookie export origin is outside the server allowlist")
        hosts.append(host)
    return hosts, suffixes


def arguments(arguments: dict) -> ExportArguments:
    try:
        return ExportArguments.model_validate(arguments)
    except ValidationError:
        raise ExportError(
            "cookie export requires only browser_handle (string) and origins "
            "(array of 1..8 strings)") from None


async def export(session, origins: list[str], hosts: list[str], suffixes: list[str]) -> str:
    try:
        # The engine supplies httpOnly cookies and URL applicability, never page JS.
        jar = await session._context.cookies(origins)
        selected = []
        now = time.time()
        for raw in jar:
            cookie = _Cookie.model_validate(raw)
            domain = cookie.domain.removeprefix(".").lower()
            if not any(matches_suffix(domain, suffix) for suffix in suffixes):
                continue
            domain = _host(domain)
            if not any(host == domain or (cookie.domain.startswith(".")
                                         and matches_suffix(host, domain)) for host in hosts):
                continue
            # Origins have the root path; narrower cookies must not be exported.
            if cookie.path != "/" or (cookie.expires != -1 and cookie.expires <= now):
                continue
            selected.append(cookie.model_dump())
            if len(selected) > MAX_COOKIES:
                raise ExportError("cookie export exceeds the 200-cookie limit")
        payload = json.dumps({
            "cookies": selected, "count": len(selected), "origins": origins,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ExportError("cookie export exceeds the 256 KiB limit")
        values = masked.registry(session)
        for cookie in selected:
            if len(cookie["value"]) >= 16:
                values.register(cookie["value"])
        session.network.protect(values)
        result.set(payload)
        return payload
    except ExportError:
        raise
    except TargetClosedError:
        raise TargetClosedError(FAILED) from None
    except Exception:
        # Engine/validation diagnostics can contain the entire jar.
        raise ExportError(FAILED) from None
