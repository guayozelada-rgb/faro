"""Contexto TLS único del motor (ADR 0012, actualización 2026-10-06; spec F1b T2b).

Todo el HTTPS saliente del motor (sitios WordPress, proveedores de IA y, más adelante, el
rastreador) verifica los certificados con el **almacén del sistema operativo**, mediante
`truststore`, y usa **el mismo objeto**, que devuelve `tls_context()`:

- `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`, construido una sola vez por proceso;
- `verify_mode = CERT_REQUIRED`, `check_hostname = True` y como mínimo TLS 1.2;
- sin raíces añadidas por Faro: nunca `load_verify_locations`, `load_default_certs` ni
  `set_default_verify_paths`. No hay forma (opción, variable ni archivo) de añadir raíces
  ni de desactivar la verificación.

Si `truststore` no se puede importar o construir (`ImportError`, `OSError`,
`NotImplementedError`), se usa una sola vez un contexto con las raíces de `certifi` y las
mismas condiciones. Es más restrictivo (menos raíces); nunca hay respaldo a
`verify=False`, a `SSL_CERT_FILE` ni a raíces del usuario.

Solo este módulo crea contextos TLS de cliente (una prueba lo comprueba). Nadie cambia el
contexto después de construirlo; los transportes que lo comparten usan `http2=False`, porque
httpcore fija ALPN en cada conexión con el valor de HTTP/1.1.

`SSL_CERT_FILE` y `SSL_CERT_DIR` los quita `__main__` antes de cualquier import: en Linux,
`truststore` usa las rutas por defecto de OpenSSL, que leen esas variables.

Un cerrojo por contexto (ADR 0012, actualización 2026-10-06, condición 12). `truststore` pone
el contexto interno en `CERT_NONE` y `check_hostname=False` durante cada `wrap_bio` y
`wrap_socket` y luego restaura lo que había guardado; además, en `do_handshake` vuelve a
leer `verify_mode` y `check_hostname` para decidir si verifica. Si dos hilos se cruzan, uno
puede "restaurar" el estado degradado del otro (y el contexto queda para siempre sin
verificar) o hacer un handshake mientras el otro lo tiene degradado (y se acepta un
certificado sin verificar). Y varios hilos sí lo usan: anyio (`TLSStream.wrap`, que usa
httpx) llama a `wrap_bio` en un hilo de trabajo cuando el contexto no es exactamente
`ssl.SSLContext`, y luego hace `do_handshake` en el hilo del bucle.

Por eso el contexto de `truststore` es un `_LockedContext`, con un `threading.Lock` (no
reentrante) propio que cubre:

- toda la ventana degradada de `wrap_bio` y `wrap_socket`, incluida la restauración;
- cada llamada a `do_handshake` de los `SSLObject`/`SSLSocket` que crea el contexto, que es
  donde `truststore` lee `verify_mode` y `check_hostname`.

Así ningún handshake ve el estado degradado y ninguna restauración se cruza con otra,
venga del hilo que venga: las llamadas se **serializan** y todas verifican. Con BIO no
bloqueante (asyncio, anyio) cada `do_handshake` es corto y el cerrojo solo se retiene
durante esa llamada. Con `wrap_socket` bloqueante (hoy no se usa en el motor) el cerrojo se
retiene durante todo el handshake de red, y ningún otro hilo puede conectar con ese
contexto mientras tanto; el handshake que `wrap_socket` hace dentro (`do_handshake_on_connect`)
no vuelve a tomar el cerrojo, que ya tiene su hilo. `read` y `write` de un `SSLObject` sin
`do_handshake` previo pasan antes por el `do_handshake` protegido. Los objetos TLS guardan
una referencia débil al contexto: si ya no existe, el handshake falla cerrado
(`ssl.SSLError`). Además, todo contexto (también los inyectados) lleva
`OP_NO_RENEGOTIATION`: con TLS 1.2, una renegociación pedida por el servidor haría un
handshake nuevo dentro de OpenSSL con el `CERT_NONE` copiado al crear el objeto y sin la
verificación de `truststore`.

El respaldo con `certifi` es un `ssl.SSLContext` normal y no lleva el cerrojo:
`ssl.SSLContext.wrap_bio`/`wrap_socket` no cambian el contexto (OpenSSL lee su
configuración, que nadie modifica tras construirlo), así que no hay estado que degradar.

`install_system_trust_for_libraries()` es la segunda barrera para bibliotecas que crean
sus propios contextos (LiteLLM, T6): construye antes el contexto del motor y, si el
almacén es el del sistema, hace lo mismo que `truststore.inject_into_ssl()` (0.10.4) pero
con la subclase protegida: sustituye `ssl.SSLContext` y `urllib3.util.ssl_.SSLContext`, y el
contexto precargado de `requests` si existe. Así, los contextos compartidos que esas
bibliotecas creen después (`ssl.create_default_context()`, el de aiohttp, la caché de
LiteLLM) también llevan el cerrojo y serializan sus conexiones en vez de degradarse.
`truststore.extract_from_ssl()` la deshace. Es la única forma permitida de inyectar; T6 la
llama antes de importar LiteLLM.

Registra `net.tls_context_ready` con el almacén (`system` o `certifi`) y la versión de
`truststore`; nunca certificados, huellas ni nombres de raíces.
"""

