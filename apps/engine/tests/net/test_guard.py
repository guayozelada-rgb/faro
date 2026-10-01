"""Guardia SSRF: rangos prohibidos, resolución, IP fijada y SNI (ADR 0012, spec §9.2)."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import httpx
import pytest

from faro_engine.core.errors import FaroError
from faro_engine.net import guard
from faro_engine.net.guard import (
    AddressPins,
    is_forbidden_address,
    pin_request,
    resolve_target,
    system_resolver,
)
from faro_engine.net.urls import NetPolicy, Target, target_of
from tests.fakes.net import PUBLIC_IP, SITE_HOST, FakeResolver

FORBIDDEN = [
    # IPv4
    "0.0.0.0",
    "0.1.2.3",
    "10.0.0.1",
    "100.64.0.1",
    "100.127.255.254",
    "127.0.0.1",
    "127.255.255.254",
    "169.254.169.254",  # metadatos de nube
    "169.254.0.1",
    "172.16.0.1",
    "172.31.255.254",
    "192.0.0.8",
    "192.0.2.1",
    "192.88.99.1",
    "192.168.0.1",
    "198.18.0.1",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    # IPv6
    "::",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:8.8.8.8",  # mapeada: nunca se conecta así
    "64:ff9b::a00:1",  # NAT64 hacia 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 hacia 127.0.0.1
    "64:ff9b::808:808",  # NAT64 hacia 8.8.8.8: Python no la considera global
    "64:ff9b:1::1",
    "100::1",
    "2001::1",
    "2001:db8::1",
    "2002:a00:1::1",
    "fc00::1",
    "fd12:3456:789a::1",
    "fe80::1",
    "febf::1",
    "fec0::1",
    "ff02::1",
    "ff0e::1",
]
ALLOWED = [
    "93.184.216.34",
    "8.8.8.8",
    "1.1.1.1",
    "100.63.255.255",
    "100.128.0.0",
    "172.15.255.255",
    "172.32.0.0",
    "2606:4700:4700::1111",
    "2a00:1450:4001:80b::200e",
]


@pytest.mark.parametrize("address", FORBIDDEN)
def test_forbidden_addresses(address: str) -> None:
    assert is_forbidden_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses(address: str) -> None:
    assert not is_forbidden_address(ipaddress.ip_address(address))


def _target(url: str, policy: NetPolicy | None = None) -> Target:
    return target_of(url, policy or NetPolicy())


async def test_resolve_public_name() -> None:
    ip = await resolve_target(_target(f"https://{SITE_HOST}"), FakeResolver())
    assert ip == ipaddress.ip_address(PUBLIC_IP)


async def test_name_with_one_private_address_is_rejected() -> None:
    resolver = FakeResolver({SITE_HOST: [PUBLIC_IP, "10.0.0.7"]})
    with pytest.raises(FaroError) as info:
        await resolve_target(_target(f"https://{SITE_HOST}"), resolver)
    assert info.value.code == "site.address_not_allowed"


@pytest.mark.parametrize("private", ["127.0.0.1", "169.254.169.254", "::1", "fd00::1"])
async def test_name_resolving_to_private_is_rejected(private: str) -> None:
    resolver = FakeResolver({SITE_HOST: [private]})
    with pytest.raises(FaroError) as info:
        await resolve_target(_target(f"https://{SITE_HOST}"), resolver)
    assert info.value.code == "site.address_not_allowed"


async def test_prefers_ipv4() -> None:
    resolver = FakeResolver({SITE_HOST: ["2606:4700:4700::1111", PUBLIC_IP]})
    ip = await resolve_target(_target(f"https://{SITE_HOST}"), resolver)
    assert ip == ipaddress.ip_address(PUBLIC_IP)
    only_v6 = FakeResolver({SITE_HOST: ["2606:4700:4700::1111"]})
    ip6 = await resolve_target(_target(f"https://{SITE_HOST}"), only_v6)
    assert ip6 == ipaddress.ip_address("2606:4700:4700::1111")


async def test_unknown_or_empty_name_is_unreachable() -> None:
    with pytest.raises(FaroError) as info:
        await resolve_target(_target("https://no-existe.example"), FakeResolver())
    assert info.value.code == "site.unreachable"
    with pytest.raises(FaroError) as info:
        await resolve_target(_target(f"https://{SITE_HOST}"), FakeResolver({SITE_HOST: []}))
    assert info.value.code == "site.unreachable"


async def test_ip_literal_is_not_resolved() -> None:
    resolver = FakeResolver()
    ip = await resolve_target(_target(f"https://{PUBLIC_IP}"), resolver)
    assert ip == ipaddress.ip_address(PUBLIC_IP)
    assert resolver.calls == []


async def test_local_mode_only_accepts_loopback() -> None:
    policy = NetPolicy(allow_local=True, engine_port=50123)
    ok = FakeResolver({"localhost": ["::1", "127.0.0.1"]})
    ip = await resolve_target(_target("http://localhost:8888", policy), ok)
    assert ip == ipaddress.ip_address("127.0.0.1")
    # `localhost` que resuelve a la red local: prohibido también en modo local.
    lan = FakeResolver({"localhost": ["192.168.1.20"]})
    with pytest.raises(FaroError) as info:
        await resolve_target(_target("http://localhost:8888", policy), lan)
    assert info.value.code == "site.address_not_allowed"


async def test_pins_resolve_once_per_target() -> None:
    """IP fijada: una segunda resolución distinta (DNS rebinding) no se usa."""
    resolver = FakeResolver()
    pins = AddressPins(resolver)
    target = _target(f"https://{SITE_HOST}")
    first = await pins.pin(target)
    resolver.mapping[SITE_HOST] = ["127.0.0.1"]
    second = await pins.pin(target)
    assert first == second == ipaddress.ip_address(PUBLIC_IP)
    assert resolver.calls == [(SITE_HOST, 443)]


def test_pin_request_keeps_host_and_sni() -> None:
    request = httpx.Request("GET", f"https://{SITE_HOST}/wp-json/faro/v1?page=2")
    pin_request(request, _target(str(request.url)), ipaddress.ip_address(PUBLIC_IP))
    assert request.url.host == PUBLIC_IP
    assert request.url.path == "/wp-json/faro/v1"
    assert request.url.params["page"] == "2"
    assert request.headers["host"] == SITE_HOST
    assert request.extensions["sni_hostname"] == SITE_HOST


def test_pin_request_ipv6_and_http() -> None:
    policy = NetPolicy(allow_local=True)
    request = httpx.Request("GET", "http://localhost:8888/")
    pin_request(request, _target(str(request.url), policy), ipaddress.ip_address("::1"))
    assert request.url.host == "::1"
    assert request.url.port == 8888
    assert request.headers["host"] == "localhost:8888"
    assert "sni_hostname" not in request.extensions
    literal = httpx.Request("GET", f"https://{PUBLIC_IP}/")
    pin_request(literal, _target(str(literal.url)), ipaddress.ip_address(PUBLIC_IP))
    assert "sni_hostname" not in literal.extensions


async def test_system_resolver_parses_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(_self: Any, host: str, port: int, **_kw: Any) -> list[Any]:
        assert (host, port) == (SITE_HOST, 443)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%12", 443, 0, 12)),
        ]

    import asyncio

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
    addresses = await system_resolver(SITE_HOST, 443)
    assert addresses == [ipaddress.ip_address(PUBLIC_IP), ipaddress.ip_address("fe80::1")]
    # La zona se quita, pero la dirección sigue siendo de enlace local: prohibida.
    with pytest.raises(FaroError):
        await resolve_target(_target(f"https://{SITE_HOST}"), guard.system_resolver)


async def test_system_resolver_failure_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing(_self: Any, *_args: Any, **_kw: Any) -> list[Any]:
        raise socket.gaierror("sin DNS")

    import asyncio

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", failing)
    with pytest.raises(FaroError) as info:
        await resolve_target(_target(f"https://{SITE_HOST}"), system_resolver)
    assert info.value.code == "site.unreachable"
