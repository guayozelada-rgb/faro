"""Build del motor sin red (condición 12 del informe de T2; spec F1b T11).

`bundle_smoke_build.py` lanza PyInstaller con esta carpeta en `PYTHONPATH` y con
`FARO_BUILD_NETWORK_LOG` apuntando a un archivo. Python importa `sitecustomize` al arrancar,
en el proceso de PyInstaller y en los subprocesos aislados con los que PyInstaller importa
los paquetes para analizarlos (LiteLLM, por ejemplo, descargaría el mapa de precios). Aquí
se instala un gancho de auditoría que hace fallar toda conexión o resolución de nombres
fuera de loopback y la anota en ese archivo; el constructor falla si el archivo tiene algún
intento. Cada proceso vigilado anota también su arranque (`start <pid>`), para comprobar que
el bloqueo llegó a los subprocesos.

Sin `FARO_BUILD_NETWORK_LOG` no hace nada. No entra en el motor empaquetado (lo comprueba
la prueba de humo `engine`).
"""

from __future__ import annotations

import ipaddress
import os
import sys

_LOG = os.environ.get("FARO_BUILD_NETWORK_LOG")


def _write(line: str) -> None:
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as handle:  # noqa: PTH123 - sin pathlib aquí
            handle.write(line + "\n")


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").split("%", 1)[0].lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _is_literal(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    try:
        ipaddress.ip_address(host.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not event.startswith("socket."):
        return
    blocked: object = None
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg"} and len(args) >= 2:
        address = args[1]
        host = address[0] if isinstance(address, tuple) and address else None
        if isinstance(host, str | bytes) and not _is_loopback(host):
            blocked = host
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}:
        host = args[0] if args else None
        if host is not None and not _is_loopback(host) and not _is_literal(host):
            blocked = host
    if blocked is not None:
        _write(f"{event} {blocked!r}")
        raise OSError(f"build sin red: {event} {blocked!r}")


if _LOG:
    _write(f"start {os.getpid()}")
    sys.addaudithook(_audit)
