"""Comprobación MANUAL del HTTPS del motor con el almacén del sistema (F1b T2b).

ADR 0012, actualización 2026-10-06 ("Una CA del sistema se acepta"). La ejecuta el usuario
en su equipo, desde `apps/engine`:

    uv run python scripts/manual_tls_check.py
    uv run python scripts/manual_tls_check.py https://mi-tienda.com/wp-json/

Hace **un** GET con el cliente del motor (`SafeHttpClient`) y su configuración de
producción: el mismo arranque que `python -m faro_engine` (quita `SSLKEYLOGFILE`,
`SSL_CERT_FILE` y `SSL_CERT_DIR`), validación de la URL, guardia SSRF, IP fijada con SNI del
nombre, `trust_env=False` (sin proxies del entorno ni del registro de Windows) y el
contexto TLS único `net/tls.py::tls_context()`.

- Sin claves ni cabeceras propias: solo `User-Agent: Faro/<versión>`.
- Sin seguir redirecciones: un 3xx se muestra tal cual. Sin reintentos.
- Solo `https` y destinos públicos (las mismas reglas que un sitio WordPress).

URL por defecto: `https://pypi.org/simple/truststore/`. Es del dominio `pypi.org`, que el
antivirus del equipo de desarrollo (Norton) intercepta, y pesa unos pocos KB; el índice
completo `https://pypi.org/simple/` pasa del límite de 5 MB del cliente del motor y daría
`site.response_too_large` aunque TLS funcione.

Escribe en stdout una sola línea, sin datos sensibles:

    store=system HTTP 200
    store=system error=site.tls_error

Código de salida: 0 si la respuesta es 2xx o 3xx; 1 en otro caso.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# Mismo arranque que el motor: quita las variables TLS antes de los imports del motor
# (argparse, asyncio y sys no crean contextos TLS).
import faro_engine.__main__  # noqa: F401
from faro_engine import __version__
from faro_engine.core.errors import FaroError
from faro_engine.core.logging import configure_logging
from faro_engine.net.client import Deadline, NetSettings, SafeHttpClient
from faro_engine.net.tls import tls_store
from faro_engine.net.urls import NetPolicy

DEFAULT_URL = "https://pypi.org/simple/truststore/"
DEADLINE_S = 30.0


def production_settings() -> NetSettings:
    """La red de la app: sin sitios locales, resolución del sistema y transporte real."""
    return NetSettings(policy=NetPolicy(), user_agent=f"Faro/{__version__}")


async def fetch_status(url: str, settings: NetSettings) -> int | str:
    """Código HTTP de un GET sin redirecciones ni reintentos, o el código de error."""
    try:
        async with SafeHttpClient(settings, Deadline(DEADLINE_S)) as http:
            response = await http.request("GET", url, retries=0)
    except FaroError as exc:
        return exc.code
    response.wipe()
    return response.status


def main(argv: list[str] | None = None, settings: NetSettings | None = None) -> int:
    parser = argparse.ArgumentParser(description="GET HTTPS con el cliente del motor.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help=f"por defecto {DEFAULT_URL}")
    args = parser.parse_args(argv)
    configure_logging()  # registros JSON a stderr; stdout solo para el resultado
    outcome = asyncio.run(fetch_status(args.url, settings or production_settings()))
    result = f"HTTP {outcome}" if isinstance(outcome, int) else f"error={outcome}"
    sys.stdout.write(f"store={tls_store()} {result}\n")
    sys.stdout.flush()
    return 0 if isinstance(outcome, int) and 200 <= outcome < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
