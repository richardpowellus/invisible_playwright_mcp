"""The certificate opt-in never expands LAN scope to public or loopback IPs."""
from __future__ import annotations

import pytest

from invisible_playwright_mcp.mcp import lan, plan


@pytest.mark.parametrize("host", [
    "10.0.0.0", "10.255.255.255", "172.16.0.0", "172.31.255.255",
    "192.168.0.0", "192.168.255.255", "169.254.0.0", "169.254.255.255",
    "fc00::", "fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "fe80::",
    "febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "::ffff:192.168.1.1",
    "::ffff:10.1.1.1", "::ffff:172.16.1.1", "::ffff:169.254.1.1",
])
def test_explicit_lan_ranges(host):
    assert lan.private_address(host)
    authority = "[" + host + "]" if ":" in host else host
    assert lan.private_address(authority)
    assert lan.in_scope("https://" + authority + ":8443/api")


@pytest.mark.parametrize("host", [
    "127.0.0.1", "::1", "0.0.0.0", "::", "100.64.0.1", "8.8.8.8",
    "172.32.0.1", "192.169.0.1", "9.255.255.255", "11.0.0.0",
    "172.15.255.255", "192.167.255.255", "169.253.255.255", "169.255.0.0",
    "fbff::1", "fe00::1", "fe7f::1", "fec0::1", "2001:4860:4860::8888",
    "ff02::1", "192.0.2.1", "::ffff:8.8.8.8", "::ffff:127.0.0.1",
    "::ffff:100.64.0.1", "::ffff:0.0.0.0",
])
def test_excluded_addresses(host):
    assert not lan.private_address(host)
    authority = "[" + host + "]" if ":" in host else host
    assert not lan.in_scope("https://" + authority + "/")


@pytest.mark.parametrize("host", [
    "local", "printer.local", "LOCAL.", "Printer.LOCAL.", "home.arpa",
    "router.home.arpa", "internal", "switch.internal", "lan", "base.lan",
    "powellhouse.net", "AP.POWELLHOUSE.NET.", "device.extra.test",
])
def test_suffixes_match_whole_labels(host):
    domains = lan.parse_domains(" powellhouse.NET.,extra.test  powellhouse.net ")
    assert domains == ("powellhouse.net", "extra.test")
    assert lan.in_scope("https://" + host + ":443/api", domains)


@pytest.mark.parametrize("host", [
    "evilpowellhouse.net", "powellhouse.net.evil.com", "evillocal",
    "localhost", "home.arpa.evil.com", "printer.example.com",
])
def test_suffix_neighbours_are_not_lan(host):
    assert not lan.in_scope("https://" + host, ("powellhouse.net",))


@pytest.mark.parametrize("url,allowed", [
    ("https://evil.com@192.168.1.1/", True),
    ("https://192.168.1.1@evil.com/", False),
    ("https://user:password@[fd00::1]:8443/", True),
    ("HTTPS://ROUTER.LOCAL./api", True),
    ("http://10.0.0.1/", True),
    ("https://8.8.8.8/", False),
    ("https://[broken/", False),
    ("https://10.0.0.1:bad/", False),
    ("https://10.0.0.1:65536/", False),
    ("file://router.local/etc/passwd", False),
    ("ftp://10.0.0.1/", False),
    ("data:text/html,https://10.0.0.1", False),
    ("javascript:alert(1)", False),
    ("about:blank", False),
    ("https:///192.168.1.1", False),
    ("//192.168.1.1/", False),
])
def test_url_parsing(url, allowed):
    assert lan.in_scope(url) is allowed


@pytest.mark.parametrize("value", [".", "*.example.com", "https://example.com",
                                    "two..labels", "-bad.lan", "bad-.lan"])
def test_invalid_domain_configuration_is_explicit(value):
    with pytest.raises(ValueError, match="invalid LAN domain suffix"):
        lan.parse_domains(value)


def test_extra_suffixes_are_read_by_the_plan():
    assert lan.parse_domains(" , \t\n") == ()
    result = plan.plan_session(env={"STEALTHFOX_LAN_DOMAINS": "PowellHouse.NET, office.test."})
    assert result.kwargs["lan_domains"] == ("powellhouse.net", "office.test")
    assert not result.kwargs.get("accept_lan_certs")


@pytest.mark.parametrize("entry,host,port", [
    ("router.local", "router.local", 443),
    ("ROUTER.LOCAL.:8443", "router.local.", 8443),
    ("192.168.2.1", "192.168.2.1", 443),
    ("https://192.168.2.1:444/api?q=1#part", "192.168.2.1", 444),
    ("HTTPS://Router.LOCAL/path", "router.local", 443),
    ("[fd00::1]", "fd00::1", 443),
    ("[fd00::1]:8443", "fd00::1", 8443),
    ("https://[fc00:0:0:0::1]/api", "fc00::1", 443),
    ("https://[::ffff:192.168.1.1]:443/", "::ffff:c0a8:101", 443),
])
def test_certificate_entry_parsing(entry, host, port):
    endpoint = lan.parse_entry(entry)
    assert endpoint == lan.Endpoint(host, port)
    authority = ("[%s]" % host if ":" in host else host) + ":" + str(port)
    assert endpoint.authority == authority
    assert endpoint.url == "https://" + authority


@pytest.mark.parametrize("entry", [
    "", " ", "http://192.168.2.1/", "ftp://router.local/", "file:///etc/passwd",
    "https:///missing", "https://user@router.local/", "router.local:",
    "router.local:0", "router.local:65536", "router.local:bad", "router.local/path",
    "router.local?query", "router.local#fragment", "fd00::1", "[broken", "router.local\n",
    "https://router.local\\@evil.com", "https://[fe80::1%25eth0]/",
])
def test_invalid_certificate_entries(entry):
    with pytest.raises(ValueError):
        lan.parse_entry(entry)
