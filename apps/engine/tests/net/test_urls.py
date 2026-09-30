"""Normalización y validación de la dirección del sitio (ADR 0012, regla 1; spec §9.2)."""

from __future__ import annotations

import ipaddress

import pytest

from faro_engine.core.errors import FaroError
from faro_engine.net.urls import (
    MAX_URL_LENGTH,
    NetPolicy,
    Target,
    check_target,
    normalize_site_url,
    target_of,
)

STRICT = NetPolicy()
LOCAL = NetPolicy(allow_local=True, engine_port=50123)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tienda.com", "https://tienda.com"),
        ("  https://tienda.com/  ", "https://tienda.com"),
        ("HTTPS://TiEnDa.COM", "https://tienda.com"),
        ("https://tienda.com.", "https://tienda.com"),
        ("https://tienda.com:443/", "https://tienda.com"),
        ("https://Tienda.com/Blog/", "https://tienda.com/Blog"),
        ("https://tienda.com/tienda/wp//", "https://tienda.com/tienda/wp"),
        ("https://tienda.com/?p=1#arriba", "https://tienda.com"),
        ("https://tienda.com/blog?x=1", "https://tienda.com/blog"),
        ("https://tienda.com/café", "https://tienda.com/caf%C3%A9"),
        ("https://tienda.com/a%20b", "https://tienda.com/a%20b"),
        ("https://café.example", "https://xn--caf-dma.example"),
        ("https://ÑANDÚ.example/", "https://xn--and-6ma2c.example"),
        ("https://93.184.216.34", "https://93.184.216.34"),
        (
            "https://[2606:2800:220:1:248:1893:25c8:1946]/",
            "https://[2606:2800:220:1:248:1893:25c8:1946]",
        ),
    ],
)
def test_normalize_valid(raw: str, expected: str) -> None:
    assert normalize_site_url(raw, STRICT) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://tienda.com",
        "https://",
        "https://usuario:clave@tienda.com",
        "https://usuario@tienda.com",
        "https://tienda.com:8443",
        "https://tienda.com:0x1",
        "https://tienda .com",
        "https://tienda.com/a b",
        "https://tienda.com\\@otro.com",
        "https://tienda.com/../admin",
        "https://tienda.com/./a",
        "https://-tienda.com",
        "https://tienda_x.com",
        "https://a..b.com",
        "https://2130706433",
        "https://0x7f.1",
        "https://127.1",
        "https://" + "a" * 64 + ".com",
        "https://tienda.com/" + "a" * MAX_URL_LENGTH,
        "https://.",
        "mailto:ana@tienda.com",
    ],
)
def test_normalize_invalid_url(raw: str) -> None:
    with pytest.raises(FaroError) as info:
        normalize_site_url(raw, STRICT)
    assert info.value.code == "site.invalid_url"
    assert info.value.status == 400


def test_normalize_rejects_too_long_result() -> None:
    raw = "https://tienda.com/" + "ñ" * 700  # cada ñ ocupa 6 caracteres codificada
    assert len(raw) < MAX_URL_LENGTH
    with pytest.raises(FaroError) as info:
        normalize_site_url(raw, STRICT)
    assert info.value.code == "site.invalid_url"


@pytest.mark.parametrize("raw", ["http://tienda.com", "http://tienda.com:443"])
def test_http_requires_https(raw: str) -> None:
    with pytest.raises(FaroError) as info:
        normalize_site_url(raw, STRICT)
    assert info.value.code == "site.https_required"


@pytest.mark.parametrize(
    "raw",
    [
        "https://localhost",
        "http://localhost:8888",
        "https://app.localhost",
        "https://127.0.0.1",
        "https://[::1]",
        "https://10.0.0.5",
        "https://192.168.1.10",
        "https://169.254.169.254",
        "https://[fe80::1]",
        "https://[::ffff:10.0.0.1]",
    ],
)
def test_local_and_private_addresses_are_not_allowed(raw: str) -> None:
    with pytest.raises(FaroError) as info:
        normalize_site_url(raw, STRICT)
    assert info.value.code == "site.address_not_allowed"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8888", "http://localhost:8888"),
        ("localhost:8888", "https://localhost:8888"),
        ("http://127.0.0.1:8889/", "http://127.0.0.1:8889"),
        ("http://[::1]:8888", "http://[::1]:8888"),
        ("http://localhost", "http://localhost"),
        ("https://tienda.com", "https://tienda.com"),
    ],
)
def test_local_mode_allows_loopback(raw: str, expected: str) -> None:
    assert normalize_site_url(raw, LOCAL) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("http://localhost:50123", "site.address_not_allowed"),  # el puerto del motor
        ("http://127.0.0.1:50123", "site.address_not_allowed"),
        ("http://192.168.1.10:8888", "site.address_not_allowed"),
        ("http://10.0.0.1", "site.address_not_allowed"),
        ("http://tienda.com", "site.https_required"),
        ("https://tienda.com:8443", "site.invalid_url"),
    ],
)
def test_local_mode_keeps_other_rules(raw: str, code: str) -> None:
    with pytest.raises(FaroError) as info:
        normalize_site_url(raw, LOCAL)
    assert info.value.code == code


def test_local_mode_without_engine_port() -> None:
    policy = NetPolicy(allow_local=True)
    assert normalize_site_url("http://localhost:50123", policy) == "http://localhost:50123"


def test_target_of_request_url() -> None:
    target = target_of("https://Tienda.com/wp-json/faro/v1/posts?page=2", STRICT)
    assert target == Target("https", "tienda.com", 443, None, local=False)
    assert target.netloc == "tienda.com"
    local = target_of("http://localhost:8888/?rest_route=/faro/v1", LOCAL)
    assert local.local
    assert local.netloc == "localhost:8888"
    assert local.default_port == 80
    ipv6 = check_target("https", "2606:2800:220:1:248:1893:25c8:1946", None, STRICT)
    assert ipv6.ip == ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")
    assert ipv6.netloc == "[2606:2800:220:1:248:1893:25c8:1946]"


@pytest.mark.parametrize(
    ("scheme", "host"), [("ftp", "tienda.com"), ("https", None), ("https", "")]
)
def test_check_target_rejects(scheme: str, host: str | None) -> None:
    with pytest.raises(FaroError) as info:
        check_target(scheme, host, None, STRICT)
    assert info.value.code == "site.invalid_url"
