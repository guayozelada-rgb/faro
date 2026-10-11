"""Cola sobre `agent_runs` (spec F1b §4.3 y §9.2): deduplicación, prioridades, toma
condicional (doble toma), cancelación en cada estado, reanudación, tope diario y eventos
con el `seq` de la base."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from faro_engine.core.jobs.queue import RunQueue, result_of
from faro_engine.core.store.runs import NewRun
from tests.jobs.world import REVOKED_SITE, SITE, JobWorld, add_approval, run_id

R1 = run_id(1)
R2 = run_id(2)
R3 = run_id(3)


def queue(world: JobWorld) -> RunQueue:
    return world.jobs.queue


async def test_encolar_deduplica_por_agente_y_sitio(world: JobWorld) -> None:
    first = await world.add_run(R1)
    assert first.status == "queued"
    duplicate = NewRun(
        id=R2,
        agent_kind="test_agent",
        agent_version=1,
        objective="test_agent.run",
        trigger="schedule",
        token_budget=1,
        max_cost_micros=1,
        created_at="2026-10-09T15:00:00Z",
        site_id=SITE,
    )
    assert await queue(world).enqueue(duplicate) is None
    other_site = await world.add_run(R3, site_id=REVOKED_SITE)
    assert other_site.id == R3
    assert world.statuses(R1) == ["queued"]


async def test_siguiente_por_prioridad_y_antiguedad_sin_las_del_tope(world: JobWorld) -> None:
    await world.add_run(R1, priority=2, created_at="2026-10-09T10:00:00Z")
    await world.add_run(R2, priority=1, site_id=REVOKED_SITE, created_at="2026-10-09T12:00:00Z")
    await world.add_run(R3, priority=1, site_id=None, created_at="2026-10-09T11:00:00Z")
    peeked = await queue(world).peek()
    assert peeked is not None
    assert peeked.id == R3
    assert await queue(world).set_reason(R3, status="queued", reason="daily_limit")
    peeked = await queue(world).peek()
    assert peeked is not None
    assert peeked.id == R2
    assert not await queue(world).set_reason(R3, status="running", reason="x")
    assert await queue(world).release_daily_limit() == 1
    assert await queue(world).release_daily_limit() == 0
    peeked = await queue(world).peek()
    assert peeked is not None
    assert peeked.id == R3


async def test_doble_toma_solo_gana_una(world: JobWorld) -> None:
    await world.add_run(R1)
    first, second = await asyncio.gather(queue(world).claim(R1), queue(world).claim(R1))
    winners = [r for r in (first, second) if r is not None]
    assert len(winners) == 1
    assert winners[0].status == "running"
    assert world.statuses(R1) == ["queued", "running"]
    assert [line["seq"] for line in world.activity(R1)] == [1, 2]


async def test_transicion_condicional_y_resultado(world: JobWorld) -> None:
    await world.add_run(R1)
    assert await queue(world).transition(R1, from_status="running", to_status="failed") is None
    await queue(world).claim(R1)
    after = await queue(world).transition(
        R1, from_status="running", to_status="succeeded", result={"ok": True}
    )
    assert after is not None
    assert result_of(after) == {"ok": True}
    assert await queue(world).update(R1, {"current_step": "paso"})
    with pytest.raises(ValueError, match="transición"):
        await queue(world).transition(R1, from_status="succeeded", to_status="queued")


def test_result_of_sin_resultado_o_no_objeto(world: JobWorld) -> None:
    record: Any = type("R", (), {"result": None})()
    assert result_of(record) is None
    record.result = "[1, 2]"
    assert result_of(record) is None


@pytest.mark.parametrize("state", ["queued", "paused", "waiting_approval"])
async def test_cancelar_al_momento_en_cada_estado(world: JobWorld, state: str) -> None:
    await world.add_run(R1)
    if state != "queued":
        await queue(world).claim(R1)
        await queue(world).transition(R1, from_status="running", to_status=state)
    add_approval(world.database, run_id(90), R1)
    add_approval(world.database, run_id(91), R1, status="approved")
    result = await queue(world).cancel(R1)
    assert result.outcome == "cancelled"
    assert result.approvals_cancelled == 2
    assert world.run(R1).status == "cancelled"
    assert world.run(R1).status_reason == "user_cancelled"
    assert world.statuses(R1)[-1] == "cancelled"
    assert world.query("SELECT DISTINCT status FROM approvals") == [("cancelled",)]


async def test_cancelar_terminada_o_inexistente(world: JobWorld) -> None:
    await world.add_run(R1)
    await queue(world).claim(R1)
    await queue(world).transition(R1, from_status="running", to_status="succeeded")
    assert (await queue(world).cancel(R1)).outcome == "not_cancellable"
    assert (await queue(world).cancel(R2)).outcome == "not_found"


async def test_cancelar_en_curso_la_marca_y_el_trabajador_la_cancela(world: JobWorld) -> None:
    await world.add_run(R1)
    await queue(world).claim(R1)
    marked = await queue(world).cancel(R1)
    assert marked.outcome == "running"
    assert marked.run is not None
    assert marked.run.status_reason == "cancel_requested"
    done = await queue(world).cancel(R1, include_running=True)
    assert done.outcome == "cancelled"


async def test_reanudar_solo_las_pausadas_por_pausa_o_cierre(world: JobWorld) -> None:
    for rid, site, reason in (
        (R1, SITE, "agents_paused"),
        (R2, REVOKED_SITE, "interrupted"),
        (R3, None, "otro_motivo"),
    ):
        await world.add_run(rid, site_id=site)
        await queue(world).claim(rid)
        await queue(world).transition(
            rid, from_status="running", to_status="paused", status_reason=reason
        )
    resumed = await queue(world).resume_paused()
    assert sorted(resumed) == [R1, R2]
    assert world.run(R1).priority == 1
    assert world.run(R3).status == "paused"
    assert await queue(world).resume_paused() == []


async def test_aviso_al_trabajador(world: JobWorld) -> None:
    q = queue(world)
    q.clear_notice()
    assert await q.wait_for_work(0.01) is False
    q.notify()
    assert await q.wait_for_work(1) is True


async def test_un_paso_con_otra_forma_no_rompe_la_transicion(
    world: JobWorld, log_stream: Any
) -> None:
    await world.add_run(R1)
    await queue(world).update(R1, {"current_step": "Paso Inválido"})
    after = await queue(world).claim(R1)
    assert after is not None
    assert after.status == "running"
    assert "jobs.activity_invalid" in log_stream.getvalue()


async def test_reanudar_tolera_una_transicion_perdida(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.add_run(R1)
    await queue(world).claim(R1)
    await queue(world).transition(
        R1, from_status="running", to_status="paused", status_reason="agents_paused"
    )

    async def lost(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue(world), "transition", lost)
    assert await queue(world).resume_paused() == []


async def test_agente_sin_sitio_no_necesita_uno(world: JobWorld) -> None:
    from tests.fakes.agents import StepAgent

    assert await world.jobs.submitter.site(StepAgent(requires_site=False), None) is None
