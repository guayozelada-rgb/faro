"""Arranque, recuperación y apagado del sistema de tareas (spec F1b §4.3 y §9.2).

- Cierre brusco: una tarea `running` → al arrancar `paused` (`interrupted`) con sus pasos
  cancelados → se vuelve a encolar cuando los agentes pueden trabajar y sigue desde su
  paso. Con pausa global se queda en pausa.
- Apagado con una llamada al LLM en curso (condición T6-C1): se cancela dentro del plazo
  de gracia y `credential_usage` tiene su máximo (`cost_estimated = 1`) antes de salir.
- `catch_up` 60 s después del primer `agents_control` sin pausa; nada si está pausado.
- Caducidad de propuestas al arrancar y cada 24 h.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from typing import Any

import pytest

from faro_engine.core.db.database import Database
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.runner import AgentCatalog, RunInvocation
from faro_engine.core.jobs.runtime import JobSystem, serve_with_jobs
from faro_engine.core.store import schedules as store
from faro_engine.core.store.common import format_utc
from faro_engine.core.store.runs import NewStep, insert_step
from faro_engine.core.store.schedules import NewSchedule
from faro_engine.llm.client import RawCompletion, ResolvedCall
from faro_engine.llm.pricing import max_call_cost
from tests.fakes.agents import FakeGrants, StepAgent
from tests.fakes.llm import FakeLLM, make_request
from tests.jobs.world import (
    NOW,
    SITE,
    JobWorld,
    add_approval,
    eventually,
    run_id,
    settled,
)

R1 = run_id(1)
ANTHROPIC_REF = "/".join(("llm", "anthropic", "default"))


class HangingLLM(FakeLLM):
    """Nunca responde: la llamada está en curso cuando llega el apagado."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:  # noqa: ARG002
        self.calls.append(call)
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("inalcanzable")  # pragma: no cover


async def test_base_no_disponible_no_arranca_nada(world: JobWorld, log_stream: Any) -> None:
    jobs = JobSystem(
        database=Database.unavailable("db.key_missing"),
        control=world.control,
        grants=world.grants,
        activity=ActivityEmitter(None),
        llm=world.llm,
        agents=AgentCatalog(),
    )
    assert await jobs.start() is False
    assert not jobs.started
    await jobs.shutdown()
    assert "jobs.disabled" in log_stream.getvalue()


async def test_fallo_al_arrancar_deja_el_motor_sin_agentes(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch, log_stream: Any
) -> None:
    async def boom() -> None:
        raise RuntimeError("programador")

    monkeypatch.setattr(world.jobs.scheduler, "start", boom)
    assert await world.jobs.start() is False
    assert "jobs.start_failed" in log_stream.getvalue()


async def test_parar_antes_de_arrancar_y_desde_otro_hilo(world: JobWorld) -> None:
    world.jobs.request_stop()  # aún sin bucle: queda anotado
    assert await world.jobs.start() is False
    other = JobWorld(
        database=world.database,
        control=world.control,
        grants=FakeGrants(),
        agent=StepAgent(),
        clock=world.clock,
        sink=world.sink,
        sleep=world.sleep,
        timer=world.timer,
    )
    assert await other.jobs.start() is True
    thread = threading.Thread(target=other.jobs.request_stop)
    thread.start()
    thread.join()
    await other.jobs.shutdown()
    other.jobs.request_stop()  # con el bucle aún vivo pero todo parado: sin efecto


async def test_tarea_de_fondo_que_falla_se_registra(world: JobWorld, log_stream: Any) -> None:
    async def boom() -> None:
        raise RuntimeError("x")

    await JobSystem._guard(boom)
    assert "jobs.task_failed" in log_stream.getvalue()


# --- Cierre brusco y recuperación -----------------------------------------------------------


async def test_cierre_brusco_se_recupera_y_sigue_desde_su_paso(world: JobWorld) -> None:
    await world.add_run(R1)
    run = await world.jobs.queue.claim(R1)
    assert run is not None
    world.agent.progress[R1] = 2  # el "checkpoint" del agente
    step = run_id(70)
    world.database.run_sync(
        lambda c: insert_step(
            c,
            NewStep(
                id=step,
                run_id=R1,
                node="n",
                kind="control",
                idempotency_key=step,
                started_at="2026-10-09T15:00:00Z",
            ),
        )
    )
    # El proceso murió aquí: la tarea quedó `running`. Arranca otro motor.
    await world.jobs.start()
    assert world.run(R1).status == "paused"
    assert world.run(R1).status_reason == "interrupted"
    assert world.query("SELECT status FROM agent_steps") == [("cancelled",)]
    world.run_control()
    await eventually(settled(world, R1, "succeeded"))
    assert world.statuses(R1)[-4:] == ["paused", "queued", "running", "succeeded"]
    assert world.run(R1).priority == 1
    assert world.agent.invocations[0].first_start is False


