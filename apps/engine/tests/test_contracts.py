"""Contratos versionados en `packages/shared` (spec F0 §4.5 y §10.4).

CI comprueba con `git diff --exit-code` que `npm run contracts` no genera diferencias;
esta prueba lo detecta antes y sin git: los archivos versionados siguen al día con la app,
la lista permitida contiene exactamente las 7 operaciones de la spec F1a §5.2 con sus
`timeout_seconds` y `secrets` (§9.6) y cada operación lleva lo que declara su ruta
(ADR 0010 §3, spec F1a §4.5). `agent-grants.json` coincide con el registro de agentes y,
en T3, está vacío (ADR 0014 §1, spec F1b T3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faro_engine.agents.registry import agent_grants_table
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


def _op(
    operation_id: str, method: str, path: str, timeout: int, secrets: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "operationId": operation_id,
        "method": method,
        "path": path,
        "timeout_seconds": timeout,
        "secrets": secrets,
    }


def _wp(ref: str, *access: str) -> list[dict[str, Any]]:
    return [{"ref": ref, "access": list(access)}]


def test_engine_operations_are_exactly_the_f1a_operations() -> None:
    # Concesiones exactas (spec F1a §5.2): cambiar esta tabla requiere revisor-seguridad,
    # igual que `concesiones_exactas_por_operacion` en el núcleo.
    site = "wp/{site_id}/token"
    assert _load("engine-operations.json") == [
        _op("checkSiteConnection", "POST", "/sites/{site_id}/check", 45, _wp(site, "get")),
        _op("connectSite", "POST", "/sites", 60, _wp("wp/{new}/token", "create", "delete")),
        _op("getHealth", "GET", "/health", 10, []),
        _op("listSiteContent", "GET", "/sites/{site_id}/content", 45, _wp(site, "get")),
        _op("listSites", "GET", "/sites", 10, []),
        _op("reconnectSite", "PUT", "/sites/{site_id}/connection", 60, _wp(site, "set")),
        _op("removeSite", "DELETE", "/sites/{site_id}", 45, _wp(site, "get", "delete")),
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


def test_engine_d_ts_declares_the_operations() -> None:
    text = (SHARED / "engine.d.ts").read_text(encoding="utf-8")
    for operation in (
        "getHealth",
        "listSites",
        "connectSite",
        "reconnectSite",
        "checkSiteConnection",
        "listSiteContent",
        "removeSite",
    ):
        assert operation in text
    assert '"/health"' in text
    assert '"/sites/{site_id}/content"' in text


def test_agent_grants_are_exactly_the_t3_table() -> None:
    # Concesiones exactas de los agentes: cambiar esta tabla requiere revisor-seguridad.
    # T9 la cambia a `site_summary` con exactamente sus cuatro referencias (spec F1b §9.5).
    assert _load("agent-grants.json") == []


def test_agent_grants_match_the_registry() -> None:
    assert _load("agent-grants.json") == agent_grants_table()
    text = (SHARED / "agent-grants.json").read_text(encoding="utf-8")
    assert text == json.dumps(agent_grants_table(), indent=2, sort_keys=True) + "\n"
