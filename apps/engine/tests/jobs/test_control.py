"""Pausa global en el motor (ADR 0014 §2): pausado hasta recibir `agents_control`."""

from __future__ import annotations

import asyncio
import io
import threading
from typing import Any

import pytest

from faro_engine.core.jobs.control import (
    EVENT_AGENTS_CONTROL,
    AgentsControlState,
    ControlSnapshot,
    _wake,
    parse_control,
)


def message(paused: object = False, providers: object = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "event": EVENT_AGENTS_CONTROL,
        "paused": paused,
        "llm_providers": ["anthropic"] if providers is None else providers,
    }
    data.update(extra)
    return data


def test_without_agents_control_nothing_runs() -> None:
    state = AgentsControlState()
    assert state.snapshot() == ControlSnapshot(received=False, paused=True, llm_providers=())
    assert state.can_run is False


def test_valid_message_enables_and_pauses(log_stream: io.StringIO) -> None:
    state = AgentsControlState()
    data = message(False, ["gemini", "anthropic"])
    assert state.handle_message(data) is True
    assert data == {}, "el mensaje se vacía"
    snapshot = state.snapshot()
    assert snapshot.can_run is True
    assert snapshot.llm_providers == ("anthropic", "gemini"), "orden canónico"
    assert state.handle_message(message(True, [])) is True
    assert state.snapshot() == ControlSnapshot(received=True, paused=True, llm_providers=())
    assert state.can_run is False
    assert "agents.control" in log_stream.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        message(1),
        message("false"),
        message(None),
        message(False, "anthropic"),
        message(False, ["mistral"]),
        message(False, ["anthropic", "anthropic"]),
        message(False, [7]),
        message(False, extra=1),
        {"event": EVENT_AGENTS_CONTROL, "paused": False},
        {**message(False), "event": "otro"},
    ],
)
def test_invalid_message_is_discarded_and_pauses(
    data: dict[str, Any], log_stream: io.StringIO
) -> None:
    assert parse_control(dict(data)) is None
    state = AgentsControlState()
    state.handle_message(message(False, ["openai"]))
    assert state.can_run is True
    assert state.handle_message(data) is False
    snapshot = state.snapshot()
    assert snapshot.paused is True, "falla cerrado"
    assert snapshot.received is True
    assert snapshot.llm_providers == ("openai",), "se conservan los proveedores"
    text = log_stream.getvalue()
    assert "agents.control_invalid" in text
    assert "mistral" not in text


def test_invalid_first_message_keeps_it_unreceived() -> None:
    state = AgentsControlState()
    assert state.handle_message(message("no")) is False
    assert state.snapshot() == ControlSnapshot(received=False, paused=True, llm_providers=())


async def test_wait_until_runnable_returns_at_once_when_running() -> None:
    state = AgentsControlState()
    state.handle_message(message(False))
    snapshot = await asyncio.wait_for(state.wait_until_runnable(), 1)
    assert snapshot.can_run


async def test_wait_until_runnable_waits_for_a_resume_from_another_thread() -> None:
    state = AgentsControlState()
    waiter = asyncio.create_task(state.wait_until_runnable())
    await asyncio.sleep(0)
    assert not waiter.done()
    # Un `agents_control` en pausa despierta al trabajador, que vuelve a esperar.
    thread = threading.Thread(target=state.handle_message, args=(message(True),))
    thread.start()
    thread.join()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()
    thread = threading.Thread(target=state.handle_message, args=(message(False),))
    thread.start()
    thread.join()
    snapshot = await asyncio.wait_for(waiter, 1)
    assert snapshot.can_run


async def test_cancelled_waiter_is_removed() -> None:
    state = AgentsControlState()
    waiter = asyncio.create_task(state.wait_until_runnable())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert state._waiters == []
    # Un mensaje posterior no falla aunque ya no haya nadie esperando.
    assert state.handle_message(message(False)) is True


def test_wake_after_loop_closed_is_ignored() -> None:
    state = AgentsControlState()
    loop = asyncio.new_event_loop()
    future: asyncio.Future[None] = loop.create_future()
    state._waiters.append((loop, future))
    loop.close()
    assert state.handle_message(message(False)) is True


def test_wake_on_finished_future_is_a_no_op() -> None:
    loop = asyncio.new_event_loop()
    try:
        future: asyncio.Future[None] = loop.create_future()
        future.cancel()
        _wake(future)
        assert future.cancelled()
    finally:
        loop.close()
