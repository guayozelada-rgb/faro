"""Sonda en proceso aparte: la inyección de `install_system_trust_for_libraries()` (ADR 0012).

Uso: `python -m tests.net.injection_probe`. Va en un proceso aparte porque sustituye
`ssl.SSLContext` para todo el proceso y no debe contaminar las demás pruebas.

Comprueba (revisiones 2 y 3 de T2b, condición 13):

1. tras la inyección, `ssl.SSLContext`, `urllib3.util.ssl_.SSLContext`, el contexto
   precargado de `requests` (si la versión lo tiene) y el contexto compartido de aiohttp
   son la subclase con cerrojo, `ssl.create_default_context()` devuelve esa subclase y
   todos llevan `OP_NO_RENEGOTIATION`;
2. sobre un `ssl.create_default_context()` compartido, un segundo hilo conecta (no hay
   dueño) y, con cuatro hilos haciendo handshakes a la vez mientras otro envuelve sin
   parar, ninguna CA desconocida se acepta, todas las válidas sí, y el contexto sigue en
   `CERT_REQUIRED` con `check_hostname`;
3. un handshake desde otro hilo sobre un `SSLObject` creado en este se verifica y rechaza
   una CA que no está en el almacén;
4. `truststore.extract_from_ssl()` restaura `ssl.SSLContext` y el de urllib3.

Todo en memoria (sin sockets ni red). Escribe una sola línea `FARO_PROBE <json>` en stdout.
"""

from __future__ import annotations

import contextlib
import json
import ssl
import sys
import threading
from collections.abc import Callable
from typing import Any

import trustme
import truststore

from faro_engine.core.logging import configure_logging
from faro_engine.net import tls
from tests.net.tls_helpers import SITE_NAME, issue, memory_handshake, server_context

_ROUNDS = 150
_WORKERS = 4


def _error_in_thread(action: Callable[[], object]) -> str | None:
    errors: list[str] = []

    def run() -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - se informa del tipo
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=30)
    return errors[0] if errors else None


def _wrap_bio(context: ssl.SSLContext) -> ssl.SSLObject:
    return context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname=SITE_NAME)


def _state(context: ssl.SSLContext) -> list[object]:
    inner: ssl.SSLContext = context._ctx  # type: ignore[attr-defined]
    return [
        int(context.verify_mode),
        context.check_hostname,
        int(inner.verify_mode),
        inner.check_hostname,
    ]


def _parallel(
    shared: ssl.SSLContext, valid: ssl.SSLContext, unknown: ssl.SSLContext
) -> dict[str, int]:
    """Cuatro hilos con handshakes (CA válida y desconocida) y este envolviendo sin parar."""
    counts = {"errors": 0, "accepted_unknown": 0, "rejected_unknown": 0, "valid": 0}
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(_ROUNDS):
            try:
                good = memory_handshake(shared, valid, SITE_NAME)
                bad = memory_handshake(shared, unknown, SITE_NAME)
            except Exception:  # noqa: BLE001 - se cuenta
                with lock:
                    counts["errors"] += 1
                continue
            with lock:
                counts["valid"] += good is None
                counts["accepted_unknown"] += bad is None
                counts["rejected_unknown"] += bad == "SSLCertVerificationError"

    threads = [threading.Thread(target=worker) for _ in range(_WORKERS)]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        _wrap_bio(shared)  # abre la ventana degradada una y otra vez
    for thread in threads:
        thread.join(timeout=120)
    return counts


def _handshake_from_other_thread(shared: ssl.SSLContext, stranger: ssl.SSLContext) -> str | None:
    """Este hilo crea el `SSLObject`; otro hace el handshake (`rev2_readside`)."""
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = shared.wrap_bio(c_in, c_out, server_hostname=SITE_NAME)
    server = stranger.wrap_bio(s_in, s_out, server_side=True)

    def handshake() -> None:
        for _ in range(20):
            for obj in (client, server):
                with contextlib.suppress(ssl.SSLWantReadError):
                    obj.do_handshake()
            s_in.write(c_out.read())
            c_in.write(s_out.read())
        client.do_handshake()

    return _error_in_thread(handshake)


def main() -> int:
    configure_logging()  # logs a stderr; stdout solo para el informe
    stdlib_context = ssl.SSLContext
    report: dict[str, Any] = {"aiohttp_before": "aiohttp" in sys.modules}
    # Los servidores de prueba, antes de inyectar: con la inyección, `server_context()` daría
    # la subclase de `truststore`, que no sirve como servidor.
    ca = trustme.CA()
    valid = server_context(issue(ca, SITE_NAME))
    unknown = server_context(issue(trustme.CA(), SITE_NAME))
    stranger = server_context(issue(trustme.CA(), "otro.test"))
    report["store"] = tls.install_system_trust_for_libraries()
    locked = tls._locked_class()

    import requests.adapters
    import urllib3.util.ssl_
    from aiohttp import connector

    report["ssl_is_owned"] = ssl.SSLContext is locked
    report["urllib3_is_owned"] = urllib3.util.ssl_.SSLContext is locked
    # `requests` 2.34 ya no precarga un contexto; si una versión lo hace, debe ser protegido.
    preloaded = getattr(requests.adapters, "_preloaded_ssl_context", None)
    report["requests_ok"] = preloaded is None or type(preloaded) is locked
    aiohttp_context = connector._SSL_CONTEXT_VERIFIED
    report["aiohttp_is_owned"] = type(aiohttp_context) is locked

    shared = ssl.create_default_context()
    report["default_is_owned"] = type(shared) is locked and shared.__class__ is locked
    report["dunder_class_is_owned"] = type(shared.__class__(ssl.PROTOCOL_TLS_CLIENT)) is locked
    report["no_renegotiation"] = all(
        bool(ctx.options & ssl.OP_NO_RENEGOTIATION) for ctx in (shared, aiohttp_context)
    )
    report["second_thread"] = _error_in_thread(lambda: _wrap_bio(shared))
    # La CA de prueba solo en este contexto de este proceso.
    shared.load_verify_locations(cadata=ca.cert_pem.bytes().decode("ascii"))
    report["parallel"] = _parallel(shared, valid, unknown)
    report["handshake_other_thread"] = _handshake_from_other_thread(shared, stranger)
    report["state"] = _state(shared)
    report["unknown_ca"] = memory_handshake(shared, unknown, SITE_NAME)
    report["state_after"] = _state(shared)

    truststore.extract_from_ssl()
    report["extract_ssl"] = ssl.SSLContext is stdlib_context
    report["extract_urllib3"] = urllib3.util.ssl_.SSLContext is stdlib_context
    sys.stdout.write("FARO_PROBE " + json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
