"""Guardia SSRF de la red saliente (ADR 0012, reglas 2 y 3).

- `is_forbidden_address`: loopback, privadas (RFC 1918), enlace local, CGNAT, ULA,
  multicast, reservadas, no especificadas, de documentación y cualquier otra que Python no
  considere global; también IPv4 mapeadas en IPv6 y NAT64 hacia una de ellas.
- `resolve_target`: resuelve el nombre y lo rechaza si **alguna** dirección está prohibida
  (`site.address_not_allowed`). En el modo de sitios locales solo admite loopback.
- `AddressPins`: una resolución por destino y por operación; las peticiones siguientes a ese
  destino usan la misma IP (sin segunda resolución: evita el DNS rebinding).
- `pin_request`: la petición se conecta a la IP fijada, con `Host` y SNI del nombre
  original (el certificado se verifica contra el nombre, no contra la IP).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Final

import httpx

from faro_engine.core.errors import SITE_ADDRESS_NOT_ALLOWED, SITE_UNREACHABLE, site_error

if TYPE_CHECKING:
    from faro_engine.net.urls import Target

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[Sequence[IPAddress]]]

_V4: Final = ipaddress.IPv4Network
_V6: Final = ipaddress.IPv6Network

# Lista explícita (no depende de la versión de Python) además de `is_global`.
FORBIDDEN_V4: Final = tuple(
    _V4(net)
    for net in (
        "0.0.0.0/8",  # no especificada / "esta red"
        "10.0.0.0/8",  # privada
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # enlace local (incluye metadatos de nube 169.254.169.254)
        "172.16.0.0/12",  # privada
        "192.0.0.0/24",  # asignaciones del IETF
        "192.0.2.0/24",  # documentación
        "192.88.99.0/24",  # relé 6to4
        "192.168.0.0/16",  # privada
        "198.18.0.0/15",  # pruebas de rendimiento
        "198.51.100.0/24",  # documentación
        "203.0.113.0/24",  # documentación
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reservada (incluye 255.255.255.255)
    )
)
FORBIDDEN_V6: Final = tuple(
    _V6(net)
    for net in (
        "::/128",  # no especificada
        "::1/128",  # loopback
        "::ffff:0:0/96",  # IPv4 mapeada
        "64:ff9b:1::/48",  # traducción local
        "100::/64",  # descarte
        "2001::/23",  # asignaciones del IETF (Teredo, ORCHID, …)
        "2001:db8::/32",  # documentación
        "2002::/16",  # 6to4
        "fc00::/7",  # ULA
        "fe80::/10",  # enlace local
        "fec0::/10",  # sitio local (obsoleta)
        "ff00::/8",  # multicast
    )
)
NAT64: Final = _V6("64:ff9b::/96")


def is_forbidden_address(ip: IPAddress) -> bool:
    """`True` si el motor nunca debe conectarse a esta dirección."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None and is_forbidden_address(mapped):
            return True
        if ip in NAT64 and is_forbidden_address(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)):
            return True
        networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = FORBIDDEN_V6
    else:
        networks = FORBIDDEN_V4
    if any(ip in net for net in networks):
        return True
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _parse_sockaddr_host(host: object) -> IPAddress:
    # IPv6 con zona (`fe80::1%12`): la zona se quita; la dirección sigue siendo de enlace local.
    return ipaddress.ip_address(str(host).split("%", 1)[0])


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    """Resolución del sistema (`getaddrinfo`), sin caché propia."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [_parse_sockaddr_host(info[4][0]) for info in infos]


def _preferred(addresses: Sequence[IPAddress]) -> IPAddress:
    """IPv4 primero (sin "happy eyeballs": se usa una sola dirección fijada)."""
    for ip in addresses:
        if isinstance(ip, ipaddress.IPv4Address):
            return ip
    return addresses[0]


async def resolve_target(target: Target, resolver: Resolver) -> IPAddress:
    """Resuelve y valida el destino; devuelve la IP a la que se conectará."""
    if target.ip is not None:
        addresses: list[IPAddress] = [target.ip]
    else:
        try:
            addresses = list(await resolver(target.host, target.port))
        except (OSError, UnicodeError, ValueError):
            raise site_error(SITE_UNREACHABLE) from None
        if not addresses:
            raise site_error(SITE_UNREACHABLE)
    if target.local:
        # Modo de sitios locales: solo loopback. La red local sigue prohibida.
        if not all(ip.is_loopback for ip in addresses):
            raise site_error(SITE_ADDRESS_NOT_ALLOWED)
    elif any(is_forbidden_address(ip) for ip in addresses):
        raise site_error(SITE_ADDRESS_NOT_ALLOWED)
    return _preferred(addresses)


class AddressPins:
    """IP fijada por destino durante una operación: se resuelve una sola vez."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._pins: dict[tuple[str, int], IPAddress] = {}

    async def pin(self, target: Target) -> IPAddress:
        key = (target.host, target.port)
        ip = self._pins.get(key)
        if ip is None:
            ip = await resolve_target(target, self._resolver)
            self._pins[key] = ip
        return ip


def pin_request(request: httpx.Request, target: Target, ip: IPAddress) -> None:
    """Conecta `request` a `ip` conservando `Host` y SNI del nombre original.

    httpx ya puso `Host` al construir la petición con la URL original; aquí solo cambia el
    destino de la conexión. Con un nombre y `https`, el SNI y la verificación del
    certificado usan el nombre (`sni_hostname`).
    """
    request.url = request.url.copy_with(host=str(ip))
    if target.scheme == "https" and target.ip is None:
        request.extensions = {**request.extensions, "sni_hostname": target.host}
