"""LangSmith siempre apagado (condiciones 7 y 13 del informe de T2, ADR 0012 condición 13).

`langchain-core` arrastra `langsmith`: con `LANGSMITH_TRACING=true` (o
`LANGCHAIN_TRACING_V2`) en el entorno del usuario, cada ejecución de un grafo enviaría su
estado (títulos del sitio, salidas del modelo) a `api.smith.langchain.com` desde un hilo
propio, por `httpx2`/`httpcore2` (que crean un `truststore.SSLContext` sin el cerrojo del
motor: excepción conocida solo mientras LangSmith siga apagado).

`disable_langsmith()` quita del entorno toda variable `LANGSMITH_*` y `LANGCHAIN_*`
(comparando en mayúsculas) y llama a `langsmith.configure(enabled=False)`, que gana al
entorno. `graph.py` la llama al importarse, así que se aplica **antes de construir
cualquier grafo**; si no se puede aplicar, el módulo no se carga y la tarea que lo
importa falla (`internal.unexpected`): `graph.py` se importa con la primera tarea, no al
arrancar el motor (mejor parado que enviando datos). Pruebas: `tests/agents/test_tracing.py`
y el escenario `faro_graph` de `tests/deps/offline_probe.py` (con LangSmith activado en el
entorno, ningún intento de red ni contexto TLS de `httpx2`/`httpcore2` al construir y
ejecutar un grafo).
"""

from __future__ import annotations

import os
from typing import Final

import langsmith

TRACING_PREFIXES: Final = ("LANGSMITH_", "LANGCHAIN_")


def tracing_variables() -> list[str]:
    return sorted(name for name in os.environ if name.upper().startswith(TRACING_PREFIXES))


def disable_langsmith() -> None:
    for name in tracing_variables():
        del os.environ[name]
    langsmith.configure(enabled=False)
    if tracing_variables():  # pragma: no cover - acabamos de quitarlas
        raise RuntimeError("no se pudo apagar LangSmith")
