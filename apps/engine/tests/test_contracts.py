"""Contratos versionados en `packages/shared` (spec F0 §4.5 y §10.4).

CI comprueba con `git diff --exit-code` que `npm run contracts` no genera diferencias;
esta prueba lo detecta antes y sin git: los archivos versionados siguen al día con la app
y la lista permitida contiene exactamente `getHealth`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faro_engine.export_openapi import build_openapi

SHARED = Path(__file__).resolve().parents[3] / "packages" / "shared"


def _load(name: str) -> Any:
    return json.loads((SHARED / name).read_text(encoding="utf-8"))


def test_engine_operations_is_exactly_get_health() -> None:
    assert _load("engine-operations.json") == [
        {"operationId": "getHealth", "method": "GET", "path": "/health"}
    ]


def test_versioned_openapi_matches_the_app() -> None:
    # Comparación de objetos: el orden de claves lo fija generate-contracts.mjs.
    assert _load("openapi.json") == build_openapi()


def test_engine_d_ts_declares_get_health() -> None:
    text = (SHARED / "engine.d.ts").read_text(encoding="utf-8")
    assert "getHealth" in text
    assert '"/health"' in text
