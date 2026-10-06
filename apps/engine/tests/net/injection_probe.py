"""Sonda en proceso aparte: la inyección de `install_system_trust_for_libraries()` (ADR 0012).

Uso: `python -m tests.net.injection_probe`. Va en un proceso aparte porque sustituye
`ssl.SSLContext` para todo el proceso y no debe contaminar las demás pruebas.

Comprueba (revisión 2 de T2b, condición 13):

1. tras la inyección, `ssl.SSLContext`, `urllib3.util.ssl_.SSLContext`, el contexto
   precargado de `requests` (si la versión lo tiene) y el contexto compartido de aiohttp
   son la subclase con dueño de hilo, y `ssl.create_default_context()` devuelve esa subclase;
2. sobre un `ssl.create_default_context()` compartido, un segundo hilo recibe
   `WrongThreadError` (también con cuatro hilos en paralelo contra el dueño) y el contexto
   sigue en `CERT_REQUIRED` con `check_hostname`;
3. un handshake desde otro hilo sobre un `SSLObject` del dueño también falla cerrado, y el
   dueño sigue rechazando una CA que no está en el almacén;
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

_ROUNDS = 200
_INTRUDERS = 4


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


def _parallel_intruders(shared: ssl.SSLContext) -> int:
    failures: list[str] = []
    lock = threading.Lock()

    def intruder() -> None:
        for _ in range(_ROUNDS):
            try:
                _wrap_bio(shared)
            except tls.WrongThreadError as exc:
                with lock:
                    failures.append(type(exc).__name__)

    threads = [threading.Thread(target=intruder) for _ in range(_INTRUDERS)]
    for thread in threads:
        thread.start()
    for _ in range(_ROUNDS):
        _wrap_bio(shared)  # el dueño
    for thread in threads:
        thread.join(timeout=60)
    return len(failures)


def _handshake_from_other_thread(shared: ssl.SSLContext) -> str | None:
    """El dueño crea el `SSLObject`; otro hilo intenta el handshake (`rev2_readside`)."""
    client = _wrap_bio(shared)
    server = server_context(issue(trustme.CA(), "otro.test")).wrap_bio(
        ssl.MemoryBIO(), ssl.MemoryBIO(), server_side=True
    )

    def handshake() -> None:
        for _ in range(20):
            for obj in (client, server):
                with contextlib.suppress(ssl.SSLWantReadError):
                    obj.do_handshake()
        client.do_handshake()

    return _error_in_thread(handshake)


def main() -> int:
    configure_logging()  # logs a stderr; stdout solo para el informe
    stdlib_context = ssl.SSLContext
    report: dict[str, Any] = {"aiohttp_before": "aiohttp" in sys.modules}
    report["store"] = tls.install_system_trust_for_libraries()
    owned = tls._thread_owned_class()

    import requests.adapters
    import urllib3.util.ssl_
    from aiohttp import connector

    report["ssl_is_owned"] = ssl.SSLContext is owned
    report["urllib3_is_owned"] = urllib3.util.ssl_.SSLContext is owned
    # `requests` 2.34 ya no precarga un contexto; si una versión lo hace, debe ser protegido.
    preloaded = getattr(requests.adapters, "_preloaded_ssl_context", None)
    report["requests_ok"] = preloaded is None or type(preloaded) is owned
    report["aiohttp_is_owned"] = type(connector._SSL_CONTEXT_VERIFIED) is owned

    shared = ssl.create_default_context()
    report["default_is_owned"] = type(shared) is owned and shared.__class__ is owned
    report["dunder_class_is_owned"] = type(shared.__class__(ssl.PROTOCOL_TLS_CLIENT)) is owned
    _wrap_bio(shared)  # este hilo es el dueño
    report["second_thread"] = _error_in_thread(lambda: _wrap_bio(shared))
    report["parallel_failures"] = _parallel_intruders(shared)
    report["handshake_other_thread"] = _handshake_from_other_thread(shared)
    report["state"] = _state(shared)
    unknown = server_context(issue(trustme.CA(), SITE_NAME))
    report["owner_unknown_ca"] = memory_handshake(shared, unknown, SITE_NAME)
    report["state_after"] = _state(shared)

    truststore.extract_from_ssl()
    report["extract_ssl"] = ssl.SSLContext is stdlib_context
    report["extract_urllib3"] = urllib3.util.ssl_.SSLContext is stdlib_context
    sys.stdout.write("FARO_PROBE " + json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
