"""Reglas de la tabla de concesiones por ejecución (ADR 0014 §1, spec F1b §4.5 y §4.7).

Cada tipo de agente declara qué secretos del llavero puede pedir durante una ejecución
(`run_grant_request`), si necesita un sitio y cuánto vive como mucho su concesión.
`npm run contracts` exporta el registro (`python -m faro_engine.export_agents`) y escribe
`packages/shared/agent-grants.json`, que el núcleo incrusta al compilar y usa para decidir:
la tabla sale de código revisado, nunca de lo que diga el motor en tiempo de ejecución.

Reglas (las comprueban este módulo al arrancar el motor, `scripts/generate-contracts.mjs`
al generar y, desde T5, una prueba del núcleo):

- Solo acceso `get`: un agente nunca crea, reemplaza ni borra secretos.
- Plantillas admitidas: `llm/<anthropic|openai|gemini>/default` (literal) y
  `wp/{site_id}/token`. Nada más: `db/*` y `oauth/*` se rechazan con su motivo.
- `wp/{site_id}/token` exige `requires_site: true` (sin sitio no hay `{site_id}`).
- `max_grant_seconds` entero entre `MIN_GRANT_SECONDS` y `MAX_GRANT_SECONDS`.
- `kind` con forma `AGENT_KIND_RE` y único en el registro.
- Forma cerrada: solo las claves `ENTRY_KEYS` por agente y `ref`/`access` por secreto.

Cada error lleva un `rule` estable. Los vectores de
`packages/shared/fixtures/agent-grants-cases.json` comprueban que el generador y este
módulo aceptan, rechazan (con la misma regla) y ordenan exactamente igual: si cambias una
regla, cámbiala en los dos a la vez y actualiza los vectores. Cualquier cambio de
`agent-grants.json` requiere revisión de `revisor-seguridad`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from faro_engine.core.operations import SecretGrant

MIN_GRANT_SECONDS: Final = 60
MAX_GRANT_SECONDS: Final = 900

AGENT_KIND_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

LLM_PROVIDERS: Final = ("anthropic", "openai", "gemini")
WP_SITE_REF_TEMPLATE: Final = "wp/{site_id}/token"
# Plantillas que un agente puede declarar, en orden canónico (el de `ref`).
AGENT_SECRET_TEMPLATES: Final[tuple[str, ...]] = tuple(
    sorted((*(f"llm/{provider}/default" for provider in LLM_PROVIDERS), WP_SITE_REF_TEMPLATE))
)
# Único acceso posible en una concesión de ejecución.
AGENT_SECRET_ACCESS: Final = ("get",)

ENTRY_KEYS: Final = frozenset({"kind", "requires_site", "max_grant_seconds", "secrets"})
SECRET_KEYS: Final = frozenset({"ref", "access"})


class AgentGrantError(ValueError):
    """Entrada inválida en la tabla de concesiones por ejecución.

    `rule` identifica la regla incumplida (la misma que usa el generador de contratos).
    """

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


@dataclass(frozen=True, slots=True)
class AgentGrantSpec:
    """Lo que el núcleo necesita saber de un tipo de agente para concederle secretos."""

    kind: str
    requires_site: bool
    max_grant_seconds: int
    secrets: tuple[SecretGrant, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "requires_site": self.requires_site,
            "max_grant_seconds": self.max_grant_seconds,
            "secrets": [{"ref": s.ref, "access": list(s.access)} for s in self.secrets],
        }


def _check_keys(value: Mapping[str, object], expected: frozenset[str], where: str) -> None:
    unknown = sorted(set(value) - expected)
    if unknown:
        raise AgentGrantError(
            "unknown_field", f"{_upper_first(where)} tiene campos desconocidos: {unknown}."
        )
    missing = sorted(expected - set(value))
    if missing:
        raise AgentGrantError("missing_field", f"Faltan campos en {where}: {missing}.")


def _check_secret(secret: object, where: str) -> str:
    if not isinstance(secret, dict):
        raise AgentGrantError(
            "shape", f"Cada secreto de {where} debe ser un objeto {{ref, access}}."
        )
    _check_keys(secret, SECRET_KEYS, f"un secreto de {where}")
    ref = secret["ref"]
    if not isinstance(ref, str):
        raise AgentGrantError("template", f"En {where}, `ref` debe ser texto.")
    if ref.startswith("db/"):
        raise AgentGrantError(
            "db", f"En {where}, {ref!r} es la llave de la base: `db/*` nunca se concede."
        )
    if ref.startswith("oauth/"):
        raise AgentGrantError(
            "oauth", f"En {where}, {ref!r}: `oauth/*` no se concede a agentes (ADR 0014 §1)."
        )
    if ref not in AGENT_SECRET_TEMPLATES:
        raise AgentGrantError(
            "template",
            f"En {where}, {ref!r} no es una plantilla admitida para agentes "
            f"({', '.join(AGENT_SECRET_TEMPLATES)}).",
        )
    access = secret["access"]
    if not isinstance(access, list) or access != list(AGENT_SECRET_ACCESS):
        raise AgentGrantError(
            "access",
            f"En {where}, {ref!r} debe declarar exactamente ['get']: un agente nunca crea, "
            "reemplaza ni borra secretos.",
        )
    return ref


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _check_entry(entry: object, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise AgentGrantError("shape", f"La entrada {index} de la tabla debe ser un objeto.")
    kind = entry.get("kind")
    where = f"el agente {kind!r}" if isinstance(kind, str) else f"la entrada {index}"
    _check_keys(entry, ENTRY_KEYS, where)
    if not isinstance(kind, str) or not AGENT_KIND_RE.fullmatch(kind):
        raise AgentGrantError("kind", f"El tipo de {where} debe cumplir {AGENT_KIND_RE.pattern}.")
    requires_site = entry["requires_site"]
    if not isinstance(requires_site, bool):
        raise AgentGrantError("requires_site", f"`requires_site` de {where} debe ser booleano.")
    seconds = entry["max_grant_seconds"]
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or not MIN_GRANT_SECONDS <= seconds <= MAX_GRANT_SECONDS
    ):
        raise AgentGrantError(
            "max_grant_seconds",
            f"`max_grant_seconds` de {where} debe ser un entero entre {MIN_GRANT_SECONDS} y "
            f"{MAX_GRANT_SECONDS}.",
        )
    secrets = entry["secrets"]
    if not isinstance(secrets, list):
        raise AgentGrantError("shape", f"`secrets` de {where} debe ser una lista.")
    refs: list[str] = []
    for secret in secrets:
        ref = _check_secret(secret, where)
        if ref in refs:
            raise AgentGrantError("duplicate_ref", f"En {where}, {ref!r} aparece más de una vez.")
        refs.append(ref)
    if WP_SITE_REF_TEMPLATE in refs and not requires_site:
        raise AgentGrantError(
            "site_token_without_site",
            f"{_upper_first(where)} declara {WP_SITE_REF_TEMPLATE} pero no `requires_site`.",
        )
    return {
        "kind": kind,
        "requires_site": requires_site,
        "max_grant_seconds": seconds,
        "secrets": [{"ref": ref, "access": list(AGENT_SECRET_ACCESS)} for ref in sorted(refs)],
    }


def canonical_agent_grants(data: object) -> list[dict[str, Any]]:
    """Valida la tabla en forma JSON y devuelve su forma canónica (ordenada por `kind`).

    Misma función que `buildAgentGrants` de `scripts/generate-contracts.mjs`.
    """
    if not isinstance(data, list):
        raise AgentGrantError("shape", "La tabla de concesiones de agentes debe ser una lista.")
    entries = [_check_entry(entry, index) for index, entry in enumerate(data)]
    kinds: set[str] = set()
    for entry in entries:
        if entry["kind"] in kinds:
            raise AgentGrantError(
                "duplicate_kind", f"El agente {entry['kind']!r} aparece más de una vez."
            )
        kinds.add(entry["kind"])
    return sorted(entries, key=lambda entry: str(entry["kind"]))


def build_agent_grants(specs: Iterable[AgentGrantSpec]) -> list[dict[str, Any]]:
    """Tabla canónica a partir del registro; falla con `AgentGrantError` si no es válida."""
    return canonical_agent_grants([spec.to_json() for spec in specs])
