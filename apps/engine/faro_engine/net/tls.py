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

Uso: solo desde transportes asíncronos en el bucle de eventos del motor. `truststore`
desactiva la verificación del contexto interno durante cada `wrap_bio`/`wrap_socket` y la
verificación con el sistema lee ese mismo estado al terminar el handshake; dentro de un
único hilo las dos cosas no se intercalan, pero un cliente síncrono en otro hilo con este
mismo objeto podría verlo a mitad del cambio.

Registra `net.tls_context_ready` con el almacén (`system` o `certifi`) y la versión de
`truststore`; nunca certificados, huellas ni nombres de raíces.
"""

from __future__ import annotations

import functools
import ssl
import threading
from typing import Final, Literal

import structlog

log = structlog.get_logger(__name__)

MINIMUM_TLS_VERSION: Final = ssl.TLSVersion.TLSv1_2
TlsStore = Literal["system", "certifi"]
# Fallos de `truststore` en ejecución que llevan al respaldo con `certifi` (ADR 0012).
_FALLBACK_ERRORS: Final = (ImportError, OSError, NotImplementedError)

_LOCK: Final = threading.Lock()


def _system_context() -> tuple[ssl.SSLContext, str]:
    import truststore  # noqa: PLC0415 - un fallo aquí lleva al respaldo con certifi

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), truststore.__version__


def _certifi_context() -> ssl.SSLContext:
    import certifi  # noqa: PLC0415 - solo en el respaldo

    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=certifi.where())


def _harden(context: ssl.SSLContext) -> ssl.SSLContext:
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.minimum_version = MINIMUM_TLS_VERSION
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
