"""Bloqueo de red en los procesos hijos de las pruebas (condición T11-C2, revisión de seguridad
de T11, hallazgo 3c).

`tests/conftest.py` pone esta carpeta al principio de `PYTHONPATH` y `FARO_TEST_NETWORK_LOG`
con la ruta de un archivo. Python importa `sitecustomize` al arrancar, así que todo proceso
Python que lancen las pruebas (el motor de las pruebas de protocolo, las sondas, los
`python -c …`) y los que lancen esos procesos instalan el **mismo** gancho de auditoría que
`tests/network_guard.py` antes de ejecutar su código. Cada intento fuera de loopback falla
y queda anotado en ese archivo; el *fixture* automático lo lee al terminar cada prueba y la
hace fallar aunque el hijo se tragara el error. Cada proceso vigilado anota también su
arranque (`start <pid>`), para comprobar que el bloqueo llegó.

`tests/network_guard.py` se carga por su ruta (sin importar el paquete `tests` ni tocar
`sys.path`). Si no se puede instalar, se anota `error` y la prueba falla (falla cerrado).
Sin `FARO_TEST_NETWORK_LOG` no hace nada. Después se ejecuta el `sitecustomize` que esta
carpeta tapa, si lo hay (p. ej. el de la distribución de Linux).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))  # noqa: PTH120 - sin pathlib aquí
_LOG = os.environ.get("FARO_TEST_NETWORK_LOG")


def _install(log: str) -> None:
    path = os.path.join(os.path.dirname(_HERE), "network_guard.py")  # noqa: PTH118, PTH120
    spec = importlib.util.spec_from_file_location("faro_test_network_guard", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.install_in_child(log)


def _chain() -> None:
    """Ejecuta el `sitecustomize` que esta carpeta tapa (si existe), como haría `site`."""
    here = os.path.normcase(_HERE)
    paths = [p for p in sys.path if os.path.normcase(os.path.realpath(p or ".")) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


if _LOG:
    try:
        _install(_LOG)
    except Exception as exc:  # noqa: BLE001 - falla cerrado: la prueba lo verá en el archivo
        with open(_LOG, "a", encoding="utf-8") as handle:  # noqa: PTH123
            handle.write(f"error {os.getpid()} {type(exc).__name__}\n")
_chain()