from __future__ import annotations

import contextlib
import functools
import socket
import ssl
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final, Literal, override

import structlog

if TYPE_CHECKING:
    import truststore

log = structlog.get_logger(__name__)

MINIMUM_TLS_VERSION: Final = ssl.TLSVersion.TLSv1_2
TlsStore = Literal["system", "certifi"]
# Fallos de `truststore` en ejecución que llevan al respaldo con `certifi` (ADR 0012).
_FALLBACK_ERRORS: Final = (ImportError, OSError, NotImplementedError)

_LOCK: Final = threading.Lock()
# Marca de un `SSLObject` cuyo handshake ya pasó por la verificación de `truststore`.
_VERIFIED_ATTR: Final = "_faro_verified"


def _context_hold(context: object) -> Callable[[], AbstractContextManager[None]]:
    """Cerrojo del contexto para los objetos TLS que crea `context`.

    Guarda una referencia débil (el contexto interno no debe mantener vivo al externo): si
    el contexto ya no existe, falla cerrado.
    """
    ref = weakref.ref(context)

    def hold() -> AbstractContextManager[None]:
        owner = ref()
        if owner is None:
            log.error("net.tls_context_gone")
            raise ssl.SSLError("el contexto TLS del motor ya no existe")
        held: AbstractContextManager[None] = owner._faro_hold()  # type: ignore[attr-defined]
        return held

    return hold


def _guard_handshakes(
    inner: ssl.SSLContext, hold: Callable[[], AbstractContextManager[None]]
) -> None:
    """`do_handshake` de los `SSLObject`/`SSLSocket` de `inner` toma antes el cerrojo.

    `truststore` verifica la cadena dentro de `do_handshake` leyendo `verify_mode` y
    `check_hostname` del contexto interno, que cada `wrap_*` degrada mientras tiene el
    cerrojo: con él, el handshake nunca ve ese estado.

    Además, `read`/`write` de un `SSLObject` sin `do_handshake` previo harían el handshake
    dentro de OpenSSL con el `CERT_NONE` copiado al crearlo, sin la verificación de
    `truststore`: aquí pasan antes por el `do_handshake` protegido.
    """
    object_base: type[ssl.SSLObject] = inner.sslobject_class
    socket_base: type[ssl.SSLSocket] = inner.sslsocket_class

    def object_handshake(obj: ssl.SSLObject) -> None:
        with hold():
            object_base.do_handshake(obj)
        setattr(obj, _VERIFIED_ATTR, True)

    def ensure_verified(obj: ssl.SSLObject) -> None:
        if not getattr(obj, _VERIFIED_ATTR, False):
            object_handshake(obj)

    def object_read(obj: ssl.SSLObject, len: int = 1024, buffer: Any = None) -> Any:  # noqa: A002 - firma de `ssl.SSLObject`
        ensure_verified(obj)
        return object_base.read(obj, len, buffer)

    def object_write(obj: ssl.SSLObject, data: Any) -> int:
        ensure_verified(obj)
        return object_base.write(obj, data)

    def socket_handshake(sock: ssl.SSLSocket, block: bool = False) -> None:
        with hold():
            socket_base.do_handshake(sock, block)

    inner.sslobject_class = type(
        "_LockedSSLObject",
        (object_base,),
        {"do_handshake": object_handshake, "read": object_read, "write": object_write},
    )
    inner.sslsocket_class = type(
        "_LockedSSLSocket", (socket_base,), {"do_handshake": socket_handshake}
    )


