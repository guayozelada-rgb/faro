"""Hallazgo 3 de la revisión de T7: una excepción fuera del agente (la base en `peek`,
`claim`, `_finish` o `resume_paused`) ya no detiene al trabajador para toda la sesión. Se
reintenta con espera, lo que quedó a medias vuelve a la cola y `/health` lo informa."""

from __future__ import annotations

from typing import Any

import pytest

from faro_engine.core.jobs.worker import RETRY_DELAYS_SECONDS
from faro_engine.core.store.runs import RunRecord
from tests.jobs.world import JobWorld, eventually, run_id, settled

R1 = run_id(1)
R2 = run_id(2)


def fail_once(target: Any, name: str, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    original = getattr(target, name)
    calls: list[int] = []

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("base no disponible")
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return calls


async def test_peek_falla_una_vez_y_la_siguiente_tarea_se_ejecuta(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch, log_stream: Any
) -> None:
    calls = fail_once(world.jobs.queue, "peek", monkeypatch)
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: world.sleep.pending(RETRY_DELAYS_SECONDS[0]) == 1)
    assert world.jobs.worker.status == "retrying"
    assert world.jobs.worker_health == "retrying"
    assert world.jobs.worker.consecutive_failures == 1
    assert world.run(R1).status == "queued"
    assert '"event": "jobs.worker_error"' in log_stream.getvalue()
    assert "base no disponible" not in log_stream.getvalue()  # solo la clase

    world.sleep.release(RETRY_DELAYS_SECONDS[0])
    await eventually(settled(world, R1, "succeeded"))
    assert len(calls) >= 2
    worker = world.jobs.worker
    await eventually(lambda: worker.consecutive_failures == 0)
    assert (worker.status, world.jobs.worker_health) == ("ok", "ok")


async def test_finish_falla_y_la_tarea_a_medias_vuelve_a_la_cola(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = world.jobs.queue
    original = queue.transition
    failed: list[bool] = []

    async def flaky(run_id: str, **kwargs: Any) -> RunRecord | None:
        if kwargs.get("to_status") == "succeeded" and not failed:
            failed.append(True)
            raise OSError("disco lleno")
        return await original(run_id, **kwargs)

    monkeypatch.setattr(queue, "transition", flaky)
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: world.sleep.pending(RETRY_DELAYS_SECONDS[0]) == 1)
    assert world.run(R1).status == "running"  # se quedó a medias
    await eventually(
        lambda: world.grants.releases == [(R1, "paused")]
    )  # la concesión se soltó igual

    world.sleep.release(RETRY_DELAYS_SECONDS[0])
    await eventually(settled(world, R1, "succeeded"))
    statuses = world.statuses(R1)
    assert statuses[-4:] == ["paused", "queued", "running", "succeeded"]


async def test_la_espera_crece_y_se_queda_en_el_maximo(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken() -> None:
        raise OSError("base caída")

    monkeypatch.setattr(world.jobs.queue, "peek", broken)
    await world.jobs.start()
    world.run_control()
    for delay in (*RETRY_DELAYS_SECONDS, RETRY_DELAYS_SECONDS[-1]):
        await eventually(lambda d=delay: world.sleep.pending(d) == 1)  # type: ignore[misc]
        world.sleep.release(delay)
    retries = [d for d in world.sleep.delays if d in RETRY_DELAYS_SECONDS]
    assert retries[:7] == [*RETRY_DELAYS_SECONDS, RETRY_DELAYS_SECONDS[-1]]
    assert world.jobs.worker.consecutive_failures >= 7


async def test_resume_paused_falla_una_vez(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_once(world.jobs.queue, "resume_paused", monkeypatch)
    await world.jobs.start()
    await world.add_run(R2)
    world.run_control()
    await eventually(lambda: world.sleep.pending(RETRY_DELAYS_SECONDS[0]) == 1)
    world.sleep.release(RETRY_DELAYS_SECONDS[0])
    await eventually(settled(world, R2, "succeeded"))


async def test_estado_del_trabajador_sin_arrancar_y_detenido(world: JobWorld) -> None:
    jobs = world.jobs
    states = [jobs.worker_health]
    await jobs.start()
    states.append(jobs.worker_health)
    await jobs.shutdown()
    states.append(jobs.worker_health)
    assert states == ["not_started", "ok", "stopped"]
