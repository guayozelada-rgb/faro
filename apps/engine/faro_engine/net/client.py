"""Cliente HTTP saliente con protección SSRF, tiempos, tamaño y reintentos (ADR 0012).

- Cada petición valida su URL (`urls.target_of`), resuelve y valida la IP una sola vez por
  operación (`guard.AddressPins`) y se conecta a esa IP con `Host` y SNI del nombre.
- Sin redirecciones automáticas: `request` devuelve el 3xx tal cual; `get_following` (solo
  para el descubrimiento del sitio) sigue hasta 3, validando cada salto.
- TLS siempre verificado, sin proxies del sistema (`trust_env=False`), sin conexiones
  reutilizadas entre peticiones y `User-Agent: Faro/<versión>`.
- Tiempos: conexión 5 s, lectura 15 s, 20 s por petición y el plazo total de la operación
  (`Deadline`, 5 s menos que el timeout del núcleo).
- Respuestas de más de 5 MB → `site.response_too_large`.
- Reintentos solo en GET y DELETE: hasta 2 ante error de red, tiempo agotado, 502, 503 o
  504 (esperas de 1 s y 3 s ±20 %); ante 429, uno si `Retry-After` ≤ 10 s. Cada intento
  vuelve a pedir las cabeceras (`headers`), así una firma lleva nonce y hora nuevos.
- Como máximo 4 peticiones simultáneas en total y 1 por sitio (`ConcurrencyLimits`).

Nunca se registran URLs completas, cabeceras ni cuerpos: solo método, host y código.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import structlog

from faro_engine.core.errors import (
    SITE_BAD_RESPONSE,
    SITE_RESPONSE_TOO_LARGE,
    SITE_TIMEOUT,
    SITE_TLS_ERROR,
    SITE_UNREACHABLE,
    FaroError,
    site_error,
)
from faro_engine.net.guard import AddressPins, Resolver, pin_request, system_resolver
from faro_engine.net.urls import NetPolicy, target_of

log = structlog.get_logger(__name__)

CONNECT_TIMEOUT: Final = 5.0
READ_TIMEOUT: Final = 15.0
REQUEST_TIMEOUT: Final = 20.0
MAX_RESPONSE_BYTES: Final = 5 * 1024 * 1024
MAX_REDIRECTS: Final = 3
MAX_CONCURRENT: Final = 4
RETRY_DELAYS: Final = (1.0, 3.0)
RETRY_JITTER: Final = 0.2
MAX_RETRY_AFTER: Final = 10
RETRY_STATUSES: Final = frozenset({502, 503, 504})
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
IDEMPOTENT_METHODS: Final = frozenset({"GET", "DELETE"})
_RETRYABLE_ERRORS: Final = frozenset({SITE_UNREACHABLE, SITE_TIMEOUT})

HeaderFactory = Callable[[], Mapping[str, str]]


class Deadline:
    """Plazo total de una operación del motor."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return self._end - self._clock()

    def check(self) -> None:
        """`site.timeout` si ya no queda tiempo."""
        if self.remaining() <= 0:
            raise site_error(SITE_TIMEOUT)


class ConcurrencyLimits:
    """Máximo `total` peticiones a la vez y una por host."""

    def __init__(self, total: int = MAX_CONCURRENT) -> None:
        self._total = asyncio.Semaphore(total)
        self._per_host: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        lock = self._per_host.setdefault(host, asyncio.Lock())
        async with lock, self._total:
            yield


def default_transport() -> httpx.AsyncBaseTransport:
    """Transporte real: TLS verificado (certifi), sin reintentos ni conexiones reutilizadas."""
    return httpx.AsyncHTTPTransport(
        verify=True,
        trust_env=False,
        retries=0,
        http2=False,
        limits=httpx.Limits(max_connections=MAX_CONCURRENT, max_keepalive_connections=0),
    )


def default_jitter() -> float:
    return random.uniform(1 - RETRY_JITTER, 1 + RETRY_JITTER)  # noqa: S311 - no es criptografía


@dataclass(slots=True)
class NetSettings:
    """Configuración de la red saliente de la app (una por proceso)."""

    policy: NetPolicy
    user_agent: str
    resolver: Resolver = system_resolver
    transport_factory: Callable[[], httpx.AsyncBaseTransport] = default_transport
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[], float] = default_jitter
    limits: ConcurrencyLimits = field(default_factory=ConcurrencyLimits)


@dataclass(slots=True)
class HttpResponse:
    """Respuesta ya leída entera. `url` es la URL lógica (con el nombre, no la IP)."""

    status: int
    headers: httpx.Headers
    body: bytearray = field(repr=False)
    url: str

    def wipe(self) -> None:
        """Sobrescribe el cuerpo (p. ej. la respuesta de `/pair`, que trae secretos)."""
        self.body[:] = bytes(len(self.body))


