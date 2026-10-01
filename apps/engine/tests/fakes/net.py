"""Red de pruebas: resolución DNS falsa y transporte simulado que comprueba la IP fijada.

Nunca se sale a la red. `pinned_transport` verifica en cada petición que el cliente se
conecta a la IP resuelta, con `Host` y SNI del nombre, y devuelve la petición al manejador
(respx o el plugin simulado) con el nombre en la URL para que las rutas se escriban con
dominios legibles.
"""

from __future__ import annotations

import inspect
import ipaddress
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import httpx

from faro_engine.net.client import ConcurrencyLimits, NetSettings
from faro_engine.net.guard import IPAddress
from faro_engine.net.urls import NetPolicy

SITE_HOST = "tienda.example"
SITE_URL = f"https://{SITE_HOST}"
# Dirección pública real (no es de documentación ni privada) solo para la resolución falsa.
PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "93.184.216.35"

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


class FakeResolver:
    """`getaddrinfo` simulado. Cuenta las resoluciones por nombre."""

    def __init__(self, mapping: Mapping[str, Sequence[str]] | None = None) -> None:
        self.mapping: dict[str, list[str]] = {
            SITE_HOST: [PUBLIC_IP],
            **{k: list(v) for k, v in (mapping or {}).items()},
        }
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> list[IPAddress]:
        self.calls.append((host, port))
        if host not in self.mapping:
            raise OSError("nombre desconocido")
        return [ipaddress.ip_address(ip) for ip in self.mapping[host]]


class RecordingSleep:
    """`asyncio.sleep` que no espera: guarda cada pausa."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def pinned_transport(handler: Handler, resolver: FakeResolver) -> httpx.MockTransport:
    async def route(request: httpx.Request) -> httpx.Response:
        name = httpx.URL(f"http://{request.headers['host']}").host
        if name in resolver.mapping:
            assert request.url.host in resolver.mapping[name], "no usa la IP fijada"
            if request.url.scheme == "https":
                assert request.extensions.get("sni_hostname") == name
        else:  # IP escrita en la URL: se conecta a ella misma
            assert request.url.host == name
            assert "sni_hostname" not in request.extensions
        request.url = request.url.copy_with(host=name)
        result = handler(request)
        if inspect.isawaitable(result):
            return await result
        return result

    return httpx.MockTransport(route)


def net_settings(
    handler: Handler,
    *,
    resolver: FakeResolver | None = None,
    policy: NetPolicy | None = None,
    sleep: RecordingSleep | None = None,
) -> NetSettings:
    resolver = resolver or FakeResolver()
    return NetSettings(
        policy=policy or NetPolicy(),
        user_agent="Faro/test",
        resolver=resolver,
        transport_factory=lambda: pinned_transport(handler, resolver),
        sleep=sleep or RecordingSleep(),
        jitter=lambda: 1.0,
        limits=ConcurrencyLimits(),
    )


def json_response(
    status: int, body: Any, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def wp_error(status: int, code: str, **data: Any) -> httpx.Response:
    """Error del plugin como lo devuelve WordPress (`WP_Error`)."""
    return json_response(
        status, {"code": code, "message": "Mensaje del sitio.", "data": {"status": status, **data}}
    )
