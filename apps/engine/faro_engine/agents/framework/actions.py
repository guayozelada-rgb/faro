"""Catálogo de acciones con efectos y ejecutor idempotente (ADR 0016 §1, §3 y §5).

Toda acción que un agente puede ejecutar fuera de su propio cálculo se declara aquí con un
`action_kind`, una **clase de efecto** (`internal` | `publish` | `spend`), el esquema de su
`payload`, el nivel de autonomía máximo y la función que la aplica. Las acciones **nunca**
se ofrecen al modelo como herramientas: el modelo propone datos y el motor ejecuta después
de la autonomía (`autonomy.py`) y, si toca, de la aprobación (`approvals.py`).

Guardarraíles que se aplican al registrar (`register_actions`; si fallan, el motor no
arranca) y al ejecutar, cada uno con su prueba:

1. **No existe acción de borrado**: una clase de efecto fuera de la lista (p. ej.
   `delete`) o un `action_kind` con un verbo de borrado (`FORBIDDEN_VERBS`) se rechaza.
3. **Nada que publique o gaste sin autonomía**: el ejecutor es el único camino y solo
   ejecuta una aprobación `approved` sin ejecutar **releída de la base**, de **esta** tarea;
   `publish`/`spend` con nivel máximo > 1 se rechazan en F1b, y su ejecución no está
   habilitada en F1b (spec §2.3: llega en F4/F5).
5. **Pausa global**: con los agentes en pausa el ejecutor no ejecuta nada, tampoco lo ya
   aprobado (`RunStopped("agents_paused")`); queda pendiente hasta reanudar.

Idempotencia (skill `agentes-langgraph` §7): para efectos locales (`internal`), la función
de la acción corre **en la misma transacción** que marca la aprobación `executed`
(`UPDATE … WHERE status = 'approved' AND executed_at IS NULL`): aunque el nodo se repita
tras un cierre brusco, el efecto ocurre una sola vez. Devuelve `previous_value` (para
deshacer, F4). Recibe la `idempotency_key` de la aprobación para los efectos externos de
fases futuras.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import structlog
from pydantic import BaseModel, ValidationError

from faro_engine.agents.framework.state import RunContext
from faro_engine.core.audit import AuditLog
from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database
from faro_engine.core.errors import INTERNAL_UNEXPECTED, FaroError
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.runner import AgentAction, RunStopped
from faro_engine.core.store import approvals as approval_store
from faro_engine.core.store.approvals import ApprovalRecord
from faro_engine.core.store.common import atomic, format_utc
from faro_engine.core.store.run_control import InconsistentStateError

log = structlog.get_logger(__name__)

SideEffect = Literal["internal", "publish", "spend"]
SIDE_EFFECTS: Final[tuple[SideEffect, ...]] = ("internal", "publish", "spend")
# Nivel de autonomía máximo por clase de efecto en F1b (ADR 0016 §2 y actualización C).
MAX_LEVEL_BY_EFFECT: Final[Mapping[str, int]] = {"internal": 3, "publish": 1, "spend": 1}
# Clases de efecto que el ejecutor puede aplicar en F1b (spec §2.3: `publish`/`spend`, F4/F5).
EXECUTABLE_EFFECTS: Final = frozenset({"internal"})
ACTION_KIND_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{1,47}\.[a-z][a-z0-9_]{1,47}")
FORBIDDEN_VERBS: Final = frozenset(
    {
        "delete",
        "remove",
        "destroy",
        "purge",
        "erase",
        "drop",
        "wipe",
        "trash",
        "unpublish",
        "borrar",
        "eliminar",
        "quitar",
        "suprimir",
    }
)


class ActionRegistryError(ValueError):
    """Una acción no cumple los guardarraíles (el motor no arranca)."""


@dataclass(frozen=True, slots=True)
class ActionInput:
    """Lo que recibe la función de una acción: el alcance (del contexto), el `payload` ya
    validado y la clave de idempotencia de su aprobación."""

    ctx: RunContext
    payload: BaseModel
    approval_id: str
    idempotency_key: str
    now: str


# Aplica la acción dentro de la transacción del ejecutor; devuelve `previous_value` (JSON).
ActionRun = Callable[[Connection, ActionInput], str | None]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    action_kind: str
    side_effect: SideEffect
    payload_schema: type[BaseModel]
    max_level: int
    run: ActionRun

    @property
    def info(self) -> AgentAction:
        return AgentAction(self.action_kind, self.side_effect, self.max_level)


def check_action(spec: ActionSpec) -> None:
    """Guardarraíles de registro (ver la cabecera del módulo)."""
    where = f"acción {spec.action_kind!r}"
    if ACTION_KIND_PATTERN.fullmatch(spec.action_kind) is None:
        raise ActionRegistryError(f"{where}: nombre inválido")
    if spec.side_effect not in SIDE_EFFECTS:
        raise ActionRegistryError(f"{where}: clase de efecto no permitida (no existe borrar)")
    words = set(re.split(r"[._]", spec.action_kind))
    if words & FORBIDDEN_VERBS:
        raise ActionRegistryError(f"{where}: ninguna acción puede borrar")
    if not 0 <= spec.max_level <= MAX_LEVEL_BY_EFFECT[spec.side_effect]:
        raise ActionRegistryError(f"{where}: nivel máximo no permitido para su clase de efecto")
    if spec.payload_schema.model_config.get("extra") != "forbid":
        raise ActionRegistryError(f"{where}: el payload debe prohibir campos de más")


class ActionCatalog:
    """Acciones registradas por `action_kind`."""

    def __init__(self, specs: Iterable[ActionSpec] = ()) -> None:
        self._specs: dict[str, ActionSpec] = {}
        for spec in specs:
            check_action(spec)
            if spec.action_kind in self._specs:
                raise ActionRegistryError(f"acción {spec.action_kind!r} repetida")
            self._specs[spec.action_kind] = spec

    def get(self, action_kind: str) -> ActionSpec | None:
        return self._specs.get(action_kind)

    def require(self, action_kind: str) -> ActionSpec:
        spec = self._specs.get(action_kind)
        if spec is None:
            raise KeyError("acción desconocida")
        return spec

    def all(self) -> list[ActionSpec]:
        return [self._specs[kind] for kind in sorted(self._specs)]


def register_actions(*specs: ActionSpec) -> ActionCatalog:
    """Registra acciones al arrancar; lanza `ActionRegistryError` si alguna no cumple."""
    return ActionCatalog(specs)


def validate_payload(spec: ActionSpec, payload: Mapping[str, Any] | BaseModel) -> BaseModel:
    """`payload` validado por el esquema de la acción (Pydantic, `extra="forbid"`)."""
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    return spec.payload_schema.model_validate(data)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ActionExecutor:
    """Único camino para aplicar una acción (guardarraíl 3)."""

    def __init__(
        self,
        *,
        database: Database,
        control: AgentsControlState,
        catalog: ActionCatalog,
        audit: AuditLog | None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._database = database
        self._control = control
        self._catalog = catalog
        self._audit = audit
        self._clock = clock

    async def _fail(self, approval: ApprovalRecord, code: str) -> None:
        now = format_utc(self._clock())
        await self._database.run(
            lambda c: approval_store.mark_approval_failed(c, approval.id, error_code=code, now=now)
        )
        log.warning(
            "agents.action_failed",
            run_id=approval.run_id,
            approval_id=approval.id,
            action_kind=approval.action_kind,
            error_code=code,
        )

    async def execute(self, ctx: RunContext, approval_id: str) -> bool:
        """Ejecuta la aprobación si está `approved`, sin ejecutar y es de esta tarea.

        `False` si no hay nada que ejecutar (ya ejecutada, no aprobada, de otra tarea).
        """
        approval = await self._database.run(
            lambda c: approval_store.get_approval_for_execution(c, approval_id)
        )
        if approval is None or approval.run_id != ctx.run_id:
            return False
        if not self._control.can_run:
            raise RunStopped("agents_paused")  # guardarraíl 5: ni lo ya aprobado
        spec = self._catalog.get(approval.action_kind)
        if spec is None or spec.side_effect != approval.side_effect:
            await self._fail(approval, INTERNAL_UNEXPECTED)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500)
        if spec.side_effect not in EXECUTABLE_EFFECTS:
            log.error("agents.side_effect_not_enabled", action_kind=spec.action_kind)
            await self._fail(approval, INTERNAL_UNEXPECTED)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500)
        try:
            payload = spec.payload_schema.model_validate_json(approval.payload)
        except ValidationError:
            await self._fail(approval, INTERNAL_UNEXPECTED)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500) from None
        now = format_utc(self._clock())
        action_input = ActionInput(
            ctx=ctx,
            payload=payload,
            approval_id=approval.id,
            idempotency_key=approval.idempotency_key,
            now=now,
        )

        def write(conn: Connection) -> bool:
            with atomic(conn):
                # Se vuelve a comprobar dentro de la transacción: una sola ejecución.
                if approval_store.get_approval_for_execution(conn, approval.id) is None:
                    return False
                previous = spec.run(conn, action_input)
                if not approval_store.mark_approval_executed(
                    conn, approval.id, now=now, previous_value=previous
                ):  # pragma: no cover - comprobado en la misma transacción
                    raise InconsistentStateError("approval")
                return True

        try:
            executed = await self._database.run(write)
        except FaroError as err:
            await self._fail(approval, err.code)
            raise
        except Exception as exc:  # noqa: BLE001 - sin contenido: solo la clase
            log.error("agents.action_crashed", error_type=type(exc).__name__)
            await self._fail(approval, INTERNAL_UNEXPECTED)
            raise FaroError.of(INTERNAL_UNEXPECTED, 500) from None
        if executed:
            log.info(
                "agents.action_executed",
                run_id=ctx.run_id,
                approval_id=approval.id,
                action_kind=approval.action_kind,
            )
            if self._audit is not None:
                await self._audit.record(
                    action="approval.executed",
                    result="ok",
                    actor="agent",
                    details={"approval_id": approval.id, "agent_kind": approval.agent_kind},
                )
        return executed