def _is_tls_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` en segundos (0 a 10). Sin cabecera → 1 s. Fecha u otro valor → `None`."""
    if value is None:
        return RETRY_DELAYS[0]
    text = value.strip()
    if not text.isdigit() or int(text) > MAX_RETRY_AFTER:
        return None
    return float(int(text))


async def _read_limited(response: httpx.Response) -> bytearray:
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
        raise site_error(SITE_RESPONSE_TOO_LARGE)
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data += chunk
        if len(data) > MAX_RESPONSE_BYTES:
            data[:] = bytes(len(data))
            raise site_error(SITE_RESPONSE_TOO_LARGE)
    return data


class SafeHttpClient:
    """Cliente de una operación: sus IP fijadas duran lo que dura el `async with`."""

    def __init__(self, settings: NetSettings, deadline: Deadline) -> None:
        self._settings = settings
        self._deadline = deadline
        self._pins = AddressPins(settings.resolver)
        self._client = httpx.AsyncClient(
            transport=settings.transport_factory(),
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=CONNECT_TIMEOUT
            ),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )

    @property
    def deadline(self) -> Deadline:
        return self._deadline

    @property
    def policy(self) -> NetPolicy:
        return self._settings.policy

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: HeaderFactory | None = None,
        content: bytes | None = None,
        retries: int = len(RETRY_DELAYS),
    ) -> HttpResponse:
        """Una petición sin seguir redirecciones, con los reintentos de ADR 0012 §8.

        `retries` (0 a 2) es el máximo ante error de red o 5xx; solo GET y DELETE se
        reintentan (POST nunca, p. ej. `/pair`: un código usado no sirve dos veces).
        """
        method = method.upper()
        can_retry = method in IDEMPOTENT_METHODS
        max_failures = min(max(retries, 0), len(RETRY_DELAYS)) if can_retry else 0
        failures = 0
        retried_429 = False
        while True:
            outcome: HttpResponse | FaroError
            try:
                outcome = await self._send_once(method, url, headers, content)
            except FaroError as exc:
                if exc.code not in _RETRYABLE_ERRORS:
                    raise
                outcome = exc
            delay: float | None = None
            failed = isinstance(outcome, FaroError) or outcome.status in RETRY_STATUSES
            if failed and failures < max_failures:
                delay = RETRY_DELAYS[failures] * self._settings.jitter()
                failures += 1
            elif (
                max_failures > 0
                and isinstance(outcome, HttpResponse)
                and outcome.status == 429
                and not retried_429
            ):
                delay = parse_retry_after(outcome.headers.get("retry-after"))
                retried_429 = True
            if delay is None or delay >= self._deadline.remaining():
                if isinstance(outcome, FaroError):
                    raise outcome
                return outcome
            if isinstance(outcome, HttpResponse):
                outcome.wipe()
            log.info("net.retry", method=method, delay_s=round(delay, 2))
            await self._settings.sleep(delay)

    async def get_following(self, url: str, max_redirects: int = MAX_REDIRECTS) -> HttpResponse:
        """GET que sigue hasta `max_redirects` redirecciones, validando cada salto.

        Un salto a una dirección no permitida falla con el código de la guardia; uno más
        del límite, con `site.bad_response`.
        """
        current = url
        redirects = 0
        while True:
            response = await self.request("GET", current)
            location = response.headers.get("location")
            if response.status not in REDIRECT_STATUSES or not location:
                return response
            if redirects == max_redirects:
                log.warning("net.too_many_redirects", max_redirects=max_redirects)
                raise site_error(SITE_BAD_RESPONSE)
            redirects += 1
            parts = urlsplit(urljoin(current, location.strip()))
            current = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
            target_of(current, self._settings.policy)

    async def _send_once(
        self,
        method: str,
        url: str,
        headers: HeaderFactory | None,
        content: bytes | None,
    ) -> HttpResponse:
        target = target_of(url, self._settings.policy)
        self._deadline.check()
        budget = min(REQUEST_TIMEOUT, self._deadline.remaining())
        try:
            async with asyncio.timeout(budget):
                ip = await self._pins.pin(target)
                request = self._client.build_request(
                    method, url, headers=dict(headers()) if headers else None, content=content
                )
                pin_request(request, target, ip)
                async with self._settings.limits.slot(target.host):
                    response = await self._client.send(request, stream=True)
                    try:
                        body = await _read_limited(response)
                    finally:
                        await response.aclose()
        except (TimeoutError, httpx.TimeoutException):
            log.warning("net.request_failed", method=method, host=target.host, code=SITE_TIMEOUT)
            raise site_error(SITE_TIMEOUT) from None
        except httpx.TransportError as exc:
            code = SITE_TLS_ERROR if _is_tls_error(exc) else SITE_UNREACHABLE
            log.warning(
                "net.request_failed",
                method=method,
                host=target.host,
                code=code,
                error_type=type(exc).__name__,
            )
            raise site_error(code) from None
        return HttpResponse(response.status_code, response.headers, body, url)