async def test_recuperada_con_pausa_global_se_queda_en_pausa(world: JobWorld) -> None:
    await world.add_run(R1)
    await world.jobs.queue.claim(R1)
    await world.jobs.start()
    world.run_control(paused=True)
    await asyncio.sleep(0.05)  # comprobación negativa
    assert world.run(R1).status == "paused"
    world.run_control()
    await eventually(settled(world, R1, "succeeded"))


async def test_cancelacion_pedida_antes_del_cierre_se_cumple_al_arrancar(
    world: JobWorld,
) -> None:
    await world.add_run(R1)
    await world.jobs.queue.claim(R1)
    assert (await world.jobs.queue.cancel(R1)).outcome == "running"
    await world.jobs.start()
    assert world.run(R1).status == "cancelled"


async def test_propuestas_caducadas_al_arrancar_y_cada_dia(world: JobWorld) -> None:
    for rid, site in ((R1, SITE), (run_id(2), None)):
        await world.add_run(rid, site_id=site)
        await world.jobs.queue.claim(rid)
        await world.jobs.queue.transition(rid, from_status="running", to_status="waiting_approval")
    add_approval(world.database, run_id(90), R1, expires_at="2026-10-09T14:00:00Z")
    add_approval(world.database, run_id(91), run_id(2), expires_at="2026-10-10T15:00:00Z")
    await world.jobs.start()
    assert world.run(R1).status == "cancelled"
    assert world.run(R1).error_code == "approval.expired"
    assert world.statuses(R1)[-1] == "cancelled"
    assert world.run(run_id(2)).status == "waiting_approval"

    await eventually(lambda: world.sleep.pending(86_400) == 1)
    world.clock.now = NOW + timedelta(days=1, hours=1)
    world.sleep.release(86_400)
    await eventually(lambda: world.run(run_id(2)).status == "cancelled")
    await eventually(lambda: world.sleep.pending(86_400) == 1)


# --- catch_up --------------------------------------------------------------------------------


def _due_schedule(world: JobWorld) -> None:
    schedule = NewSchedule(
        id=run_id(500),
        agent_kind="test_agent",
        site_id=SITE,
        cadence="daily",
        time_local="09:00",
        timezone="America/Santiago",
        next_run_at=format_utc(NOW - timedelta(days=3)),
        created_at="2026-10-01T12:00:00Z",
    )
    world.database.run_sync(lambda c: store.insert_schedule(c, schedule))


def _catch_up_runs(world: JobWorld) -> list[tuple[Any, ...]]:
    return world.query("SELECT id FROM agent_runs WHERE trigger = 'catch_up'")


async def test_catch_up_sesenta_segundos_despues_del_control_sin_pausa(world: JobWorld) -> None:
    _due_schedule(world)
    await world.jobs.start()
    await asyncio.sleep(0.05)
    assert world.sleep.pending(60) == 0  # sin `agents_control` no cuenta el plazo
    world.run_control()
    await eventually(lambda: world.sleep.pending(60) == 1)
    assert _catch_up_runs(world) == []
    world.sleep.release(60)
    await eventually(lambda: len(_catch_up_runs(world)) == 1)
    [(rid,)] = _catch_up_runs(world)
    await eventually(settled(world, rid, "succeeded"))
    assert world.grants.requests[-1]["trigger"] == "catch_up"


async def test_catch_up_nada_si_esta_en_pausa(world: JobWorld) -> None:
    _due_schedule(world)
    await world.jobs.start()
    world.run_control()
    await eventually(lambda: world.sleep.pending(60) == 1)
    world.run_control(paused=True)  # pausa durante el plazo
    world.sleep.release(60)
    await asyncio.sleep(0.05)
    assert _catch_up_runs(world) == []
    world.run_control()
    await eventually(lambda: world.sleep.pending(60) == 1)
    world.sleep.release(60)
    await eventually(lambda: len(_catch_up_runs(world)) == 1)


# --- Apagado ------------------------------------------------------------------------------


