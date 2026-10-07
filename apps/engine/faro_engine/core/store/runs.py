"""Tablas `agent_runs` y `agent_steps` (spec F1b §4.3 y §6).

Estados de una tarea: `queued` → `running` → `waiting_approval` | `paused` | `succeeded` |
`failed` | `cancelled`; `waiting_approval` → `queued` (decisión) | `cancelled` (caducada);
`paused` → `queued` (reanudar). Además `queued` → `cancelled` (cancelar) | `failed` (tope
diario en una tarea del usuario) | `running` y `running` → `queued` (la concesión respondió
`agents.paused`). Cada transición es un `UPDATE … WHERE status = ?` (`transition_run`).

`result` es el JSON validado del esquema del agente; nunca secretos ni respuestas crudas.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from faro_engine.core.db.connection import Connection
from faro_engine.core.store.common import (
    DEFAULT_PAGE_SIZE,
    Page,
    atomic,
    check_limit,
    check_llm_secret_ref,
    check_non_negative,
    check_provider,
)

RUN_STATUSES: Final = frozenset(
    {"queued", "running", "waiting_approval", "paused", "succeeded", "failed", "cancelled"},
)
TERMINAL_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled"})
# Como mucho una tarea en estos estados por (agente, sitio) (`agent.already_queued`).
ACTIVE_STATUSES: Final = ("queued", "running", "paused")
TRIGGERS: Final = frozenset({"user", "schedule", "catch_up"})
RUN_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = {
    "queued": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset(
        {"queued", "waiting_approval", "paused", "succeeded", "failed", "cancelled"},
    ),
    "waiting_approval": frozenset({"queued", "cancelled"}),
    "paused": frozenset({"queued", "cancelled"}),
}
# Prioridad: usuario (0) > reanudada (1) > programada o `catch_up` (2).
PRIORITY_USER: Final = 0
PRIORITY_RESUMED: Final = 1
PRIORITY_SCHEDULED: Final = 2

STEP_KINDS: Final = frozenset({"llm_call", "tool_call", "approval", "control"})
STEP_FINAL_STATUSES: Final = frozenset({"succeeded", "failed", "skipped", "cancelled"})
AUTONOMY_DECISIONS: Final = frozenset({"suggest", "propose", "execute"})
TIERS: Final = frozenset({"economy", "premium"})

# Columnas de `agent_runs` que se pueden cambiar fuera de una transición (lista cerrada:
# se interpolan en el SQL como nombres, nunca como valores).
RUN_UPDATABLE_COLUMNS: Final = frozenset(
    {"current_step", "provider", "result", "status_reason", "estimated_cost_micros"},
)

_RUN_COLUMNS: Final = (
    "id",
    "parent_run_id",
    "agent_kind",
    "agent_version",
    "objective",
    "site_id",
    "trigger",
    "schedule_id",
    "status",
    "status_reason",
    "error_code",
    "priority",
    "provider",
    "current_step",
    "token_budget",
    "tokens_in",
    "tokens_out",
    "cost_micros",
    "estimated_cost_micros",
    "max_cost_micros",
    "currency",
    "result",
    "activity_seq",
    "notice_ack_at",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
)
_SELECT_RUN: Final = f"SELECT {', '.join(_RUN_COLUMNS)} FROM agent_runs"  # noqa: S608

_STEP_COLUMNS: Final = (
    "id",
    "run_id",
    "seq",
    "node",
    "kind",
    "status",
    "idempotency_key",
    "autonomy_decision",
    "provider",
    "model",
    "tier",
    "secret_ref",
    "prompt_id",
    "prompt_version",
    "attempts",
    "tokens_in",
    "tokens_out",
    "cost_micros",
    "cost_estimated",
    "currency",
    "error_code",
    "started_at",
    "finished_at",
)
_SELECT_STEP: Final = f"SELECT {', '.join(_STEP_COLUMNS)} FROM agent_steps"  # noqa: S608


@dataclass(frozen=True, slots=True)
class NewRun:
    id: str
    agent_kind: str
    agent_version: int
    objective: str
    trigger: str
    token_budget: int
    max_cost_micros: int
    created_at: str
    site_id: str | None = None
    parent_run_id: str | None = None
    schedule_id: str | None = None
    priority: int = PRIORITY_SCHEDULED
    provider: str | None = None
    estimated_cost_micros: int | None = None
    status_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    parent_run_id: str | None
    agent_kind: str
    agent_version: int
    objective: str
    site_id: str | None
    trigger: str
    schedule_id: str | None
    status: str
    status_reason: str | None
    error_code: str | None
    priority: int
    provider: str | None
    current_step: str | None
    token_budget: int
    tokens_in: int
    tokens_out: int
    cost_micros: int
    estimated_cost_micros: int | None
    max_cost_micros: int
    currency: str
    result: str | None
    activity_seq: int
    notice_ack_at: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def notice_pending(self) -> bool:
        """Tarea recuperada al abrir que el usuario aún no vio (**Entendido**)."""
        return self.trigger == "catch_up" and self.notice_ack_at is None


@dataclass(frozen=True, slots=True)
class NewStep:
    id: str
    run_id: str
    node: str
    kind: str
    idempotency_key: str
    started_at: str
    autonomy_decision: str | None = None
    provider: str | None = None
    model: str | None = None
    tier: str | None = None
    secret_ref: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None


@dataclass(frozen=True, slots=True)
class StepRecord:
    id: str
    run_id: str
    seq: int
    node: str
    kind: str
    status: str
    idempotency_key: str
    autonomy_decision: str | None
    provider: str | None
    model: str | None
    tier: str | None
    secret_ref: str | None
    prompt_id: str | None
    prompt_version: int | None
    attempts: int
    tokens_in: int
    tokens_out: int
    cost_micros: int
    cost_estimated: int
    currency: str
    error_code: str | None
    started_at: str
    finished_at: str | None


def _run(row: Sequence[Any] | None) -> RunRecord | None:
    return None if row is None else RunRecord(*row)


# --- Tareas --------------------------------------------------------------------------


def has_active_run(conn: Connection, agent_kind: str, site_id: str | None) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_runs WHERE agent_kind = ? AND site_id IS ? "
        "AND status IN (?, ?, ?) LIMIT 1",
        (agent_kind, site_id, *ACTIVE_STATUSES),
    ).fetchone()
    return row is not None


def insert_run(conn: Connection, run: NewRun, *, deduplicate: bool = True) -> bool:
    """Encola una tarea (`queued`). `False` si ya hay una activa del mismo agente y sitio."""
    if run.trigger not in TRIGGERS:
        raise ValueError("trigger desconocido")
    if run.provider is not None:
        check_provider(run.provider)
    with atomic(conn):
        if deduplicate and has_active_run(conn, run.agent_kind, run.site_id):
            return False
        conn.execute(
            "INSERT INTO agent_runs (id, parent_run_id, agent_kind, agent_version, objective, "
            "site_id, trigger, schedule_id, status, status_reason, priority, provider, "
            "token_budget, estimated_cost_micros, max_cost_micros, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.parent_run_id,
                run.agent_kind,
                run.agent_version,
                run.objective,
                run.site_id,
                run.trigger,
                run.schedule_id,
                run.status_reason,
                run.priority,
                run.provider,
                run.token_budget,
                run.estimated_cost_micros,
                run.max_cost_micros,
                run.created_at,
                run.created_at,
            ),
        )
    return True


def get_run(conn: Connection, run_id: str) -> RunRecord | None:
    return _run(conn.execute(_SELECT_RUN + " WHERE id = ?", (run_id,)).fetchone())


def list_runs(
    conn: Connection,
    *,
    status: str | None = None,
    site_id: str | None = None,
    notice_pending: bool | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> Page[RunRecord]:
    """Más recientes primero (`created_at`, `id`); `cursor` = `id` del último de la página."""
    check_limit(limit)
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if site_id is not None:
        clauses.append("site_id = ?")
        params.append(site_id)
    if notice_pending is True:
        clauses.append("trigger = 'catch_up' AND notice_ack_at IS NULL")
    elif notice_pending is False:
        clauses.append("NOT (trigger = 'catch_up' AND notice_ack_at IS NULL)")
    if cursor is not None:
        clauses.append("(created_at, id) < (SELECT created_at, id FROM agent_runs WHERE id = ?)")
        params.append(cursor)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        _SELECT_RUN + where + " ORDER BY created_at DESC, id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    items = [RunRecord(*row) for row in rows[:limit]]
    next_cursor = items[-1].id if len(rows) > limit else None
    return Page(items, next_cursor)


def list_runs_by_status(
    conn: Connection, status: str, *, status_reason: str | None = None
) -> list[RunRecord]:
    """Para la recuperación y la reanudación (p. ej. `paused` por `agents_paused`)."""
    if status_reason is None:
        rows = conn.execute(
            _SELECT_RUN + " WHERE status = ? ORDER BY created_at, id", (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            _SELECT_RUN + " WHERE status = ? AND status_reason = ? ORDER BY created_at, id",
            (status, status_reason),
        ).fetchall()
    return [RunRecord(*row) for row in rows]


def next_queued_run(conn: Connection, *, skip_reason: str | None = None) -> RunRecord | None:
    """La siguiente tarea `queued` por prioridad y antigüedad (opcionalmente sin las que
    esperan por un motivo, p. ej. `daily_limit`)."""
    order = " ORDER BY priority, created_at, id LIMIT 1"
    if skip_reason is None:
        row = conn.execute(_SELECT_RUN + " WHERE status = 'queued'" + order).fetchone()
    else:
        row = conn.execute(
            _SELECT_RUN + " WHERE status = 'queued' AND status_reason IS NOT ?" + order,
            (skip_reason,),
        ).fetchone()
    return _run(row)


def transition_run(
    conn: Connection,
    run_id: str,
    *,
    from_status: str,
    to_status: str,
    now: str,
    status_reason: str | None = None,
    error_code: str | None = None,
    priority: int | None = None,
) -> bool:
    """`UPDATE … WHERE status = from_status`. `False` si la tarea ya no estaba en ese estado.

    Reemplaza `status_reason` y `error_code`; fija `started_at` la primera vez que pasa a
    `running` y `finished_at` al llegar a un estado final.
    """
    if to_status not in RUN_TRANSITIONS.get(from_status, frozenset()):
        raise ValueError(f"transición no permitida: {from_status} → {to_status}")
    started = now if to_status == "running" else None
    finished = now if to_status in TERMINAL_STATUSES else None
    cursor = conn.execute(
        "UPDATE agent_runs SET status = ?, status_reason = ?, error_code = ?, "
        "priority = COALESCE(?, priority), started_at = COALESCE(started_at, ?), "
        "finished_at = COALESCE(?, finished_at), updated_at = ? "
        "WHERE id = ? AND status = ?",
        (
            to_status,
            status_reason,
            error_code,
            priority,
            started,
            finished,
            now,
            run_id,
            from_status,
        ),
    )
    return int(cursor.rowcount) == 1


def update_run(conn: Connection, run_id: str, fields: Mapping[str, object], *, now: str) -> bool:
    """Cambia columnas de la lista cerrada (`result` ya es JSON validado)."""
    unknown = set(fields) - RUN_UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"columnas no actualizables: {sorted(unknown)}")
    provider = fields.get("provider")
    if isinstance(provider, str):
        check_provider(provider)
    assignments = "".join(f"{column} = ?, " for column in fields)
    cursor = conn.execute(
        f"UPDATE agent_runs SET {assignments}updated_at = ? WHERE id = ?",  # noqa: S608
        (*fields.values(), now, run_id),
    )
    return int(cursor.rowcount) == 1


def add_run_usage(
    conn: Connection,
    run_id: str,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_micros: int,
    now: str,
) -> bool:
    """Suma tokens y costo a los acumulados de la tarea."""
    check_non_negative(tokens_in=tokens_in, tokens_out=tokens_out, cost_micros=cost_micros)
    cursor = conn.execute(
        "UPDATE agent_runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
        "cost_micros = cost_micros + ?, updated_at = ? WHERE id = ?",
        (tokens_in, tokens_out, cost_micros, now, run_id),
    )
    return int(cursor.rowcount) == 1


def next_activity_seq(conn: Connection, run_id: str) -> int | None:
    """Incrementa y devuelve el `seq` del evento de actividad de la tarea."""
    row = conn.execute(
        "UPDATE agent_runs SET activity_seq = activity_seq + 1 WHERE id = ? RETURNING activity_seq",
        (run_id,),
    ).fetchone()
    return None if row is None else int(row[0])


def acknowledge_notices(conn: Connection, run_ids: Sequence[str], *, now: str) -> int:
    """**Entendido** del aviso de tareas recuperadas. Devuelve cuántas se marcaron."""
    if not run_ids:
        return 0
    marks = ", ".join("?" for _ in run_ids)
    cursor = conn.execute(
        f"UPDATE agent_runs SET notice_ack_at = ?, updated_at = ? "  # noqa: S608 - solo `?`
        f"WHERE id IN ({marks}) AND trigger = 'catch_up' AND notice_ack_at IS NULL",
        (now, now, *run_ids),
    )
    return int(cursor.rowcount)


# --- Pasos ---------------------------------------------------------------------------


def insert_step(conn: Connection, step: NewStep) -> int:
    """Crea el paso `running` con el siguiente `seq` de la tarea y lo devuelve."""
    if step.kind not in STEP_KINDS:
        raise ValueError("tipo de paso desconocido")
    if step.provider is not None:
        check_provider(step.provider)
    if step.secret_ref is not None:
        check_llm_secret_ref(step.secret_ref, step.provider)
    row = conn.execute(
        "INSERT INTO agent_steps (id, run_id, seq, node, kind, status, idempotency_key, "
        "autonomy_decision, provider, model, tier, secret_ref, prompt_id, prompt_version, "
        "started_at) SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, 'running', ?, ?, ?, ?, ?, "
        "?, ?, ?, ? FROM agent_steps WHERE run_id = ? RETURNING seq",
        (
            step.id,
            step.run_id,
            step.node,
            step.kind,
            step.idempotency_key,
            step.autonomy_decision,
            step.provider,
            step.model,
            step.tier,
            step.secret_ref,
            step.prompt_id,
            step.prompt_version,
            step.started_at,
            step.run_id,
        ),
    ).fetchone()
    return int(row[0])


def get_step(conn: Connection, step_id: str) -> StepRecord | None:
    row = conn.execute(_SELECT_STEP + " WHERE id = ?", (step_id,)).fetchone()
    return None if row is None else StepRecord(*row)


def list_steps(conn: Connection, run_id: str) -> list[StepRecord]:
    rows = conn.execute(_SELECT_STEP + " WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
    return [StepRecord(*row) for row in rows]


def set_step_decision(conn: Connection, step_id: str, decision: str) -> bool:
    """Decisión de autonomía del paso (`suggest`, `propose` o `execute`)."""
    if decision not in AUTONOMY_DECISIONS:
        raise ValueError("decisión de autonomía desconocida")
    cursor = conn.execute(
        "UPDATE agent_steps SET autonomy_decision = ? WHERE id = ? AND status = 'running'",
        (decision, step_id),
    )
    return int(cursor.rowcount) == 1


def add_step_usage(
    conn: Connection,
    step_id: str,
    *,
    tokens_in: int,
    tokens_out: int,
    cost_micros: int,
    attempts: int = 1,
    cost_estimated: bool = False,
    provider: str | None = None,
    model: str | None = None,
    tier: str | None = None,
    secret_ref: str | None = None,
    prompt_id: str | None = None,
    prompt_version: int | None = None,
) -> bool:
    """Suma una llamada al LLM al paso. Los metadatos `None` no pisan los anteriores; una
    llamada con costo estimado (`cost_estimated`) deja el paso marcado."""
    check_non_negative(
        tokens_in=tokens_in, tokens_out=tokens_out, cost_micros=cost_micros, attempts=attempts
    )
    if provider is not None:
        check_provider(provider)
    if tier is not None and tier not in TIERS:
        raise ValueError("nivel de modelo desconocido")
    if secret_ref is not None:
        check_llm_secret_ref(secret_ref, provider)
    cursor = conn.execute(
        "UPDATE agent_steps SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
        "cost_micros = cost_micros + ?, attempts = attempts + ?, "
        "cost_estimated = MAX(cost_estimated, ?), provider = COALESCE(?, provider), "
        "model = COALESCE(?, model), tier = COALESCE(?, tier), "
        "secret_ref = COALESCE(?, secret_ref), prompt_id = COALESCE(?, prompt_id), "
        "prompt_version = COALESCE(?, prompt_version) WHERE id = ?",
        (
            tokens_in,
            tokens_out,
            cost_micros,
            attempts,
            int(cost_estimated),
            provider,
            model,
            tier,
            secret_ref,
            prompt_id,
            prompt_version,
            step_id,
        ),
    )
    return int(cursor.rowcount) == 1


def finish_step(
    conn: Connection, step_id: str, *, status: str, now: str, error_code: str | None = None
) -> bool:
    """Cierra un paso `running`. `False` si ya estaba cerrado."""
    if status not in STEP_FINAL_STATUSES:
        raise ValueError("estado final de paso desconocido")
    cursor = conn.execute(
        "UPDATE agent_steps SET status = ?, error_code = ?, finished_at = ? "
        "WHERE id = ? AND status = 'running'",
        (status, error_code, now, step_id),
    )
    return int(cursor.rowcount) == 1


def cancel_running_steps(conn: Connection, run_id: str, *, now: str) -> int:
    """Pasos que quedaron `running` tras un cierre brusco → `cancelled`."""
    cursor = conn.execute(
        "UPDATE agent_steps SET status = 'cancelled', finished_at = ? "
        "WHERE run_id = ? AND status = 'running'",
        (now, run_id),
    )
    return int(cursor.rowcount)
