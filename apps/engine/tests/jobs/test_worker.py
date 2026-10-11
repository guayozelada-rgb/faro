"""Trabajador (spec F1b §4.3 y §9.2): estados, concesiones, pausa, cancelación, apagado en
un límite, errores, tope diario en tareas programadas y del usuario."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest

from faro_engine.core.errors import FaroError
from faro_engine.core.jobs.runner import RunInvocation
from faro_engine.core.secrets import SecretError
from faro_engine.core.store.runs import NewStep, insert_step
from faro_engine.llm.client import RawCompletion, ResolvedCall
from faro_engine.llm.pricing import max_call_cost
from tests.fakes.agents import GRANT_DENIED, GRANT_PAUSED, FakeGrants, Gate
from tests.fakes.llm import FakeLLM, make_request
from tests.jobs.world import (
    REVOKED_SITE,
    SITE,
    JobWorld,
    add_approval,
    eventually,
    run_id,
    settled,
    spend,
)

R1 = run_id(1)
R2 = run_id(2)


def status_is(world: JobWorld, rid: str, status: str) -> Any:
    return settled(world, rid, status)


async def started(world: JobWorld) -> None:
    await world.jobs.start()


# --- Camino feliz --------------------------------------------------------------------


async def test_tarea_completa_con_concesion_run_id_y_actividad(world: JobWorld) -> None:
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))

    run = world.run(R1)
    assert json.loads(run.result or "") == {"kind": "test", "steps": 3}
    assert run.started_at is not None
    assert run.finished_at is not None
    assert world.statuses(R1) == ["queued", "running", "succeeded"]
    assert [line["seq"] for line in world.activity(R1)] == [1, 2, 3]
    assert run.activity_seq == 3
    assert world.grants.requests == [
        {"run_id": R1, "agent": "test_agent", "site_id": SITE, "provider": "anthropic",
         "trigger": "user"}
    ]  # fmt: skip
    assert world.grants.releases == [(R1, "succeeded")]
    # El `run_id` de la concesión está fijado mientras corre el agente (canal de secretos).
    assert world.agent.run_ids_seen == [R1]
    [invocation] = world.agent.invocations
    assert invocation.first_start is True
    assert invocation.approval is None
    # T6-C2: el agente recibe el único `LlmService` del motor.
    assert invocation.llm is world.llm
    assert invocation.ctx.provider == "anthropic"
    assert invocation.ctx.trigger == "user"


async def test_sin_agents_control_no_se_ejecuta_nada(world: JobWorld) -> None:
    await started(world)
    await world.add_run(R1)
    await asyncio.sleep(0.05)  # comprobación negativa: el trabajador espera al control
    assert world.run(R1).status == "queued"
    assert world.grants.requests == []
    world.run_control(paused=True)
    await asyncio.sleep(0.05)
    assert world.run(R1).status == "queued"
    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))


async def test_prioridad_usuario_reanudada_programada(world: JobWorld) -> None:
    order: list[str] = []
    original = world.agent.run

    async def spy(invocation: RunInvocation) -> Any:
        order.append(invocation.ctx.run_id)
        return await original(invocation)

    world.agent.run = spy  # type: ignore[method-assign]
    await world.add_run(R1, trigger="schedule", priority=2, site_id=None)
    await world.add_run(R2, trigger="user", priority=0, site_id=REVOKED_SITE)
    await world.add_run(run_id(3), trigger="user", priority=1, created_at="2026-10-09T14:00:00Z")
    await started(world)
    world.run_control()
    await eventually(lambda: len(order) == 3)
    assert order == [R2, run_id(3), R1]


# --- Pausa ------------------------------------------------------------------------------


async def test_pausa_a_mitad_y_reanudacion_desde_su_paso(world: JobWorld) -> None:
    gate = Gate()
    world.agent.hooks[1] = gate
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await gate.entered.wait()
    world.run_control(paused=True)
    gate.opened.set()
    await eventually(status_is(world, R1, "paused"))
    run = world.run(R1)
    assert run.status_reason == "agents_paused"
    assert world.agent.progress[R1] == 2  # se detuvo en el límite antes del paso 3
    assert world.grants.releases == [(R1, "paused")]

    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))
    assert world.statuses(R1) == ["queued", "running", "paused", "queued", "running", "succeeded"]
    assert [inv.first_start for inv in world.agent.invocations] == [True, False]
    assert world.agent.progress[R1] == 3
    assert world.grants.releases == [(R1, "paused"), (R1, "succeeded")]


async def test_agents_paused_de_la_capa_de_ia_deja_la_tarea_en_pausa(world: JobWorld) -> None:
    raised: list[int] = []

    async def paused_llm(_inv: RunInvocation) -> None:
        if not raised:
            raised.append(1)
            raise FaroError.of("agents.paused", 409)

    world.agent.hooks[0] = paused_llm
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))
    assert "paused" in world.statuses(R1)


# --- Cancelación y apagado -----------------------------------------------------------------


async def test_cancelar_una_tarea_en_curso_para_en_el_siguiente_limite(world: JobWorld) -> None:
    gate = Gate()
    world.agent.hooks[1] = gate
    await started(world)
    await world.add_run(R1)
    add_approval(world.database, run_id(90), R1)
    world.run_control()
    await gate.entered.wait()

    result = await world.jobs.queue.cancel(R1)
    assert result.outcome == "running"
    assert world.run(R1).status_reason == "cancel_requested"
    assert world.jobs.worker.request_cancel(R1) is True
    assert world.jobs.worker.request_cancel(R2) is False
    gate.opened.set()
    await eventually(status_is(world, R1, "cancelled"))
    run = world.run(R1)
    assert run.status_reason == "user_cancelled"
    assert world.agent.progress[R1] == 2
    assert world.query("SELECT status FROM approvals") == [("cancelled",)]
    assert world.grants.releases == [(R1, "cancelled")]


async def test_apagado_en_un_limite_deja_la_tarea_en_pausa_interrumpida(world: JobWorld) -> None:
    gate = Gate()
    world.agent.hooks[1] = gate
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await gate.entered.wait()
    world.jobs.request_stop()  # como desde el hilo del protocolo
    await asyncio.sleep(0)
    gate.opened.set()
    await eventually(status_is(world, R1, "paused"))
    assert world.run(R1).status_reason == "interrupted"
    assert world.grants.releases == [(R1, "paused")]
    await world.jobs.shutdown()
    assert world.jobs.worker_task is not None
    assert world.jobs.worker_task.done()


# --- Concesiones ---------------------------------------------------------------------------


async def test_concesion_denegada_falla_sin_liberar(world: JobWorld) -> None:
    world.grants.answers = [GRANT_DENIED]
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "failed"))
    assert world.run(R1).error_code == "agent.grant_denied"
    assert world.grants.releases == []
    assert world.agent.invocations == []


async def test_concesion_en_pausa_vuelve_a_la_cola_hasta_otro_control(world: JobWorld) -> None:
    world.grants.answers = [GRANT_PAUSED]
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: len(world.grants.requests) == 1)
    await eventually(status_is(world, R1, "queued"))
    assert world.statuses(R1)[:3] == ["queued", "running", "queued"]
    world.run_control(providers=["anthropic"])  # el núcleo vuelve a avisar
    await eventually(status_is(world, R1, "succeeded"))
    assert len(world.grants.requests) == 2


async def test_concesion_caducada_se_renueva_una_vez_y_repite_el_paso(world: JobWorld) -> None:
    failures: list[int] = []

    async def expired(_inv: RunInvocation) -> None:
        if not failures:
            failures.append(1)
            raise SecretError("vault.secret_not_allowed")

    world.agent.hooks[1] = expired
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))
    assert len(world.grants.requests) == 2
    assert [inv.first_start for inv in world.agent.invocations] == [True, False]
    assert world.grants.releases == [(R1, "succeeded")]


async def test_renovar_con_el_nucleo_en_pausa_deja_la_tarea_en_pausa(world: JobWorld) -> None:
    failures: list[int] = []

    async def expired(_inv: RunInvocation) -> None:
        if not failures:
            failures.append(1)
            raise SecretError("vault.secret_not_allowed")

    world.agent.hooks[1] = expired
    world.grants.answers = [None, GRANT_PAUSED]
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: "paused" in world.statuses(R1))
    assert world.grants.releases[0] == (R1, "paused")
    await eventually(status_is(world, R1, "succeeded"))  # sigue sin pausa en el motor


@pytest.mark.parametrize("renewal", [None, GRANT_DENIED])
async def test_concesion_caducada_dos_veces_falla(world: JobWorld, renewal: str | None) -> None:
    async def expired(_inv: RunInvocation) -> None:
        raise SecretError("vault.secret_not_allowed")

    world.agent.hooks[0] = expired
    world.grants.answers = [None, renewal]
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "failed"))
    assert world.run(R1).error_code == "agent.grant_denied"
    assert world.grants.releases == [(R1, "failed")]


# --- Errores y finales -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FaroError("llm.timeout", "x", 504), "llm.timeout"),
        (RuntimeError("contenido que no debe salir"), "internal.unexpected"),
    ],
)
async def test_errores_dejan_la_tarea_fallida_con_su_codigo(
    world: JobWorld, error: Exception, code: str, log_stream: Any
) -> None:
    async def fail(_inv: RunInvocation) -> None:
        raise error

    world.agent.hooks[1] = fail
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "failed"))
    assert world.run(R1).error_code == code
    assert world.statuses(R1)[-1] == "failed"
    assert world.activity(R1)[-1]["error_code"] == code
    assert "contenido que no debe salir" not in log_stream.getvalue()


async def test_espera_de_decision(world: JobWorld) -> None:
    world.agent.outcome = "waiting_approval"
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "waiting_approval"))
    assert world.grants.releases == [(R1, "waiting_approval")]


async def test_agente_desconocido_falla_sin_pedir_concesion(world: JobWorld) -> None:
    await started(world)
    await world.add_run(R1, agent_kind="otro_agente")
    world.run_control()
    await eventually(status_is(world, R1, "failed"))
    assert world.run(R1).error_code == "agent.unknown"
    assert world.grants.requests == []


async def test_sin_clave_falla_con_no_key(world: JobWorld) -> None:
    await started(world)
    await world.add_run(R1)
    world.run_control(providers=["openai"])
    await eventually(status_is(world, R1, "failed"))
    assert world.run(R1).error_code == "llm.no_key"
    assert world.grants.requests == []


async def test_tarea_sin_proveedor_usa_la_primera_clave_y_lo_guarda(world: JobWorld) -> None:
    await started(world)
    await world.add_run(R1, provider=None)
    world.run_control(providers=["openai", "gemini"])
    await eventually(status_is(world, R1, "succeeded"))
    assert world.grants.requests[0]["provider"] == "openai"
    assert world.run(R1).provider == "openai"


async def test_una_toma_perdida_no_ejecuta_nada(world: JobWorld) -> None:
    world.run_control()
    await world.add_run(R1)
    candidate = world.run(R1)
    assert await world.jobs.queue.claim(R1) is not None
    await world.jobs.worker.take(candidate)  # otra toma ya ganó
    assert world.grants.requests == []


# --- Tope diario ---------------------------------------------------------------------------


async def test_tope_diario_programada_espera_a_otro_dia_y_del_usuario_falla(
    world: JobWorld,
) -> None:
    spend(world.database, 495_000, limit=500_000)  # quedan 5 000 y el máximo es 10 000
    await world.add_run(R1, trigger="schedule", priority=2)
    await world.add_run(R2, trigger="user", priority=0, site_id=REVOKED_SITE)
    await started(world)
    world.run_control()
    await eventually(status_is(world, R2, "failed"))
    assert world.run(R2).error_code == "llm.daily_limit_reached"
    await eventually(lambda: world.run(R1).status_reason == "daily_limit")
    assert world.run(R1).status == "queued"
    assert world.statuses(R1) == ["queued"]  # nunca pasó a `running`
    assert world.grants.requests == []

    # Otro día local: se libera y corre.
    world.clock.now += timedelta(days=1)
    world.jobs.queue.notify()
    await eventually(status_is(world, R1, "succeeded"))


async def test_tarea_ya_empezada_no_se_frena_por_el_tope(world: JobWorld) -> None:
    spend(world.database, 495_000, limit=500_000)
    await world.add_run(R1, trigger="schedule", priority=2)
    world.database.run_sync(
        lambda c: c.execute(
            "UPDATE agent_runs SET started_at = '2026-10-09T14:00:00Z' WHERE id = ?", (R1,)
        )
    )
    await started(world)
    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))
    assert world.agent.invocations[0].first_start is False


class GatedLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        self.started.set()
        await self.gate.wait()
        return await FakeLLM.complete(self, call, api_key)


async def test_dos_llamadas_a_la_vez_desde_el_trabajador_no_pasan_el_tope(
    world: JobWorld,
) -> None:
    """T6-C2: el trabajador usa el único `LlmService` (un `DailyLimiter`)."""
    gated = GatedLLM()
    world.llm_client = gated
    request = make_request(max_output_tokens=100)
    model = world.llm.catalog.model_for("anthropic", "economy")
    one = max_call_cost(model, request.prompt_chars, 100, world.llm.today())
    outcomes: list[str] = []

    async def two_calls(inv: RunInvocation) -> None:
        # Ya empezada (el tope previo del trabajador no aplica): cabe exactamente una llamada.
        spend(world.database, 500_000 - one, limit=500_000)
        step_id = run_id(77)
        world.database.run_sync(
            lambda c: insert_step(
                c,
                NewStep(
                    id=step_id,
                    run_id=inv.ctx.run_id,
                    node="llm_node",
                    kind="llm_call",
                    idempotency_key=step_id,
                    started_at="2026-10-09T15:00:00Z",
                ),
            )
        )

        class Step:
            pass

        step = Step()
        step.step_id = step_id  # type: ignore[attr-defined]
        first = asyncio.create_task(inv.llm.call(inv.ctx, step, request))  # type: ignore[arg-type]
        await gated.started.wait()
        try:
            await inv.llm.call(inv.ctx, step, request)  # type: ignore[arg-type]
        except FaroError as err:
            outcomes.append(err.code)
        gated.gate.set()
        await first
        outcomes.append("ok")

    world.agent.hooks[0] = two_calls
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "succeeded"))
    assert outcomes == ["llm.daily_limit_reached", "ok"]
    assert len(gated.calls) == 1
    [(spent,)] = world.query("SELECT cost_micros FROM credential_usage")
    assert spent <= 500_000
    assert world.llm.limiter.reserved("/".join(("llm", "anthropic", "default"))) == 0


# --- Apagado y esperas del control ------------------------------------------------------------


async def test_parar_mientras_espera_el_control_sale_del_bucle(world: JobWorld) -> None:
    await started(world)
    for _ in range(10):  # el trabajador llega a esperar el control
        await asyncio.sleep(0)
    world.jobs.worker.request_stop()
    world.run_control()
    task = world.jobs.worker_task
    assert task is not None
    await asyncio.wait_for(task, 2)
    assert world.jobs.worker.busy is False


async def test_tarea_tomada_durante_el_apagado_para_en_el_primer_limite(world: JobWorld) -> None:
    world.run_control()
    await world.add_run(R1)
    world.jobs.worker.request_stop()
    await world.jobs.worker.take(world.run(R1))
    assert world.run(R1).status == "paused"
    assert world.run(R1).status_reason == "interrupted"
    assert world.agent.progress.get(R1, 0) == 0


async def test_espera_del_control_tras_concesion_en_pausa(world: JobWorld) -> None:
    from faro_engine.core.jobs.worker import Worker

    world.run_control()
    worker = Worker(
        queue=world.jobs.queue,
        control=world.control,
        grants=world.grants,
        llm=world.llm,
        agents=world.jobs.agents,
        grant_paused_backoff=0.01,
    )
    before = world.control.snapshot()
    world.run_control(providers=["openai"])
    await worker._wait_control_change(before)  # ya cambió: vuelve al momento
    await worker._wait_control_change(world.control.snapshot())  # vence el plazo
    assert worker._seconds_to_midnight() >= 1.0


async def test_reloj_por_defecto_en_utc() -> None:
    from faro_engine.core.jobs import queue, runtime, scheduler, worker

    for module in (queue, runtime, scheduler, worker):
        assert module._utc_now().tzinfo is not None


class CancellingGrants(FakeGrants):
    """El usuario cancela la tarea mientras el núcleo decide la concesión."""

    queue: Any = None

    async def request(self, **kwargs: Any) -> Any:
        await self.queue.cancel(kwargs["run_id"])  # la ruta: `running` → `cancel_requested`
        return await super().request(**kwargs)


@pytest.mark.parametrize("answer", [None, GRANT_PAUSED])
async def test_cancelar_mientras_se_pide_la_concesion(world: JobWorld, answer: str | None) -> None:
    grants = CancellingGrants(answers=[answer])
    world.grants = grants
    grants.queue = world.jobs.queue
    await started(world)
    await world.add_run(R1)
    world.run_control()
    await eventually(status_is(world, R1, "cancelled"))
    assert world.agent.progress.get(R1, 0) == 0
    assert grants.releases == ([(R1, "cancelled")] if answer is None else [])
