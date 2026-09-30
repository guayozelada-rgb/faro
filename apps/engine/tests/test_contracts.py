"""Contratos versionados en `packages/shared` (spec F0 §4.5 y §10.4).

CI comprueba con `git diff --exit-code` que `npm run contracts` no genera diferencias;
esta prueba lo detecta antes y sin git: los archivos versionados siguen al día con la app,
la lista permitida contiene exactamente `getHealth` y cada operación lleva los
`timeout_seconds` y `secrets` que declara su ruta (ADR 0010 §3, spec F1a §4.5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faro_engine.core.operations import (
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    SECRET_REF_TEMPLATE_RE,
    SECRETS_KEY,
    TIMEOUT_KEY,
)
from faro_engine.export_openapi import build_openapi

SHARED = Path(__file__).resolve().parents[3] / "packages" / "shared"


def _load(name: str) -> Any:
    return json.loads((SHARED / name).read_text(encoding="utf-8"))


def test_engine_operations_is_exactly_get_health() -> None:
    assert _load("engine-operations.json") == [
        {
            "operationId": "getHealth",
            "method": "GET",
            "path": "/health",
            "timeout_seconds": 10,
            "secrets": [],
        }
    ]


def test_engine_operations_copies_faro_extensions_from_openapi() -> None:
    schema = build_openapi()
    expected = {
        op["operationId"]: {
            "method": method.upper(),
            "path": path,
            "timeout_seconds": op[TIMEOUT_KEY],
            "secrets": op[SECRETS_KEY],
        }
        for path, item in schema["paths"].items()
        for method, op in item.items()
    }
    actual = {
        entry["operationId"]: {k: v for k, v in entry.items() if k != "operationId"}
        for entry in _load("engine-operations.json")
    }
    assert actual == expected


def test_engine_operations_fields_are_valid() -> None:
    operations = _load("engine-operations.json")
    assert [op["operationId"] for op in operations] == sorted(
        op["operationId"] for op in operations
    )
    for op in operations:
        assert set(op) == {"operationId", "method", "path", "timeout_seconds", "secrets"}
        timeout = op["timeout_seconds"]
        assert type(timeout) is int
        assert MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS
        refs = [grant["ref"] for grant in op["secrets"]]
        assert refs == sorted(set(refs))
        for grant in op["secrets"]:
            assert set(grant) == {"ref", "access"}
            assert not grant["ref"].startswith("db/")
            assert SECRET_REF_TEMPLATE_RE.fullmatch(grant["ref"])
            assert grant["access"]


def test_versioned_openapi_matches_the_app() -> None:
    # Comparación de objetos: el orden de claves lo fija generate-contracts.mjs.
    assert _load("openapi.json") == build_openapi()


def test_engine_d_ts_declares_get_health() -> None:
    text = (SHARED / "engine.d.ts").read_text(encoding="utf-8")
    assert "getHealth" in text
    assert '"/health"' in text
