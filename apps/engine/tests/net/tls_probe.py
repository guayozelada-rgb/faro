"""Sonda en proceso aparte: `SSL_CERT_FILE`/`SSL_CERT_DIR` no añaden raíces (ADR 0012, T2b).

Uso: `python -m tests.net.tls_probe <modo> <cert.pem> <puerto_control> <puerto_motor>`, con
`SSL_CERT_FILE` y `SSL_CERT_DIR` apuntando a la CA de prueba que firmó `cert.pem`
(certificado de `sitio.test` y su clave, el mismo que usan los dos servidores).

- `cleanup`:
  1. control: un `ssl.create_default_context()` creado **antes** de importar el punto de
     entrada acepta el certificado (en memoria y, por red, el servidor de control): la
     variable habría tenido efecto;
  2. se importa `faro_engine.__main__`, que quita las variables;
  3. el contexto del motor rechaza el certificado en memoria y el cliente del motor, por
     red, recibe `site.tls_error`.
- `no_cleanup` (Windows y macOS): sin importar el punto de entrada, con las variables aún
  en el entorno, el motor también lo rechaza (en memoria y por red).

Las comprobaciones en memoria no pasan por sockets: valen también en un equipo cuyo
antivirus intercepta TLS en loopback. Escribe una sola línea `FARO_PROBE <json>` en stdout.
"""

from __future__ import annotations

import os
import ssl
import sys

# Se crea antes de cualquier import del motor: aquí la variable aún está en el entorno.
_CONTROL_CONTEXT = ssl.create_default_context()

import asyncio  # noqa: E402
import json  # noqa: E402

import httpx  # noqa: E402

_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_NAME = "sitio.test"


def _control_status(port: int) -> int | str:
    transport = httpx.HTTPTransport(verify=_CONTROL_CONTEXT, trust_env=False)
    with httpx.Client(transport=transport, trust_env=False) as http:
        try:
            response = http.get(f"https://127.0.0.1:{port}/", extensions={"sni_hostname": _NAME})
        except httpx.TransportError as exc:
            return type(exc).__name__
        return response.status_code


def main(argv: list[str]) -> int:
    mode, cert_file = argv[0], argv[1]
    control_port, engine_port = int(argv[2]), int(argv[3])
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(cert_file)
    report: dict[str, object] = {"env_before": {v: bool(os.environ.get(v)) for v in _VARS}}
    if mode == "cleanup":
        report["control"] = _control_status(control_port)
        import faro_engine.__main__  # noqa: F401 - quita las variables al importarse

    from faro_engine.core.logging import configure_logging
    from faro_engine.net.tls import tls_context, tls_store
    from tests.net.tls_helpers import fetch_through_engine, memory_handshake

    configure_logging()  # logs a stderr; stdout solo para el informe
    if mode == "cleanup":
        # El contexto de control cargó la CA al crearse, antes de la limpieza.
        report["control_memory"] = memory_handshake(_CONTROL_CONTEXT, server, _NAME)
    report["env_after"] = {v: bool(os.environ.get(v)) for v in _VARS}
    report["engine_memory"] = memory_handshake(tls_context(), server, _NAME)
    report["engine"] = asyncio.run(fetch_through_engine(_NAME, engine_port))
    report["store"] = tls_store()
    sys.stdout.write("FARO_PROBE " + json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
