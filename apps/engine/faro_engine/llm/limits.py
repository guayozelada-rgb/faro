"""Tope diario por clave de IA: reservas en memoria + gasto en `credential_usage`.

Spec F1b §4.1 y skill `capa-llm` §7. Antes de llamar, `LlmService` reserva el **máximo** de
la llamada bajo un `asyncio.Lock` por `secret_ref`:

    gastado_hoy (base) + reservado (memoria) + máximo ≤ límite

Así dos llamadas concurrentes no pueden pasar el tope entre las dos. Al registrar el costo
real en la base (`credential_usage`) se suelta la reserva, **después** de escribir: en
medio, el gasto cuenta dos veces (nunca cero). Si un intento sin respuesta obliga a
repetir, `Reservation.recheck` vuelve a comprobar el tope con la reserva aún tomada.

Las reservas no se guardan. Una llamada cancelada (cierre ordenado del motor) cuenta su
máximo como gastado antes de soltar la reserva (`LlmService`); tras un cierre brusco del
proceso a mitad de una llamada, esa llamada no queda contada (riesgo residual, a lo sumo
el máximo de una llamada).

Tope por defecto: US$5/día (5 000 000 micros, decisión del usuario); rango 0,50-500 USD.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# (gastado hoy, límite) leídos de la base para una clave.
DailyState = tuple[int, int]
ReadState = Callable[[], Awaitable[DailyState]]


class DailyLimitReachedError(Exception):
    """La llamada no cabe en el tope diario de la clave (`llm.daily_limit_reached`)."""


@dataclass(slots=True)
class Reservation:
    """Reserva de `amount` micros sobre `secret_ref`. Suéltala siempre (`release`)."""

    limiter: DailyLimiter
    secret_ref: str
    amount: int
    released: bool = False

    async def recheck(self, read_state: ReadState) -> None:
        """Con esta reserva tomada, ¿sigue cabiendo? Si no, `DailyLimitReachedError`."""
        await self.limiter.recheck(self, read_state)

    def release(self) -> None:
        self.limiter.release(self)


class DailyLimiter:
    """Reservas en memoria por clave. Un solo bucle de eventos (el del motor)."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._reserved: dict[str, int] = {}

    def _lock(self, secret_ref: str) -> asyncio.Lock:
        lock = self._locks.get(secret_ref)
        if lock is None:
            lock = self._locks[secret_ref] = asyncio.Lock()
        return lock

    def reserved(self, secret_ref: str) -> int:
        return self._reserved.get(secret_ref, 0)

    async def reserve(self, secret_ref: str, amount: int, read_state: ReadState) -> Reservation:
        if amount < 0:
            raise ValueError("la reserva no puede ser negativa")
        async with self._lock(secret_ref):
            spent, limit = await read_state()
            if spent + self.reserved(secret_ref) + amount > limit:
                raise DailyLimitReachedError
            self._reserved[secret_ref] = self.reserved(secret_ref) + amount
        return Reservation(self, secret_ref, amount)

    async def recheck(self, reservation: Reservation, read_state: ReadState) -> None:
        if reservation.released:
            raise ValueError("la reserva ya se soltó")
        async with self._lock(reservation.secret_ref):
            spent, limit = await read_state()
            if spent + self.reserved(reservation.secret_ref) > limit:
                raise DailyLimitReachedError

    def release(self, reservation: Reservation) -> None:
        if reservation.released:
            return
        reservation.released = True
        left = self.reserved(reservation.secret_ref) - reservation.amount
        if left > 0:
            self._reserved[reservation.secret_ref] = left
        else:
            self._reserved.pop(reservation.secret_ref, None)
