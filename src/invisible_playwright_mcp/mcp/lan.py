"""The explicit LAN scope, without environment reads or DNS side effects."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fc00::/7", "fe80::/10",
))
DOMAINS = ("local", "home.arpa", "internal", "lan")


def parse_domains(value: str) -> tuple[str, ...]:
    """Normalize the comma/space-separated extra suffixes decided at open."""
    domains = []
    for part in re.split(r"[,\s]+", value.strip()):
        if not part:
            continue
        domain = part.lower().rstrip(".")
        if not domain or len(domain) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in domain.split(".")):
            raise ValueError("invalid LAN domain suffix: %r" % part)
        if domain not in domains:
            domains.append(domain)
    return tuple(domains)


def address(host: str):
    """An IP literal, with bracketed and IPv4-mapped IPv6 normalized."""
    try:
        parsed = ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address):
        return parsed.ipv4_mapped or parsed
    return parsed


def private_address(host: str) -> bool:
    parsed = address(host)
    return parsed is not None and any(parsed in network for network in NETWORKS)


def url_host(url: str) -> str | None:
    """Only HTTP(S) authorities, never userinfo, paths or opaque URL schemes."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        # Accessing port validates it; it is not part of the host's scope.
        parsed.port
        return parsed.hostname.lower().rstrip(".")
    except ValueError:
        return None


def in_scope(url: str, domains: tuple[str, ...] = ()) -> bool:
    host = url_host(url)
    if not host:
        return False
    if address(host) is not None:
        return private_address(host)
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in DOMAINS + domains)


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int = 443

    @property
    def authority(self) -> str:
        host = "[" + self.host + "]" if ":" in self.host else self.host
        return "%s:%d" % (host, self.port)

    @property
    def url(self) -> str:
        return "https://" + self.authority


def parse_entry(entry: str) -> Endpoint:
    """An HTTPS URL or a bare host[:port], with an unambiguous override key."""
    if not isinstance(entry, str) or not entry or any(
            ord(c) <= 32 or ord(c) == 127 for c in entry):
        raise ValueError("expected an HTTPS URL or host[:port], without whitespace")
    explicit_url = "://" in entry
    parsed = urlsplit(entry if explicit_url else "https://" + entry)
    if parsed.scheme != "https":
        raise ValueError("only https:// certificate entries are accepted")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("a host is required; userinfo is not accepted")
    if not explicit_url and (parsed.path or parsed.query or parsed.fragment):
        raise ValueError("use https:// for an entry with a path, query or fragment")
    if parsed.netloc.endswith(":") or parsed.port == 0:
        raise ValueError("the port must be between 1 and 65535")
    host = parsed.hostname
    if "\\" in host or "%" in host:
        raise ValueError("escaped or zone-qualified hosts are not accepted")
    if ":" in host:
        ipv6 = ipaddress.IPv6Address(host)
        if ipv6.ipv4_mapped is not None:
            # Firefox URL keys use hex; Python 3.14 renders mapped IPs dotted.
            mapped = int(ipv6.ipv4_mapped)
            host = "::ffff:%x:%x" % (mapped >> 16, mapped & 0xffff)
        else:
            host = str(ipv6)
    else:
        host = host.encode("idna").decode("ascii").lower()
    return Endpoint(host, parsed.port or 443)
