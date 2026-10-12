"""Estado de los grafos y alcance de una ejecución (skill `agentes-langgraph` §2).

- `RunContext` (definido en `core/jobs/runner.py`, el contrato con el trabajador) es
  inmutable, lo construye el trabajador a partir de `agent_runs` y **no** forma parte del
  estado ni del checkpoint. El alcance (tarea, sitio, proveedor, límites) solo sale de aquí,
  nunca del estado que escribió un nodo ni de lo que devuelve el modelo.
- El estado de un grafo es un modelo Pydantic que hereda de `AgentState` y cuyos campos
  son **solo tipos JSON** (`str`, `int`, `bool`, `None`, `list`, `dict` con claves `str`,
  `Literal` y `Any` dentro de contenedores). `check_state_schema` lo comprueba al registrar
  el agente; el serializador del checkpointer lo vuelve a exigir con cada valor.
- `AgentState` trae los campos que gestiona el marco: el contador de llamadas a
  herramientas por tarea (`tool_calls`) y el resultado de la propuesta (`approval_id`,
  `outcome`, `suggestion`). En el estado nunca van secretos, prompts completos ni
  respuestas crudas del proveedor.
"""

from __future__ import annotations

import types
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from faro_engine.core.errors import AGENT_SITE_REQUIRED, jobs_error
from faro_engine.core.jobs.runner import RunContext

__all__ = [
    "AgentState",
    "NodeKindError",
    "RunContext",
    "StateSchemaError",
    "check_state_schema",
    "require_site",
]

Outcome = Literal["suggested", "approved", "rejected", "executed"]


class StateSchemaError(TypeError):
    """El estado de un agente tiene un campo que no es JSON (el agente no arranca)."""


class NodeKindError(RuntimeError):
    """Un nodo hizo algo que su tipo no permite (condición T8-C2 de la revisión de T8): el
    nodo que propone (`approval`) no llama al modelo ni a herramientas, porque se vuelve a
    ejecutar entero al reanudarse, también al rechazar (ADR 0016, actualización B). Es un
    error de programación del agente: la tarea falla con `internal.unexpected`."""


class AgentState(BaseModel):
    """Base del estado de todo agente (solo tipos JSON)."""

    model_config = ConfigDict(extra="forbid")

    tool_calls: dict[str, int] = {}
    approval_id: str | None = None
    outcome: Outcome | None = None
    suggestion: dict[str, Any] | None = None


_SCALARS = (str, int, bool, type(None))


def _check_annotation(annotation: Any, where: str, *, inside: bool) -> None:
    if annotation is Any:
        if not inside:
            raise StateSchemaError(f"{where}: Any solo dentro de una lista o un mapa")
        return
    if annotation in _SCALARS:
        return
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        if not all(isinstance(arg, str | int) for arg in args):
            raise StateSchemaError(f"{where}: Literal solo de textos o enteros")
        return
    if origin in (Union, types.UnionType):
        for arg in args:
            _check_annotation(arg, where, inside=inside)
        return
    if origin is list:
        _check_annotation(args[0] if args else Any, where, inside=True)
        return
    if origin is dict:
        if not args or args[0] is not str:
            raise StateSchemaError(f"{where}: los mapas solo con claves de texto")
        _check_annotation(args[1], where, inside=True)
        return
    raise StateSchemaError(f"{where}: tipo no JSON")


def check_state_schema(model: type[BaseModel]) -> None:
    """Todos los campos del estado son tipos JSON (ver la cabecera del módulo)."""
    if not issubclass(model, AgentState):
        raise StateSchemaError("el estado de un agente hereda de AgentState")
    if model.model_config.get("extra") != "forbid":
        raise StateSchemaError("el estado de un agente prohíbe campos de más")
    for name, info in model.model_fields.items():
        _check_annotation(info.annotation, f"{model.__name__}.{name}", inside=False)


def require_site(ctx: RunContext) -> str:
    """El sitio de la tarea (del contexto, nunca del modelo) o `agent.site_required`."""
    if ctx.site_id is None:
        raise jobs_error(AGENT_SITE_REQUIRED)
    return ctx.site_id
