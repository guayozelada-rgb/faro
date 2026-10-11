"""Catálogo de acciones y ejecutor idempotente (ADR 0016 §1, §3 y §5; spec F1b §9.2).

Guardarraíles con su prueba: 1 (no existe acción de borrado: el registro no arranca),
3 (`publish`/`spend` como mucho nivel 1, nada sin pasar por el ejecutor, que relee la
aprobación de la base) y 5 (con la pausa global no se ejecuta nada, tampoco lo aprobado)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from faro_engine.agents.framework.actions import (
    ActionCatalog,
    ActionExecutor,
    ActionInput,
    ActionRegistryError,
    ActionSpec,
    register_actions,
    validate_payload,
)
from faro_engine.agents.framework.state import RunContext
from faro_engine.core.audit import AuditLog
from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database
from faro_engine.core.errors import SITE_REVOKED, FaroError, site_error
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.runner import RunStopped
from faro_engine.core.store import approvals
from faro_engine.core.store.runs import NewRun, insert_run
from tests.fakes.graph_agent import SAVE_ACTION, SaveNotesPayload
from tests.jobs.world import SITE, run_id, set_control

R1 = run_id(1)
R2 = run_id(2)
A1 = run_id(101)


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class LoosePayload(BaseModel):
    value: str


def noop(_conn: Connection, _action: ActionInput) -> str | None:
    return None


def spec(**over: Any) -> ActionSpec:
    base = ActionSpec(
        action_kind="demo.save",
        side_effect="internal",
        payload_schema=Payload,
        max_level=3,
        run=noop,
    )
    return replace(base, **over)


# --- Registro (guardarraíles 1 y 3) -------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"side_effect": "delete"},
        {"action_kind": "demo.delete"},
        {"action_kind": "demo.remove_page"},
        {"action_kind": "wp.borrar"},
        {"action_kind": "ads.drop_campaign"},
        {"action_kind": "wp.unpublish"},
    ],
)
def test_no_existe_accion_de_borrado(over: dict[str, Any]) -> None:
    with pytest.raises(ActionRegistryError, match="borr"):
        register_actions(spec(**over))


@pytest.mark.parametrize(("side_effect", "level"), [("publish", 2), ("spend", 2), ("spend", 3)])
def test_publicar_o_gastar_como_mucho_nivel_1(side_effect: str, level: int) -> None:
    with pytest.raises(ActionRegistryError, match="nivel"):
        register_actions(spec(side_effect=side_effect, max_level=level))
    register_actions(spec(side_effect=side_effect, max_level=1))


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"action_kind": "sin_punto"}, "nombre"),
        ({"max_level": 4}, "nivel"),
        ({"max_level": -1}, "nivel"),
        ({"payload_schema": LoosePayload}, "campos de más"),
    ],
)
def test_otras_reglas_del_registro(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ActionRegistryError, match=message):
        register_actions(spec(**over))


def test_catalogo() -> None:
    catalog = register_actions(spec(), spec(action_kind="demo.other"))
    assert [s.action_kind for s in catalog.all()] == ["demo.other", "demo.save"]
    assert catalog.get("demo.save") is not None
    assert catalog.get("demo.nope") is None
    with pytest.raises(KeyError):
        catalog.require("demo.nope")
    with pytest.raises(ActionRegistryError, match="repetida"):
        register_actions(spec(), spec())
    assert spec().info.side_effect == "internal"
    assert spec(max_level=1).info.max_level == 1


def test_validar_payload() -> None:
    assert validate_payload(spec(), {"value": "x"}) == Payload(value="x")
    assert validate_payload(spec(), Payload(value="y")) == Payload(value="y")
    with pytest.raises(ValueError, match="extra"):
        validate_payload(spec(), {"value": "x", "otro": 1})


# --- Ejecutor -----------------------------------------------------------------------------


def ctx(run: str = R1) -> RunContext:
    return RunContext(
        run_id=run,
        agent_kind="test_graph",
        agent_version=1,
        site_id=SITE,
        provider="anthropic",
        trigger="user",
        token_budget=10,
        max_cost_micros=10,
    )


def add_run(database: Database, rid: str) -> None:
    database.run_sync(
        lambda c: insert_run(
            c,
            NewRun(
                id=rid,
                agent_kind="test_graph",
                agent_version=1,
                objective="test_graph.run",
                trigger="user",
                token_budget=10,
                max_cost_micros=10,
                created_at="2026-10-09T15:00:00Z",
                site_id=SITE,
            ),
            deduplicate=False,
        )
    )


def add_approval(
    database: Database,
    *,
    status: str = "approved",
    side_effect: str = "internal",
    action_kind: str = "test_graph.save",
    payload: str = '{"kind":"test_graph.save","summary":{"headline":"Hola","version":1}}',
    run: str = R1,
    approval_id: str = A1,
) -> None:
    database.run_sync(
        lambda c: approvals.insert_approval(
            c,
            approvals.NewApproval(
                id=approval_id,
                run_id=run,
                agent_kind="test_graph",
                action_kind=action_kind,
                side_effect=side_effect,
                autonomy_level=1,
                payload=payload,
                idempotency_key=f"{run}:{action_kind}",
                created_at="2026-10-09T15:00:00Z",
                expires_at="2026-10-23T15:00:00Z",
                site_id=SITE,
            ),
            status="approved" if status == "approved" else "pending",
            decided_by="user" if status == "approved" else None,
        )
    )


@pytest.fixture
def control() -> AgentsControlState:
    state = AgentsControlState()
    set_control(state, paused=False, providers=["anthropic"])
    return state


def executor(database: Database, control: AgentsControlState, *specs: ActionSpec) -> ActionExecutor:
    return ActionExecutor(
        database=database,
        control=control,
        catalog=ActionCatalog(specs or (SAVE_ACTION,)),
        audit=AuditLog(database),
    )


def status(database: Database, approval_id: str = A1) -> tuple[str, str | None]:
    record = database.run_sync(lambda c: approvals.get_approval(c, approval_id))
    assert record is not None
    return record.status, record.error_code


def summaries(database: Database) -> int:
    return int(
        database.run_sync(lambda c: c.execute("SELECT count(*) FROM site_summaries").fetchone()[0])
    )


async def test_ejecuta_una_sola_vez_con_la_misma_clave(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    add_approval(database)
    run = executor(database, control)
    assert await run.execute(ctx(), A1) is True
    assert status(database) == ("executed", None)
    assert summaries(database) == 1
    assert await run.execute(ctx(), A1) is False  # el nodo se repite: nada más
    assert summaries(database) == 1
    rows = database.run_sync(
        lambda c: c.execute(
            "SELECT actor, details FROM audit_log WHERE action = 'approval.executed'"
        ).fetchall()
    )
    assert [(row[0], json.loads(row[1])) for row in rows] == [
        ("agent", {"agent_kind": "test_graph", "approval_id": A1})
    ]


async def test_sin_aprobacion_ejecutable_o_de_otra_tarea_no_hace_nada(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    add_run(database, R2)
    add_approval(database, status="pending")
    run = executor(database, control)
    assert await run.execute(ctx(), A1) is False  # pendiente
    assert await run.execute(ctx(), run_id(999)) is False  # no existe
    add_approval(database, run=R2, approval_id=run_id(102))
    assert await run.execute(ctx(R1), run_id(102)) is False  # de otra tarea: nunca
    assert summaries(database) == 0


async def test_pausa_global_no_ejecuta_ni_lo_aprobado(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    add_approval(database)
    set_control(control, paused=True, providers=["anthropic"])
    with pytest.raises(RunStopped) as stopped:
        await executor(database, control).execute(ctx(), A1)
    assert stopped.value.reason == "agents_paused"
    assert status(database) == ("approved", None)  # queda pendiente hasta reanudar
    assert summaries(database) == 0


async def test_publicar_o_gastar_no_se_ejecuta_en_f1b(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    publish = spec(action_kind="test_graph.save", side_effect="publish", max_level=1)
    add_approval(database, side_effect="publish")
    with pytest.raises(FaroError) as err:
        await executor(database, control, publish).execute(ctx(), A1)
    assert err.value.code == "internal.unexpected"
    assert status(database) == ("failed", "internal.unexpected")


async def test_accion_desconocida_o_de_otra_clase(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    add_approval(database, action_kind="test_graph.other")
    with pytest.raises(FaroError):
        await executor(database, control).execute(ctx(), A1)
    assert status(database) == ("failed", "internal.unexpected")


async def test_payload_que_no_cumple_el_esquema(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    add_approval(database, payload='{"kind":"test_graph.save","otro":1}')
    with pytest.raises(FaroError):
        await executor(database, control).execute(ctx(), A1)
    assert status(database) == ("failed", "internal.unexpected")
    assert summaries(database) == 0


async def test_error_de_la_accion_marca_la_aprobacion_y_deshace_el_efecto(
    database: Database, control: AgentsControlState
) -> None:
    add_run(database, R1)
    add_approval(database)

    def revoked(conn: Connection, action: ActionInput) -> str | None:
        SAVE_ACTION.run(conn, action)  # el efecto local se deshace con la transacción
        raise site_error(SITE_REVOKED)

    def crash(conn: Connection, action: ActionInput) -> str | None:
        raise RuntimeError("contenido")

    with pytest.raises(FaroError) as err:
        await executor(database, control, replace(SAVE_ACTION, run=revoked)).execute(ctx(), A1)
    assert err.value.code == "site.revoked"
    assert status(database) == ("failed", "site.revoked")
    assert summaries(database) == 0

    add_approval(database, approval_id=run_id(103), run=R1, action_kind="test_graph.save2")
    crashing = replace(SAVE_ACTION, action_kind="test_graph.save2", run=crash)
    with pytest.raises(FaroError) as err:
        await executor(database, control, crashing).execute(ctx(), run_id(103))
    assert err.value.code == "internal.unexpected"


async def test_guarda_el_valor_anterior(database: Database, control: AgentsControlState) -> None:
    add_run(database, R1)
    add_run(database, R2)
    add_approval(database)
    add_approval(database, run=R2, approval_id=run_id(102))
    run = executor(database, control)
    await run.execute(ctx(R1), A1)
    await run.execute(ctx(R2), run_id(102))
    second = database.run_sync(lambda c: approvals.get_approval(c, run_id(102)))
    assert second is not None
    assert second.previous_value is not None
    assert SaveNotesPayload.model_validate_json(second.payload).summary.headline == "Hola"