@functools.cache
def _locked_class() -> type[truststore.SSLContext]:
    """Subclase de `truststore.SSLContext` con un cerrojo por contexto (condición 12).

    Es la que usa el motor y la que inyecta `install_system_trust_for_libraries()`.
    """
    import truststore  # noqa: PLC0415 - solo si `truststore` se pudo importar

    class _LockedContext(truststore.SSLContext):
        def __init__(self, protocol: int | None = None) -> None:
            super().__init__(protocol)  # type: ignore[arg-type]
            self._faro_lock = threading.Lock()
            # Marca del hilo que está dentro de `wrap_socket` (y ya tiene el cerrojo).
            self._faro_local = threading.local()
            # También en los contextos inyectados, que no pasan por `_harden`.
            self.options |= ssl.OP_NO_RENEGOTIATION
            _guard_handshakes(self._ctx, _context_hold(self))

        @property  # type: ignore[misc]
        def __class__(self) -> type:
            # `truststore` devuelve aquí `truststore.SSLContext`: `ctx.__class__(...)`
            # crearía un contexto sin cerrojo.
            return type(self)

        @contextlib.contextmanager
        def _faro_hold(self) -> Iterator[None]:
            """El cerrojo, salvo para el handshake que `wrap_socket` hace dentro.

            Con `do_handshake_on_connect`, el `SSLSocket` hace el handshake dentro de
            `wrap_socket`, en el mismo hilo y con el cerrojo ya tomado: volver a tomarlo
            (no es reentrante) lo bloquearía para siempre.
            """
            if getattr(self._faro_local, "wrapping_socket", False):
                yield
                return
            with self._faro_lock:
                yield

        @override
        def wrap_socket(
            self,
            sock: socket.socket,
            server_side: bool = False,
            do_handshake_on_connect: bool = True,
            suppress_ragged_eofs: bool = True,
            server_hostname: str | None = None,
            session: ssl.SSLSession | None = None,
        ) -> ssl.SSLSocket:
            # Bloqueante: con `do_handshake_on_connect`, el cerrojo se retiene durante todo
            # el handshake de red (hoy el motor no usa `wrap_socket`).
            with self._faro_lock:
                self._faro_local.wrapping_socket = True
                try:
                    return super().wrap_socket(
                        sock,
                        server_side=server_side,
                        do_handshake_on_connect=do_handshake_on_connect,
                        suppress_ragged_eofs=suppress_ragged_eofs,
                        server_hostname=server_hostname,
                        session=session,
                    )
                finally:
                    self._faro_local.wrapping_socket = False

        @override
        def wrap_bio(
            self,
            incoming: ssl.MemoryBIO,
            outgoing: ssl.MemoryBIO,
            server_side: bool = False,
            server_hostname: str | None = None,
            session: ssl.SSLSession | None = None,
        ) -> ssl.SSLObject:
            with self._faro_lock:
                return super().wrap_bio(
                    incoming,
                    outgoing,
                    server_side=server_side,
                    server_hostname=server_hostname,
                    session=session,
                )

    return _LockedContext


