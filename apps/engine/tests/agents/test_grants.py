"""Reglas de la tabla de concesiones por ejecución (ADR 0014 §1, spec F1b T3).

Los vectores de `packages/shared/fixtures/agent-grants-cases.json` los usa también
`scripts/generate-contracts.test.mjs`: si esta prueba falla, el motor y el generador de
contratos ya no aplican las mismas reglas.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faro_engine.agents import registry
from faro_engine.agents.grants import (
    AGENT_KIND_RE,
    AGENT_SECRET_ACCESS,
    AGENT_SECRET_TEMPLATES,
    MAX_GRANT_SECONDS,
    MIN_GRANT_SECONDS,
    AgentGrantError,
    AgentGrantSpec,
    build_agent_grants,
    canonical_agent_grants,
)
from faro_engine.core.operations import ACCESS_BY_KIND, SECRET_REF_TEMPLATE_RE, SecretGrant

FIXTURES = Path(__file__).resolve().parents[4] / "packages" / "shared" / "fixtures"
CASES: dict[str, Any] = json.loads(
    (FIXTURES / "agent-grants-cases.json").read_text(encoding="utf-8")
)
ALL_RULES = {
    "shape",
    "unknown_field",
    "missing_field",
    "kind",
    "duplicate_kind",
    "requires_site",
    "max_grant_seconds",
    "access",
    "db",
    "oauth",
    "template",
    "duplicate_ref",
    "site_token_without_site",
}


def _get(ref: str) -> SecretGrant:
    return SecretGrant(ref=ref, access=("get",))


def _spec(**overrides: Any) -> AgentGrantSpec:
    values: dict[str, Any] = {
        "kind": "site_summary",
        "requires_site": True,
        "max_grant_seconds": 900,
        "secrets": (_get("wp/{site_id}/token"), _get("llm/anthropic/default")),
    }
    values.update(overrides)
    return AgentGrantSpec(**values)


def test_constants_match_shared_rules() -> None:
    rules = CASES["rules"]
    assert CASES["version"] == 1
    assert rules["min_grant_seconds"] == MIN_GRANT_SECONDS
    assert rules["max_grant_seconds"] == MAX_GRANT_SECONDS
    assert rules["kind_pattern"] == AGENT_KIND_RE.pattern
    assert rules["templates"] == list(AGENT_SECRET_TEMPLATES)
    assert rules["access"] == list(AGENT_SECRET_ACCESS)


def test_vectors_cover_every_rule() -> None:
    assert {case["rule"] for case in CASES["invalid"]} == ALL_RULES


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda c: c["name"])
def test_shared_valid_vectors(case: dict[str, Any]) -> None:
    assert canonical_agent_grants(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda c: c["name"])
def test_shared_invalid_vectors(case: dict[str, Any]) -> None:
    with pytest.raises(AgentGrantError) as excinfo:
        canonical_agent_grants(case["input"])
    assert excinfo.value.rule == case["rule"]


def test_agent_templates_are_valid_get_templates_for_operations() -> None:
    assert AGENT_SECRET_TEMPLATES == (
        "llm/anthropic/default",
        "llm/gemini/default",
        "llm/openai/default",
        "wp/{site_id}/token",
    )
    for ref in AGENT_SECRET_TEMPLATES:
        assert SECRET_REF_TEMPLATE_RE.fullmatch(ref)
        kind = next(k for k in ACCESS_BY_KIND if k[1].fullmatch(ref))
        assert "get" in kind[2]


def test_build_from_specs_is_canonical() -> None:
    table = build_agent_grants(
        [_spec(kind="zeta", requires_site=False, secrets=(_get("llm/openai/default"),)), _spec()]
    )
    assert table == [
        {
            "kind": "site_summary",
            "requires_site": True,
            "max_grant_seconds": 900,
            "secrets": [
                {"ref": "llm/anthropic/default", "access": ["get"]},
                {"ref": "wp/{site_id}/token", "access": ["get"]},
            ],
        },
        {
            "kind": "zeta",
            "requires_site": False,
            "max_grant_seconds": 900,
            "secrets": [{"ref": "llm/openai/default", "access": ["get"]}],
        },
    ]


@pytest.mark.parametrize("access", ["set", "create", "delete"])
def test_spec_with_write_access_fails(access: str) -> None:
    spec = _spec(secrets=(SecretGrant(ref="wp/{site_id}/token", access=(access,)),))  # type: ignore[arg-type]
    with pytest.raises(AgentGrantError, match="nunca crea, reemplaza ni borra") as excinfo:
        build_agent_grants([spec])
    assert excinfo.value.rule == "access"


@pytest.mark.parametrize(
    ("ref", "rule"),
    [
        ("db/{site_id}/key", "db"),
        ("oauth/google/{account}", "oauth"),
        ("wp/{new}/token", "template"),
    ],
)
def test_spec_with_forbidden_ref_fails(ref: str, rule: str) -> None:
    with pytest.raises(AgentGrantError) as excinfo:
        build_agent_grants([_spec(secrets=(_get(ref),))])
    assert excinfo.value.rule == rule


@pytest.mark.parametrize("seconds", [MIN_GRANT_SECONDS - 1, MAX_GRANT_SECONDS + 1])
def test_spec_with_grant_seconds_out_of_range_fails(seconds: int) -> None:
    with pytest.raises(AgentGrantError, match="entre 60 y 900") as excinfo:
        build_agent_grants([_spec(max_grant_seconds=seconds)])
    assert excinfo.value.rule == "max_grant_seconds"


def test_error_messages_name_the_agent() -> None:
    with pytest.raises(AgentGrantError, match="El agente 'site_summary' tiene campos desconocidos"):
        canonical_agent_grants([{**_spec().to_json(), "version": 1}])
    with pytest.raises(AgentGrantError, match="Faltan campos en la entrada 0"):
        canonical_agent_grants([{"requires_site": True, "max_grant_seconds": 900, "secrets": []}])


def test_registry_is_empty_in_t3() -> None:
    # T9 registra `site_summary`; hasta entonces el núcleo no concede nada a ningún agente.
    assert registry.AGENT_GRANTS == ()
    assert registry.agent_grants_table() == []


def test_registry_rejects_invalid_specs() -> None:
    with pytest.raises(AgentGrantError):
        registry.agent_grants_table((_spec(), _spec()))
