"""Programador (spec F1b §4.3 y §9.2): próxima ocurrencia diaria y semanal con cambio de
horario en `America/Santiago`, disparos, `catch_up` (una tarea aunque falten varias
ocurrencias) y APScheduler de verdad con `MemoryJobStore`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from faro_engine.core.jobs import scheduler as scheduler_module
from faro_engine.core.jobs.scheduler import (
    Scheduler,
    apscheduler_timer,
    is_valid_timezone,
    next_occurrence,
    parse_utc,
    system_timezone,
)
from faro_engine.core.store import schedules as store
from faro_engine.core.store.common import format_utc
from faro_engine.core.store.schedules import NewSchedule
from tests.jobs.world import NOW, REVOKED_SITE, SITE, JobWorld, eventually, run_id

SANTIAGO = "America/Santiago"
S1 = run_id(501)
S2 = run_id(502)


def occurrence(**over: Any) -> datetime:
    values: dict[str, Any] = {
        "cadence": "daily",
        "time_local": "09:00",
        "weekday": None,
        "timezone_name": SANTIAGO,
        "after": NOW,
    }
    values.update(over)
    return next_occurrence(**values)


def _transitions(zone: ZoneInfo, year: int) -> list[datetime]:
    """Instantes UTC (a la hora) en que cambia el desfase de `zone` ese año."""
    found: list[datetime] = []
    moment = datetime(year, 1, 1, tzinfo=UTC)
    previous = moment.astimezone(zone).utcoffset()
    while moment.year == year:
        moment += timedelta(hours=1)
        offset = moment.astimezone(zone).utcoffset()
        if offset != previous:
            found.append(moment)
            previous = offset
    return found


# --- Próxima ocurrencia ----------------------------------------------------------------


def test_diaria_hoy_si_no_paso_y_manana_si_ya_paso() -> None:
    zone = ZoneInfo(SANTIAGO)
    after = datetime(2026, 10, 9, 8, 0, tzinfo=zone)
    assert occurrence(after=after.astimezone(UTC)).astimezone(zone) == after.replace(hour=9)
    late = after.replace(hour=9)  # justo a la hora: la siguiente es mañana
    nxt = occurrence(after=late.astimezone(UTC)).astimezone(zone)
    assert (nxt.day, nxt.hour, nxt.minute) == (10, 9, 0)


def test_semanal_el_dia_pedido() -> None:
    zone = ZoneInfo(SANTIAGO)
    nxt = occurrence(cadence="weekly", weekday=0, time_local="07:30")  # lunes
    local = nxt.astimezone(zone)
    assert local.weekday() == 0
    assert (local.hour, local.minute) == (7, 30)
    assert nxt > NOW
    assert nxt - NOW <= timedelta(days=7)


@pytest.mark.parametrize("cadence", ["daily", "weekly"])
def test_la_hora_local_se_mantiene_con_el_cambio_de_horario(cadence: str) -> None:
    zone = ZoneInfo(SANTIAGO)
    for change in _transitions(zone, 2026):
        start = change - timedelta(days=3)
        weekday = (change + timedelta(days=1)).astimezone(zone).weekday()
        moment = start
        offsets = set()
        for _ in range(6):
            moment = occurrence(
                cadence=cadence,
                weekday=weekday if cadence == "weekly" else None,
                after=moment,
            )
            local = moment.astimezone(zone)
            assert (local.hour, local.minute) == (9, 0)
            offsets.add(local.utcoffset())
        if cadence == "daily":
            assert len(offsets) == 2  # cruzó el cambio sin moverse de las 9:00


def test_hora_que_no_existe_corre_con_la_hora_nueva_y_la_repetida_una_vez() -> None:
    zone = ZoneInfo(SANTIAGO)
    changes = _transitions(zone, 2026)
    assert len(changes) == 2
    for change in changes:
        before = (change - timedelta(hours=1)).astimezone(zone)
        after = change.astimezone(zone)
        forward = after.utcoffset() > before.utcoffset()  # type: ignore[operator]
        if forward:
            # Salto adelante: la media hora antes del cambio no existe ese día.
            local_minute = after.hour * 60 + after.minute - 30
        else:
            # Salto atrás: esa media hora existe dos veces.
            local_minute = after.hour * 60 + 30
        hour, minute = divmod(local_minute % (24 * 60), 60)
        time_local = f"{hour:02d}:{minute:02d}"
        nxt = occurrence(time_local=time_local, after=change - timedelta(hours=6))
        local = nxt.astimezone(zone)
        if forward:
            assert (local.hour, local.minute) == ((hour + 1) % 24, minute)
        else:
            assert (local.hour, local.minute, local.fold) == (hour, minute, 0)
            assert nxt < change + timedelta(hours=1)  # la primera de las dos
        # Una sola ocurrencia ese día local: la siguiente es al otro día.
        again = occurrence(time_local=time_local, after=nxt)
        assert again - nxt > timedelta(hours=20)


@pytest.mark.parametrize(
    "over",
    [
        {"cadence": "monthly"},
        {"cadence": "weekly", "weekday": None},
        {"cadence": "daily", "weekday": 1},
        {"cadence": "weekly", "weekday": 7},
        {"time_local": "24:00"},
    ],
)
def test_entradas_invalidas(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        occurrence(**over)


def test_zona_del_sistema_y_respaldo(monkeypatch: pytest.MonkeyPatch, log_stream: Any) -> None:
    monkeypatch.setattr(scheduler_module.tzlocal, "get_localzone_name", lambda: SANTIAGO)
    assert system_timezone() == SANTIAGO
    monkeypatch.setattr(scheduler_module.tzlocal, "get_localzone_name", lambda: "No/Existe")
    assert system_timezone() == "UTC"

    def boom() -> str:
        raise OSError("registro ilegible")

    monkeypatch.setattr(scheduler_module.tzlocal, "get_localzone_name", boom)
    assert system_timezone() == "UTC"
    assert "jobs.system_timezone_unknown" in log_stream.getvalue()
    assert is_valid_timezone("UTC")
    assert not is_valid_timezone("../etc")


# --- Disparos con el reloj inyectado --------------------------------------------------------


def add_schedule(
    world: JobWorld, schedule_id: str = S1, *, next_run_at: datetime, **over: Any
) -> None:
    values: dict[str, Any] = {
        "id": schedule_id,
        "agent_kind": "test_agent",
        "site_id": SITE,
        "cadence": "daily",
        "time_local": "09:00",
        "timezone": SANTIAGO,
        "next_run_at": format_utc(next_run_at),
        "created_at": "2026-10-01T12:00:00Z",
    }
    values.update(over)
    assert world.database.run_sync(lambda c: store.insert_schedule(c, NewSchedule(**values)))


def schedule(world: JobWorld, schedule_id: str = S1) -> store.ScheduleRecord:
    record = world.database.run_sync(lambda c: store.get_schedule(c, schedule_id))
    assert record is not None
    return record


async def test_disparo_encola_programada_y_calcula_la_siguiente(world: JobWorld) -> None:
    world.run_control()
    add_schedule(world, next_run_at=NOW - timedelta(minutes=1))
    created = await world.jobs.scheduler.fire(S1)
    assert created is not None
    run = world.run(created)
    assert (run.trigger, run.priority, run.schedule_id, run.status) == ("schedule", 2, S1, "queued")
    record = schedule(world)
    assert record.last_run_id == created
    assert record.last_run_at == format_utc(NOW)
    assert parse_utc(record.next_run_at) > NOW


async def test_disparo_con_pausa_global_crea_la_tarea_igual(world: JobWorld) -> None:
    world.run_control(paused=True)
    add_schedule(world, next_run_at=NOW)
    assert await world.jobs.scheduler.fire(S1) is not None


async def test_disparo_adelantado_desactivado_o_inexistente_no_hace_nada(world: JobWorld) -> None:
    world.run_control()
    add_schedule(world, next_run_at=NOW + timedelta(hours=1))
    assert await world.jobs.scheduler.fire(S1) is None
    add_schedule(world, S2, next_run_at=NOW, site_id=REVOKED_SITE, enabled=False)
    assert await world.jobs.scheduler.fire(S2) is None
    assert await world.jobs.scheduler.fire(run_id(999)) is None
    assert world.query("SELECT COUNT(*) FROM agent_runs") == [(0,)]


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("agent", "agent.unknown"),
        ("site", "agent.site_not_active"),
        ("key", "llm.no_key"),
        ("duplicate", "agent.already_queued"),
    ],
)
async def test_disparo_que_no_puede_crear_la_tarea_avanza_igual(
    world: JobWorld, setup: str, code: str, log_stream: Any
) -> None:
    world.run_control(providers=[] if setup == "key" else None)
    over: dict[str, Any] = {}
    if setup == "agent":
        over["agent_kind"] = "agente_viejo"
    if setup == "site":
        over["site_id"] = REVOKED_SITE
    if setup == "duplicate":
        await world.add_run(run_id(1))
    add_schedule(world, next_run_at=NOW - timedelta(minutes=5), **over)
    assert await world.jobs.scheduler.fire(S1) is None
    record = schedule(world)
    assert record.last_run_id is None
    assert parse_utc(record.next_run_at) > NOW
    assert code in log_stream.getvalue()


async def test_catch_up_una_sola_tarea_aunque_falten_tres(world: JobWorld) -> None:
    world.run_control()
    add_schedule(world, next_run_at=NOW - timedelta(days=3))  # tres ocurrencias perdidas
    add_schedule(world, S2, next_run_at=NOW + timedelta(hours=2), site_id=REVOKED_SITE)
    created = await world.jobs.scheduler.catch_up()
    assert len(created) == 1
    run = world.run(created[0])
    assert run.trigger == "catch_up"
    assert run.notice_pending is True
    assert parse_utc(schedule(world).next_run_at) > NOW
    assert await world.jobs.scheduler.catch_up() == []  # ya no está vencida
    assert world.query("SELECT COUNT(*) FROM agent_runs") == [(1,)]


# --- Trabajos de APScheduler ------------------------------------------------------------


async def test_trabajos_desde_la_tabla_y_al_cambiar(world: JobWorld) -> None:
    add_schedule(world, next_run_at=NOW + timedelta(hours=1))
    add_schedule(world, S2, next_run_at=NOW - timedelta(hours=1), site_id=REVOKED_SITE)
    sched = world.jobs.scheduler
    await sched.refresh(S1)  # sin arrancar: nada
    assert world.timer.jobs == {}
    await sched.start()
    await sched.start()  # dos veces: una sola
    assert sched.running
    assert set(world.timer.jobs) == {S1}  # la vencida la recupera `catch_up`
    assert world.timer.jobs[S1]["trigger"].run_date == NOW + timedelta(hours=1)

    world.database.run_sync(
        lambda c: store.update_schedule(c, S1, {"enabled": False}, now="2026-10-09T15:00:00Z")
    )
    await sched.refresh(S1)
    assert world.timer.jobs == {}
    world.database.run_sync(
        lambda c: store.update_schedule(
            c, S1, {"enabled": True, "timezone": "No/Existe"}, now="2026-10-09T15:00:00Z"
        )
    )
    await sched.refresh(S1)
    assert world.timer.jobs == {}  # zona ilegible: no se programa
    world.database.run_sync(
        lambda c: store.update_schedule(c, S1, {"timezone": SANTIAGO}, now="2026-10-09T15:00:00Z")
    )
    await sched.refresh(S1)
    assert set(world.timer.jobs) == {S1}
    world.database.run_sync(lambda c: store.delete_schedule(c, S1))
    await sched.refresh(S1)
    assert world.timer.jobs == {}
    await sched.refresh(S1)  # ya no estaba
    sched.shutdown()
    sched.shutdown()
    assert world.timer.stopped
    assert not sched.running


async def test_un_disparo_fallido_no_tumba_el_programador(
    world: JobWorld, monkeypatch: pytest.MonkeyPatch, log_stream: Any
) -> None:
    async def boom(_schedule_id: str, *, trigger: str = "schedule") -> str | None:  # noqa: ARG001
        raise RuntimeError("fallo")

    monkeypatch.setattr(world.jobs.scheduler, "fire", boom)
    await world.jobs.scheduler._fire_job(S1)
    assert "jobs.schedule_fire_failed" in log_stream.getvalue()


async def test_apscheduler_de_verdad_dispara_a_su_hora(world: JobWorld) -> None:
    """`AsyncIOScheduler` + `MemoryJobStore`: el trabajo `date` llama a `fire`."""
    world.run_control()
    fired: list[str] = []
    real_now = datetime.now(UTC)
    world.clock.now = real_now
    add_schedule(world, next_run_at=real_now + timedelta(seconds=1))

    sched = Scheduler(world.database, world.jobs.submitter.from_schedule, clock=world.clock,
                      timer_factory=apscheduler_timer)  # fmt: skip
    original = sched.fire

    async def spy(schedule_id: str, *, trigger: str = "schedule") -> str | None:
        world.clock.now = datetime.now(UTC) + timedelta(seconds=1)
        result = await original(schedule_id, trigger=trigger)
        fired.append(schedule_id)
        return result

    sched.fire = spy  # type: ignore[method-assign]
    await sched.start()
    try:
        await eventually(lambda: fired == [S1], timeout=10)
        assert schedule(world).last_run_id is not None
    finally:
        sched.shutdown()
    await asyncio.sleep(0)