async def test_apagado_sin_tarea_es_inmediato(world: JobWorld) -> None:
    await world.jobs.start()
    world.run_control()
    loop = asyncio.get_running_loop()
    start = loop.time()
    await world.jobs.shutdown()
    assert loop.time() - start < 0.4  # no espera los 0,5 s de `stop_wait`
    assert world.timer.stopped


async def test_apagado_con_llamada_en_curso_registra_su_maximo(world: JobWorld) -> None:
    """T6-C1 (a): la llamada se cancela dentro del plazo y su máximo cuenta en el tope."""
    hanging = HangingLLM()
    world.llm_client = hanging
    request = make_request(max_output_tokens=100)
    step = run_id(71)

    async def call_llm(inv: RunInvocation) -> None:
        world.database.run_sync(
            lambda c: insert_step(
                c,
                NewStep(
                    id=step,
                    run_id=inv.ctx.run_id,
                    node="llm_node",
                    kind="llm_call",
                    idempotency_key=step,
                    started_at="2026-10-09T15:00:00Z",
                ),
            )
        )

        class Step:
            step_id = step

        await inv.llm.call(inv.ctx, Step(), request)

    world.agent.hooks[1] = call_llm
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await hanging.entered.wait()

    world.jobs.request_stop()
    await world.jobs.shutdown()

    maximum = max_call_cost(
        world.llm.catalog.model_for("anthropic", "economy"),
        request.prompt_chars,
        100,
        world.llm.today(),
    )
    assert world.query("SELECT cost_micros, requests FROM credential_usage") == [(maximum, 1)]
    assert world.query(
        "SELECT attempts, cost_micros, cost_estimated FROM agent_steps WHERE id = ?", (step,)
    ) == [(1, maximum, 1)]
    assert world.llm.limiter.reserved(ANTHROPIC_REF) == 0
    # La tarea se queda `running`; la recuperación del siguiente arranque la retoma.
    assert world.run(R1).status == "running"
    assert world.grants.releases == [(R1, "paused")]
    assert world.jobs.worker_task is not None
    assert world.jobs.worker_task.done()


async def test_serve_with_jobs_arranca_y_para_alrededor_del_servidor(world: JobWorld) -> None:
    events: list[str] = []

    async def serve() -> None:
        events.append(f"serve:{world.jobs.started}")

    await serve_with_jobs(serve, world.jobs)
    assert events == ["serve:True"]
    assert world.jobs.worker_task is not None
    assert world.jobs.worker_task.done()


