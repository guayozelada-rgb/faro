"""Reservas del tope diario por clave (skill `capa-llm` §7): bajo candado, dos reservas
concurrentes no pasan el tope y soltar es idempotente."""

from __future__ import annotations

import asyncio

import pytest

from faro_engine.llm.limits import DailyLimiter, DailyLimitReachedError

REF = "llm/" + "openai/default"


def state(spent: int, limit: int, *, delay: float = 0.0) -> object:
    async def read() -> tuple[int, int]:
        if delay:
            await asyncio.sleep(delay)
        return spent, limit

    return read


async def test_reserva_y_suelta() -> None:
    limiter = DailyLimiter()
    first = await limiter.reserve(REF, 300, state(0, 1000))  # type: ignore[arg-type]
    second = await limiter.reserve(REF, 700, state(0, 1000))  # type: ignore[arg-type]
    assert limiter.reserved(REF) == 1000
    with pytest.raises(DailyLimitReachedError):
        await limiter.reserve(REF, 1, state(0, 1000))  # type: ignore[arg-type]
    first.release()
    first.release()  # idempotente
    assert limiter.reserved(REF) == 700
    second.release()
    assert limiter.reserved(REF) == 0


async def test_el_gasto_de_la_base_cuenta() -> None:
    limiter = DailyLimiter()
    with pytest.raises(DailyLimitReachedError):
        await limiter.reserve(REF, 101, state(900, 1000))  # type: ignore[arg-type]
    reservation = await limiter.reserve(REF, 100, state(900, 1000))  # type: ignore[arg-type]
    reservation.release()


async def test_reservas_concurrentes_no_pasan_el_tope() -> None:
    """La lectura de la base es lenta: sin candado, las dos verían 0 reservado."""
    limiter = DailyLimiter()
    results = await asyncio.gather(
        *(limiter.reserve(REF, 600, state(0, 1000, delay=0.01)) for _ in range(5)),  # type: ignore[arg-type]
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    rejected = [r for r in results if isinstance(r, DailyLimitReachedError)]
    assert len(accepted) == 1
    assert len(rejected) == 4
    assert limiter.reserved(REF) == 600


async def test_recheck_con_la_reserva_tomada() -> None:
    limiter = DailyLimiter()
    reservation = await limiter.reserve(REF, 400, state(0, 1000))  # type: ignore[arg-type]
    await reservation.recheck(state(600, 1000))  # type: ignore[arg-type]
    with pytest.raises(DailyLimitReachedError):
        await reservation.recheck(state(601, 1000))  # type: ignore[arg-type]
    reservation.release()
    with pytest.raises(ValueError, match="soltó"):
        await reservation.recheck(state(0, 1000))  # type: ignore[arg-type]


async def test_reserva_negativa_es_un_error() -> None:
    with pytest.raises(ValueError, match="negativa"):
        await DailyLimiter().reserve(REF, -1, state(0, 1))  # type: ignore[arg-type]
