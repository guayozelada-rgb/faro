"""Metadatos de cada operación del motor en el OpenAPI (ADR 0010 §3, spec F1a §4.5).

Toda ruta declara, con `openapi_extra=faro_operation(...)`:

- `x-faro-timeout-seconds`: tiempo máximo que el núcleo espera la respuesta (entero entre
  `MIN_TIMEOUT_SECONDS` y `MAX_TIMEOUT_SECONDS`). El motor aplica su plazo total 5 s por
  debajo (ADR 0012).
- `x-faro-secrets`: plantillas de referencias del llavero que la operación puede pedir por
  `secret_request` y con qué acceso. Lista vacía = ningún secreto.

`npm run contracts` copia ambos campos a `packages/shared/engine-operations.json`, que el
núcleo incrusta al compilar: la tabla de concesiones sale de código revisado, no de lo que
diga el motor en tiempo de ejecución. Cualquier cambio de `x-faro-secrets` requiere
revisión de `revisor-seguridad`.

Las mismas reglas se comprueban aquí (al construir la app) y en
`scripts/generate-contracts.mjs` (al generar los contratos).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from fastapi import FastAPI
from fastapi.routing import APIRoute

TIMEOUT_KEY: Final = "x-faro-timeout-seconds"
SECRETS_KEY: Final = "x-faro-secrets"
# Extensiones `x-faro-*` que admite una operación; cualquier otra es un error.
ALLOWED_KEYS: Final = frozenset({TIMEOUT_KEY, SECRETS_KEY})

MIN_TIMEOUT_SECONDS: Final = 10
MAX_TIMEOUT_SECONDS: Final = 300

SecretAccess = Literal["get", "create", "set", "delete"]
# Orden canónico de `access` en el OpenAPI y en engine-operations.json.
ACCESS_ORDER: Final[tuple[SecretAccess, ...]] = get_args(SecretAccess)

NEW_PLACEHOLDER: Final = "{new}"

# Gramática del llavero (skill `llavero-y-cifrado`) en forma de plantilla: el `<uuid>` de
# `wp/<uuid>/token` se escribe siempre como `{parametro_de_ruta}` o `{new}`; el núcleo lo
# sustituye por el parámetro de ruta de la llamada, validado como UUID. `llm/*` y
# `oauth/google/*` se declaran literales. `db/*` nunca.
_PLACEHOLDER = r"\{[a-z][a-z0-9_]{0,31}\}"
SECRET_REF_TEMPLATE_RE: Final = re.compile(
    r"^(?:llm/(?:anthropic|openai|gemini)/[a-z0-9_-]{1,32}"
    rf"|wp/{_PLACEHOLDER}/token"
    r"|oauth/google/[0-9]{1,64})$"
)
_PLACEHOLDER_RE: Final = re.compile(_PLACEHOLDER)
_PATH_PARAM_RE: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")


class OperationMetadataError(ValueError):
    """Metadatos `x-faro-*` inválidos en una operación del motor."""


@dataclass(frozen=True, slots=True)
class SecretGrant:
    """Una referencia (plantilla) del llavero y los accesos que la operación puede pedir."""

    ref: str
    access: tuple[SecretAccess, ...]


def _validate_grant(grant: SecretGrant) -> dict[str, Any]:
    ref = grant.ref
    if ref.startswith("db/"):
        raise OperationMetadataError(
            f"La referencia {ref!r} es de la llave de la base: `db/*` nunca se concede."
        )
    if not SECRET_REF_TEMPLATE_RE.fullmatch(ref):
        raise OperationMetadataError(
            f"La referencia {ref!r} no cumple la gramática del llavero en forma de plantilla."
        )
    if not grant.access:
        raise OperationMetadataError(f"La referencia {ref!r} no declara ningún acceso.")
    if len(set(grant.access)) != len(grant.access):
        raise OperationMetadataError(f"La referencia {ref!r} repite accesos.")
    unknown = [a for a in grant.access if a not in ACCESS_ORDER]
    if unknown:
        raise OperationMetadataError(f"La referencia {ref!r} tiene accesos desconocidos.")
    if NEW_PLACEHOLDER in ref and (
        "create" not in grant.access or not set(grant.access) <= {"create", "delete"}
    ):
        raise OperationMetadataError(
            f"La referencia {ref!r} usa {{new}}: solo admite `create` y, opcionalmente, `delete`."
        )
    access = [a for a in ACCESS_ORDER if a in grant.access]
    return {"ref": ref, "access": access}


def faro_operation(*, timeout_seconds: int, secrets: Sequence[SecretGrant]) -> dict[str, Any]:
    """`openapi_extra` de una ruta con su tiempo máximo y los secretos que puede pedir."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise OperationMetadataError(
            f"timeout_seconds debe ser un entero entre {MIN_TIMEOUT_SECONDS} y "
            f"{MAX_TIMEOUT_SECONDS}."
        )
    grants = [_validate_grant(grant) for grant in secrets]
    refs = [grant["ref"] for grant in grants]
    if len(set(refs)) != len(refs):
        raise OperationMetadataError("Una referencia aparece más de una vez en secrets.")
    grants.sort(key=lambda grant: str(grant["ref"]))
    return {TIMEOUT_KEY: timeout_seconds, SECRETS_KEY: grants}


def _placeholders(refs: Iterable[str]) -> set[str]:
    return {m.group(0)[1:-1] for ref in refs for m in _PLACEHOLDER_RE.finditer(ref)}


def validate_app_operations(app: FastAPI) -> None:
    """Falla si alguna ruta de la API no declara sus metadatos con `faro_operation`.

    Comprueba además que cada `{parametro}` de `x-faro-secrets` (salvo `{new}`) sea un
    parámetro de la ruta. Se llama desde `create_app`: un error aquí impide arrancar.
    """
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        where = f"{sorted(route.methods or ())} {route.path} ({route.operation_id})"
        extra = route.openapi_extra or {}
        declared = {k: v for k, v in extra.items() if k.startswith("x-faro")}
        if set(declared) != ALLOWED_KEYS:
            raise OperationMetadataError(
                f"La operación {where} debe declarar openapi_extra=faro_operation(...) "
                f"(solo {', '.join(sorted(ALLOWED_KEYS))})."
            )
        # Revalida lo declarado (aunque se haya escrito a mano) y exige su forma canónica.
        try:
            grants = [
                SecretGrant(ref=grant["ref"], access=tuple(grant["access"]))
                for grant in declared[SECRETS_KEY]
            ]
            canonical = faro_operation(timeout_seconds=declared[TIMEOUT_KEY], secrets=grants)
        except (KeyError, TypeError) as exc:
            raise OperationMetadataError(
                f"La operación {where} tiene x-faro-secrets con forma inválida."
            ) from exc
        except OperationMetadataError as exc:
            raise OperationMetadataError(f"La operación {where}: {exc}") from exc
        if canonical != declared:
            raise OperationMetadataError(
                f"La operación {where} no usa la forma canónica de faro_operation(...)."
            )
        refs = [str(grant["ref"]) for grant in extra[SECRETS_KEY]]
        path_params = set(_PATH_PARAM_RE.findall(route.path))
        if "new" in path_params:
            raise OperationMetadataError(
                f"La operación {where} usa `new` como parámetro de ruta (está reservado)."
            )
        missing = sorted(_placeholders(refs) - {"new"} - path_params)
        if missing:
            raise OperationMetadataError(
                f"La operación {where} usa en secrets parámetros que no están en la ruta: "
                f"{', '.join(missing)}."
            )