async def test_recuperacion_tolera_cambios_concurrentes(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ramas defensivas: otro camino cambió la tarea entre la lectura y la escritura."""
    from faro_engine.core.jobs import recovery
    from faro_engine.core.store.run_control import CancelResult

    for rid, site in ((R1, SITE), (run_id(2), None)):
        await world.add_run(rid, site_id=site)
        await world.jobs.queue.claim(rid)
    await world.jobs.queue.cancel(R1)  # `cancel_requested`
    queue = world.jobs.queue

    async def lost_cancel(*_args: Any, **_kwargs: Any) -> CancelResult:
        return CancelResult("not_found", None)

    async def lost_transition(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue, "cancel", lost_cancel)
    monkeypatch.setattr(queue, "transition", lost_transition)
    assert await recovery.recover_interrupted(queue) == ([], [])


async def test_caducidad_con_la_tarea_aun_en_curso_o_borrada(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from faro_engine.core.jobs import recovery

    await world.add_run(R1)
    await world.jobs.queue.claim(R1)  # entre el nodo que propone y la espera
    add_approval(world.database, run_id(90), R1, expires_at="2026-10-09T14:00:00Z")
    expired = await recovery.expire_approvals(world.jobs.queue)
    assert [(e.approval_id, e.run_cancelled) for e in expired] == [(run_id(90), False)]
    assert world.run(R1).status == "running"

    await world.add_run(run_id(2), site_id=None)
    await world.jobs.queue.claim(run_id(2))
    await world.jobs.queue.transition(
        run_id(2), from_status="running", to_status="waiting_approval"
    )
    add_approval(world.database, run_id(91), run_id(2), expires_at="2026-10-09T14:00:00Z")

    async def gone(_run_id: str) -> None:
        return None

    monkeypatch.setattr(world.jobs.queue, "get", gone)
    expired = await recovery.expire_approvals(world.jobs.queue)
    assert [e.run_cancelled for e in expired] == [True]


async def test_apagado_espera_a_que_la_tarea_llegue_a_su_limite(
    world: JobWorld, log_stream: Any
) -> None:
    from tests.fakes.agents import Gate

    gate = Gate()
    world.agent.hooks[1] = gate
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await gate.entered.wait()
    stopping = asyncio.create_task(world.jobs.shutdown())
    await asyncio.sleep(0)
    gate.opened.set()  # termina el paso y para en el siguiente límite
    await stopping
    assert world.run(R1).status == "paused"
    assert world.run(R1).status_reason == "interrupted"
    assert "jobs.worker_cancelled" not in log_stream.getvalue()


async def test_el_plazo_de_cancelacion_cuenta_desde_el_aviso_no_desde_serve(
    world: JobWorld,
) -> None:
    """Revisión de seguridad de T7, hallazgo 1 (T6-C1): aunque `server.serve()` tarde (8 s
    tras el aviso, simulados), la llamada en curso se cancela a los `stop_wait` s del aviso
    y su máximo queda en `credential_usage` **antes** del `os._exit` del controlador."""
    from faro_engine.__main__ import ShutdownController

    hanging = HangingLLM()
    world.llm_client = hanging
    request = make_request(max_output_tokens=100)
    step = run_id(72)

    async def call_llm(inv: RunInvocation) -> None:
        world.database.run_sync(
            lambda c: insert_step(
                c,
                NewStep(
                    id=step,
                    run_id=inv.ctx.run_id,
                    node="llm_node",
                    kind="llm_call",
                    idempotency_key=step,
                    started_at="2026-10-09T15:00:00Z",
                ),
            )
        )

        class Step:
            step_id = step

        await inv.llm.call(inv.ctx, Step(), request)

    world.agent.hooks[1] = call_llm
    forced: list[int] = []
    server_returns = asyncio.Event()

    class SlowServer:
        should_exit = False

        async def serve(self) -> None:
            await server_returns.wait()  # uvicorn esperando peticiones largas

    server = SlowServer()
    controller = ShutdownController(
        server,  # type: ignore[arg-type]
        grace=60,
        force_exit=forced.append,
        on_exit=world.jobs.request_stop,
    )
    serving = asyncio.create_task(serve_with_jobs(server.serve, world.jobs))
    await eventually(lambda: world.jobs.started)
    await world.add_run(R1)
    world.run_control()
    await hanging.entered.wait()

    controller.request_exit("shutdown_event")
    await eventually(lambda: world.sleep.pending(0.5) == 1)  # cancelación programada
    world.sleep.release(0.5)  # pasan los `stop_wait` s desde el aviso
    maximum = max_call_cost(
        world.llm.catalog.model_for("anthropic", "economy"),
        request.prompt_chars,
        100,
        world.llm.today(),
    )
    await eventually(lambda: world.query("SELECT cost_micros FROM credential_usage") != [])
    assert not serving.done()  # `serve()` aún no volvió
    assert forced == []  # y el `os._exit` aún no llegó
    assert world.query("SELECT cost_micros FROM credential_usage") == [(maximum,)]
    assert world.query("SELECT cost_estimated FROM agent_steps WHERE id = ?", (step,)) == [(1,)]
    worker = world.jobs.worker_task
    assert worker is not None
    await eventually(worker.done)

    server_returns.set()
    await serving
    controller.cancel()
    assert world.grants.releases == [(R1, "paused")]


async def test_aviso_de_parada_repetido_o_sin_trabajador(world: JobWorld) -> None:
    await world.jobs.start()
    world.jobs.request_stop()
    world.jobs.request_stop()  # dos veces: una sola cancelación
    await world.jobs.shutdown()
    world.jobs._cancel_worker()  # ya terminó: sin efecto
    assert world.jobs.worker_task is not None
    assert world.jobs.worker_task.done()


async def test_si_serve_vuelve_con_el_plazo_vencido_cancela_al_momento(world: JobWorld) -> None:
    from tests.fakes.agents import Gate

    gate = Gate()
    world.agent.hooks[1] = gate
    world.jobs._stop_wait = 0.0  # `serve()` tardó más que el plazo desde el aviso
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await gate.entered.wait()
    world.jobs.request_stop()
    await eventually(lambda: world.jobs._stop_deadline is not None)
    await world.jobs.shutdown()
    assert world.jobs.worker_task is not None
    assert world.jobs.worker_task.done()
    assert world.run(R1).status == "running"  # la recupera el siguiente arranque
    assert world.grants.releases == [(R1, "paused")]