def _system_context() -> tuple[ssl.SSLContext, str]:
    import truststore  # noqa: PLC0415 - un fallo aquí lleva al respaldo con certifi

    return _locked_class()(ssl.PROTOCOL_TLS_CLIENT), truststore.__version__


def _certifi_context() -> ssl.SSLContext:
    import certifi  # noqa: PLC0415 - solo en el respaldo

    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=certifi.where())


def _harden(context: ssl.SSLContext) -> ssl.SSLContext:
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = MINIMUM_TLS_VERSION
    # Con TLS 1.2, una renegociación haría un handshake nuevo sin la verificación de
    # `truststore` (con el `CERT_NONE` del objeto) y podría cambiar el certificado.
    context.options |= ssl.OP_NO_RENEGOTIATION
    return context


def _build_tls_context() -> tuple[ssl.SSLContext, TlsStore]:
    """Contexto nuevo con las condiciones de ADR 0012 y el almacén usado.

    Solo para `tls_context()` (y las pruebas, que lo necesitan sin caché). Ningún otro
    módulo de `faro_engine` la llama.
    """
    try:
        context, version = _system_context()
    except _FALLBACK_ERRORS as exc:
        log.warning("net.tls_store_fallback", error_type=type(exc).__name__)
        context = _harden(_certifi_context())
        log.info("net.tls_context_ready", store="certifi", truststore_version=None)
        return context, "certifi"
    context = _harden(context)
    log.info("net.tls_context_ready", store="system", truststore_version=version)
    return context, "system"


@functools.cache
def _shared() -> tuple[ssl.SSLContext, TlsStore]:
    return _build_tls_context()


def tls_context() -> ssl.SSLContext:
    """El contexto TLS de cliente del motor: siempre el mismo objeto."""
    with _LOCK:
        return _shared()[0]


def tls_store() -> TlsStore:
    """Almacén que usa `tls_context()`: `system` (`truststore`) o `certifi` (respaldo)."""
    with _LOCK:
        return _shared()[1]


def _inject_locked_class() -> None:
    """`truststore.inject_into_ssl()` (0.10.4) con la subclase protegida.

    Los mismos puntos: `ssl.SSLContext`, `urllib3.util.ssl_.SSLContext` y, si existe, el
    contexto precargado de `requests.adapters`. `truststore.extract_from_ssl()` restaura
    los dos primeros.
    """
    locked = _locked_class()
    ssl.SSLContext = locked  # type: ignore[misc]
    try:
        urllib3_ssl: Any = import_module("urllib3.util.ssl_")
    except ImportError:
        pass
    else:
        urllib3_ssl.SSLContext = locked
    try:
        adapters: Any = import_module("requests.adapters")
    except ImportError:
        pass
    else:
        if getattr(adapters, "_preloaded_ssl_context", None) is not None:
            adapters._preloaded_ssl_context = locked(ssl.PROTOCOL_TLS_CLIENT)


def install_system_trust_for_libraries() -> TlsStore:
    """Segunda barrera para bibliotecas que crean sus propios contextos (T6, LiteLLM).

    Construye primero el contexto del motor (así el respaldo, si hace falta, se decide
    antes de sustituir `ssl.SSLContext`) y, solo con el almacén del sistema, sustituye
    `ssl.SSLContext` por la subclase con cerrojo (como `truststore.inject_into_ssl()`,
    pero protegida). Con el respaldo no inyecta: esas bibliotecas siguen con sus raíces
    (`certifi`), que es más restrictivo. Hay que llamarla antes de importar la biblioteca:
    un contexto creado antes no cambia. Idempotente.
    """
    store = tls_store()
    if store == "system":
        _inject_locked_class()
    log.info("net.tls_libraries_trust", store=store)
    return store
