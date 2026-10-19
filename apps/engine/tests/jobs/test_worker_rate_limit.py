"""Ritmo de concesiones del núcleo (`agents.grant_rate_limited`, 10 seguidas y luego una por
minuto por tipo de agente): la tarea vuelve a la cola sin fallar ni gastar y el trabajador
espera antes de volver a pedir; la espera la cortan la pausa, el apagado y la cancelación.
Si llega al renovar la concesión a mitad de tarea, queda `paused` y sigue desde su último
estado guardado (spec F1b §4.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from faro_engine.core.errors import AGENTS_GRANT_RATE_LIMITED, AGENTS_PAUSED
from faro_engine.core.jobs.grants import RunGrant, RunGrantError
from faro_engine.core.jobs.runner import RunInvocation
from faro_engine.core.jobs.worker import Worker
from faro_engine.core.secrets import SecretError
from tests.db.helpers import insert_row
from tests.fakes.agents import FakeGrants
from tests.jobs.world import SITE, JobWorld, ManualSleep, eventually, run_id, settled, spend

WAIT = 61.0  # `JobWorld.grant_rate_limited_backoff` (en producción, 60 s)
R1 = run_id(1)
R2 = run_id(2)


@dataclass
class RateLimitedGrants(FakeGrants):
    """Núcleo simulado con el cubo de concesiones de un tipo de agente: `tokens` fichas;
    sin fichas responde `agents.grant_rate_limited`. La prueba repone con `refill`."""

    tokens: int = 10
    limited: int = 0

    async def request(self, **kwargs: Any) -> RunGrant:
        if self.tokens == 0:
            self.requests.append(dict(kwargs))
            self.limited += 1
            raise RunGrantError(AGENTS_GRANT_RATE_LIMITED)
        grant = await super().request(**kwargs)
        self.tokens -= 1
        return grant

    def refill(self) -> None:
        self.tokens += 1


def site_id(n: int) -> str:
    return f"01920000-0000-7000-8000-0000000c{n:04x}"


def add_sites(world: JobWorld, count: int) -> list[str]:
    ids = [site_id(n) for n in range(count)]

    def write(conn: Any) -> None:
        for n, sid in enumerate(ids):
            insert_row(
                conn,
                "sites",
                {
                    "id": sid,
                    "url": f"https://tienda-{n}.test",
                    "name": None,
                    "created_at": "2026-09-30T12:00:00Z",
                    "updated_at": "2026-09-30T12:00:00Z",
                },
            )

    world.database.run_sync(write)
    return ids


def limited(world: JobWorld, count: int = 10) -> RateLimitedGrants:
    grants = RateLimitedGrants(tokens=count)
    world.grants = grants  # antes de crear el sistema de tareas
    return grants


async def test_catch_up_de_mas_de_10_sitios_no_falla_ninguna(world: JobWorld) -> None:
    grants = limited(world)
    sites = add_sites(world, 12)
    await world.jobs.start()
    runs = [run_id(100 + n) for n in range(12)]
    for n, (rid, sid) in enumerate(zip(runs, sites, strict=True)):
        await world.add_run(
            rid,
            site_id=sid,
            trigger="catch_up",
            priority=2,
            created_at=f"2026-10-09T15:00:{n:02d}Z",
        )
    world.run_control()

    await eventually(lambda: world.sleep.pending(WAIT) == 1)
    assert [world.run(rid).status for rid in runs[:10]] == ["succeeded"] * 10
    eleventh = world.run(runs[10])
    assert (eleventh.status, eleventh.status_reason, eleventh.error_code) == (
        "queued",
        "grant_rate_limited",
        None,
    )
    assert eleventh.cost_micros == 0
    assert world.statuses(runs[10]) == ["queued", "running", "queued"]
    assert grants.limited == 1

    # Una ficha por minuto: la 11.ª sale y la 12.ª vuelve a esperar; luego sale la 12.ª.
    grants.refill()
    world.sleep.release(WAIT)
    await eventually(settled(world, runs[10], "succeeded"))
    await eventually(lambda: world.sleep.pending(WAIT) == 1 and grants.limited == 2)
    assert world.run(runs[11]).status_reason == "grant_rate_limited"
    grants.refill()
    world.sleep.release(WAIT)
    await eventually(lambda: all(settled(world, rid, "succeeded")() for rid in runs))

    assert grants.limited == 2
    assert all("failed" not in world.statuses(rid) for rid in runs)
    assert world.query("SELECT COUNT(*) FROM agent_runs WHERE status = 'failed'") == [(0,)]
    # Las devueltas a la cola no llegaron a invocarse: siguen siendo un primer arranque.
    assert [inv.first_start for inv in world.agent.invocations] == [True] * 12
    # Solo se liberan las concesiones emitidas.
    await eventually(lambda: sorted(rid for rid, _ in grants.releases) == sorted(runs))
    assert world.sleep.delays.count(WAIT) == 2


async def test_renovacion_limitada_queda_en_pausa_y_sigue_desde_su_paso(world: JobWorld) -> None:
    grants = limited(world, count=1)
    failures: list[int] = []

    async def expired(_inv: RunInvocation) -> None:
        if not failures:
            failures.append(1)
            raise SecretError("vault.secret_not_allowed")

    world.agent.hooks[1] = expired
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()

    await eventually(lambda: world.sleep.pending(WAIT) == 1)
    record = world.run(R1)
    assert (record.status, record.status_reason, record.error_code) == (
        "paused",
        "grant_rate_limited",
        None,
    )
    assert grants.releases == [(R1, "paused")]
    assert world.agent.progress[R1] == 1  # el paso 0 quedó hecho

    # Al volver a la cola, la concesión inicial sigue limitada: espera otra vez, en cola.
    world.sleep.release(WAIT)
    await eventually(lambda: grants.limited == 2 and world.sleep.pending(WAIT) == 1)
    assert (world.run(R1).status, world.run(R1).status_reason) == ("queued", "grant_rate_limited")
    grants.refill()
    world.sleep.release(WAIT)
    await eventually(settled(world, R1, "succeeded"))
    assert "failed" not in world.statuses(R1)
    assert [inv.first_start for inv in world.agent.invocations] == [True, False]
    await eventually(lambda: grants.releases == [(R1, "paused"), (R1, "succeeded")])


async def test_la_pausa_corta_la_espera(world: JobWorld) -> None:
    grants = limited(world, count=0)
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: world.sleep.pending(WAIT) == 1)

    world.run_control(paused=True)
    await eventually(lambda: world.sleep.pending(WAIT) == 0)
    grants.refill()
    world.run_control()
    await eventually(settled(world, R1, "succeeded"))
    assert grants.limited == 1


async def test_cancelar_la_tarea_que_espera_corta_la_espera(world: JobWorld) -> None:
    grants = limited(world, count=0)
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: world.sleep.pending(WAIT) == 1)

    # Como `cancelAgentRun`: la tarea está `queued` y se cancela al momento.
    result = await world.jobs.queue.cancel(R1)
    assert result.outcome == "cancelled"
    assert world.jobs.worker.request_cancel(R1) is False  # no estaba en curso
    await eventually(lambda: world.sleep.pending(WAIT) == 0)
    assert world.run(R1).status_reason == "user_cancelled"

    grants.refill()
    await world.add_run(R2, site_id=SITE)
    await eventually(settled(world, R2, "succeeded"))
    assert world.jobs.worker.request_cancel(R1) is False


async def test_el_apagado_corta_la_espera_y_la_tarea_sigue_en_cola(world: JobWorld) -> None:
    limited(world, count=0)
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: world.sleep.pending(WAIT) == 1)

    await world.jobs.shutdown()
    assert world.sleep.pending(WAIT) == 0
    record = world.run(R1)
    assert (record.status, record.status_reason) == ("queued", "grant_rate_limited")


async def test_apagado_al_renovar_no_espera(world: JobWorld) -> None:
    """La renovación llega limitada justo cuando se pide el apagado: no se espera."""

    @dataclass
    class StopThenLimit(FakeGrants):
        async def request(self, **kwargs: Any) -> RunGrant:
            if self.requests:
                self.requests.append(dict(kwargs))
                world.jobs.worker.request_stop()
                raise RunGrantError(AGENTS_GRANT_RATE_LIMITED)
            return await super().request(**kwargs)

    async def expired(_inv: RunInvocation) -> None:
        raise SecretError("vault.secret_not_allowed")

    world.grants = StopThenLimit()
    world.agent.hooks[1] = expired
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()
    await eventually(lambda: world.grants.releases == [(R1, "paused")])
    await world.jobs.shutdown()
    assert WAIT not in world.sleep.delays
    assert world.run(R1).status_reason == "grant_rate_limited"


# --- Hallazgo H1 de la segunda revisión de T13: la cancelación gana ----------------------


@pytest.mark.parametrize("answer", [AGENTS_GRANT_RATE_LIMITED, AGENTS_PAUSED])
@pytest.mark.parametrize("signal", [True, False], ids=["con_aviso", "solo_marca"])
async def test_cancelar_mientras_la_renovacion_se_deniega_no_vuelve_a_la_cola(
    world: JobWorld, answer: str, signal: bool
) -> None:
    """El usuario cancela la tarea en curso y, antes del siguiente límite, la renovación
    de la concesión llega limitada o en pausa: la tarea acaba `cancelled`, no `paused`."""

    @dataclass
    class CancelThenDeny(FakeGrants):
        async def request(self, **kwargs: Any) -> RunGrant:
            if self.requests:
                self.requests.append(dict(kwargs))
                result = await world.jobs.queue.cancel(R1)  # como `cancelAgentRun`
                assert result.outcome == "running"
                if signal:
                    world.jobs.worker.request_cancel(R1)
                raise RunGrantError(answer)
            return await super().request(**kwargs)

    async def expired(_inv: RunInvocation) -> None:
        raise SecretError("vault.secret_not_allowed")

    world.grants = CancelThenDeny()
    world.agent.hooks[1] = expired
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()

    await eventually(settled(world, R1, "cancelled"))
    record = world.run(R1)
    assert (record.status_reason, record.error_code) == ("user_cancelled", None)
    assert world.statuses(R1) == ["queued", "running", "cancelled"]
    await eventually(lambda: world.grants.releases == [(R1, "cancelled")])
    assert WAIT not in world.sleep.delays  # sin espera por el ritmo
    assert len(world.agent.invocations) == 1


@pytest.mark.parametrize("answer", [AGENTS_GRANT_RATE_LIMITED, AGENTS_PAUSED])
async def test_cancelar_entre_comprobar_y_devolver_a_la_cola(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """La concesión inicial se deniega y el usuario cancela justo después de que el
    trabajador comprobó `cancel_requested`: la transición a `queued` lo vuelve a mirar."""
    world.grants.answers = [answer]
    original = Worker._cancel_requested
    late: list[str] = []

    async def check_then_cancel(self: Worker, rid: str) -> bool:
        found = await original(self, rid)
        if not late:
            late.append(rid)
            result = await world.jobs.queue.cancel(rid)
            assert result.outcome == "running"
        return found

    monkeypatch.setattr(Worker, "_cancel_requested", check_then_cancel)
    await world.jobs.start()
    await world.add_run(R1)
    world.run_control()

    await eventually(settled(world, R1, "cancelled"))
    assert late == [R1]
    assert world.run(R1).status_reason == "user_cancelled"
    assert world.statuses(R1) == ["queued", "running", "cancelled"]
    assert WAIT not in world.sleep.delays
    assert world.agent.invocations == []


# --- Hallazgo H2: devuelta a la cola sin invocarse, también tras reiniciar --------------


async def test_tras_reiniciar_sigue_siendo_primer_arranque(world: JobWorld) -> None:
    limited(world, count=0)
    await world.jobs.start()
    await world.add_run(R1, trigger="catch_up", priority=2)
    world.run_control()
    await eventually(lambda: world.sleep.pending(WAIT) == 1)
    record = world.run(R1)
    assert (record.status, record.started_at) == ("queued", None)
    await world.jobs.shutdown()

    # Motor nuevo sobre la misma base, y su máximo ya no cabe en el tope diario.
    spend(world.database, 495_000, limit=500_000)
    world._jobs = None
    world.grants = FakeGrants()
    world.sleep = ManualSleep()
    await world.jobs.start()
    world.run_control()
    await eventually(lambda: world.run(R1).status_reason == "daily_limit")
    assert world.run(R1).status == "queued"
    assert world.grants.requests == []  # se comprobó el tope: no se pidió concesión
    assert world.agent.invocations == []
