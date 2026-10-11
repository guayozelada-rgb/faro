"""Propuestas (ADR 0016 §5, spec F1b §9.2): crear (idempotente, `payload` validado,
caducidad del ajuste), decidir dos veces, caducar, la tarea aún corriendo (error
reintentable) y aceptar una sugerencia de nivel 0."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from faro_engine.agents.framework.approvals import (
    ApprovalService,
    accept_suggestion,
    check_evidence,
    decide,
    decision_of,
    idempotency_key_for,
)
from faro_engine.agents.framework.state import RunContext
from faro_engine.core.audit import AuditLog
from faro_engine.core.errors import FaroError
from faro_engine.core.store import approvals, settings
from faro_engine.core.store.common import to_json
from tests.agents.world import GraphWorld
from tests.fakes.graph_agent import SAVE_ACTION
from tests.jobs.world import NOW, SITE, run_id

R1 = run_id(1)
R2 = run_id(2)
PAYLOAD: dict[str, Any] = {"kind": "test_graph.save", "summary": {"headline": "Hola"}}


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


async def propose(world: GraphWorld, run: str = R1, **over: Any) -> approvals.ApprovalRecord:
    service = ApprovalService(database=world.database, clock=world.clock)
    values: dict[str, Any] = {
        "step_id": None,
        "action": SAVE_ACTION,
        "payload": PAYLOAD,
        "evidence": {"reason_key": "test_graph.show"},
        "level": 1,
    }
    values.update(over)
    return await service.get_or_create(ctx(run), **values)


async def waiting(world: GraphWorld, run: str = R1) -> approvals.ApprovalRecord:
    await world.add_graph_run(run, site_id=SITE if run == R1 else None)
    world.database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_runs SET status = 'waiting_approval' WHERE id = ?", (run,)
        )
    )
    return await propose(world, run)


async def test_crear_es_idempotente_y_valida(gworld: GraphWorld) -> None:
    await gworld.add_graph_run(R1)
    first = await propose(gworld)
    again = await propose(gworld, payload={**PAYLOAD, "summary": {"headline": "Otro"}})
    assert again.id == first.id  # el nodo se repite: misma propuesta
    assert first.idempotency_key == idempotency_key_for(R1, "test_graph.save")
    assert json.loads(first.payload)["summary"] == {"headline": "Hola", "version": 1}
    assert first.expires_at == "2026-10-23T15:00:00Z"  # 14 días
    assert first.status == "pending"
    assert decision_of(first) is None
    with pytest.raises(ValidationError):
        await propose(gworld, R2, payload={"kind": "test_graph.save"})


async def test_caducidad_sale_del_ajuste_y_la_regla_solo_decide_internal(
    gworld: GraphWorld,
) -> None:
    await gworld.add_graph_run(R1)
    gworld.database.run_sync(
        lambda c: settings.set_setting(
            c, settings.APPROVAL_EXPIRY_DAYS, 3, now="2026-10-09T10:00:00Z"
        )
    )
    record = await propose(gworld, decided_by="rule", level=3)
    assert record.expires_at == "2026-10-12T15:00:00Z"
    assert (record.status, record.decided_by) == ("approved", "rule")
    assert decision_of(record) == "approve"


async def test_leer_para_ejecutar_solo_de_esta_tarea(gworld: GraphWorld) -> None:
    await gworld.add_graph_run(R1)
    service = ApprovalService(database=gworld.database, clock=gworld.clock)
    assert await service.load_for_execution(ctx(), "test_graph.save") is None
    await propose(gworld, decided_by="rule", level=3)
    loaded = await service.load_for_execution(ctx(), "test_graph.save")
    assert loaded is not None
    assert await service.load_for_execution(ctx(R2), "test_graph.save") is None


def test_evidencia() -> None:
    assert check_evidence({"reason_key": "x"}) == {"reason_key": "x"}
    for bad in ({"Mal": "x"}, {"reason_key": ""}, {"reason_key": "x" * 201}, {"reason_key": 1}):
        with pytest.raises(ValueError, match="evidencia"):
            check_evidence(bad)  # type: ignore[arg-type]


# --- Decidir (`decideApproval`) -----------------------------------------------------------


async def test_decidir_y_decidir_dos_veces(gworld: GraphWorld) -> None:
    approval = await waiting(gworld)
    audit = AuditLog(gworld.database)
    decided = await decide(gworld.jobs.queue, audit, approval.id, "approve")
    assert decided.status == "approved"
    assert gworld.run(R1).status == "queued"
    assert gworld.run(R1).priority == 1  # reanudada
    assert gworld.run_statuses(R1)[-1] == "queued"
    rows = gworld.query("SELECT details FROM audit_log WHERE action = 'approval.decided'")
    assert [json.loads(r[0]) for r in rows] == [
        {"agent_kind": "test_graph", "approval_id": approval.id, "decision": "approve"}
    ]
    with pytest.raises(FaroError) as err:
        await decide(gworld.jobs.queue, audit, approval.id, "reject")
    assert (err.value.code, err.value.status) == ("approval.already_decided", 409)


async def test_decidir_inexistente(gworld: GraphWorld) -> None:
    with pytest.raises(FaroError) as err:
        await decide(gworld.jobs.queue, AuditLog(gworld.database), run_id(999), "approve")
    assert (err.value.code, err.value.status) == ("approval.not_found", 404)


async def test_decidir_vencida_la_marca_y_cancela_la_tarea(gworld: GraphWorld) -> None:
    approval = await waiting(gworld)
    gworld.clock.now = NOW.replace(day=24)
    with pytest.raises(FaroError) as err:
        await decide(gworld.jobs.queue, AuditLog(gworld.database), approval.id, "approve")
    assert (err.value.code, err.value.status) == ("approval.expired", 410)
    after = gworld.approval(R1)
    assert after is not None
    assert after.status == "expired"
    assert (gworld.run(R1).status, gworld.run(R1).error_code) == ("cancelled", "approval.expired")
    with pytest.raises(FaroError) as again:
        await decide(gworld.jobs.queue, AuditLog(gworld.database), approval.id, "approve")
    assert again.value.code == "approval.expired"


@pytest.mark.parametrize("status", ["running", "queued", "paused"])
async def test_tarea_aun_preparando_la_propuesta_error_reintentable(
    gworld: GraphWorld, status: str
) -> None:
    approval = await waiting(gworld)
    gworld.database.run_sync(
        lambda c: c.execute("UPDATE agent_runs SET status = ? WHERE id = ?", (status, R1))
    )
    with pytest.raises(FaroError) as err:
        await decide(gworld.jobs.queue, AuditLog(gworld.database), approval.id, "approve")
    assert (err.value.code, err.value.status) == ("approval.not_ready", 409)
    still = gworld.approval(R1)
    assert still is not None
    assert still.status == "pending"  # no se cambió nada


async def test_tarea_terminada_con_propuesta_pendiente(gworld: GraphWorld) -> None:
    approval = await waiting(gworld)
    gworld.database.run_sync(
        lambda c: c.execute("UPDATE agent_runs SET status = 'failed' WHERE id = ?", (R1,))
    )
    with pytest.raises(FaroError) as err:
        await decide(gworld.jobs.queue, AuditLog(gworld.database), approval.id, "approve")
    assert err.value.code == "approval.already_decided"


# --- Aceptar una sugerencia (`acceptAgentSuggestion`) -------------------------------------


def suggestion_result(**over: Any) -> str:
    suggestion = {
        "action_kind": "test_graph.save",
        "payload": PAYLOAD,
        "evidence": {"reason_key": "test_graph.show"},
    }
    suggestion.update(over)
    return to_json({"kind": "test_graph", "suggestion": suggestion})


async def finished(world: GraphWorld, result: str | None, status: str = "succeeded") -> None:
    await world.add_graph_run(R1)
    world.database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_runs SET status = ?, result = ?, finished_at = ? WHERE id = ?",
            (status, result, "2026-10-09T15:00:00Z", R1),
        )
    )


async def accept(world: GraphWorld, run: str = R1) -> approvals.ApprovalRecord:
    return await accept_suggestion(
        world.jobs.queue,
        AuditLog(world.database),
        run,
        actions=lambda _kind, action: SAVE_ACTION if action == "test_graph.save" else None,
        clock=world.clock,
    )


async def test_aceptar_crea_la_aprobacion_del_usuario_y_reencola(gworld: GraphWorld) -> None:
    await finished(gworld, suggestion_result())
    approval = await accept(gworld)
    assert (approval.status, approval.decided_by, approval.autonomy_level) == (
        "approved",
        "user",
        0,
    )
    assert json.loads(approval.payload)["summary"]["version"] == 1
    run = gworld.run(R1)
    assert (run.status, run.priority, run.finished_at) == ("queued", 1, None)
    with pytest.raises(FaroError) as err:
        await accept(gworld)
    assert err.value.code == "agent.no_suggestion"


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (None, "succeeded"),
        (to_json({"kind": "test_graph"}), "succeeded"),
        (suggestion_result(action_kind="test_graph.other"), "succeeded"),
        (suggestion_result(payload="no es un mapa"), "succeeded"),
        (suggestion_result(), "failed"),
        ('["no es un objeto"]', "succeeded"),
    ],
)
async def test_sin_sugerencia(gworld: GraphWorld, result: str | None, status: str) -> None:
    await finished(gworld, result, status)
    with pytest.raises(FaroError) as err:
        await accept(gworld)
    assert (err.value.code, err.value.status) == ("agent.no_suggestion", 409)


async def test_aceptar_tarea_inexistente_o_con_el_agente_ocupado(gworld: GraphWorld) -> None:
    with pytest.raises(FaroError) as err:
        await accept(gworld)
    assert (err.value.code, err.value.status) == ("agent.run_not_found", 404)
    await finished(gworld, suggestion_result())
    await gworld.add_graph_run(R2)  # otra tarea del mismo agente y sitio, en cola
    with pytest.raises(FaroError) as busy:
        await accept(gworld)
    assert busy.value.code == "agent.already_queued"
