"""Emisor de `agent_activity` (ADR 0014 §3): esquema cerrado, `seq` por tarea."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from faro_engine.core import protocol
from faro_engine.core.jobs.activity import (
    MAX_COUNTER,
    ActivityEmitter,
    build_activity_line,
)

RUN_ID = "01920000-0000-7000-8000-0000000000aa"
RUN_2 = "01920000-0000-7000-8000-0000000000ab"
SITE_ID = "01920000-0000-7000-8000-0000000000bb"
MOMENT = datetime(2026, 10, 8, 12, 0, 0, 123456, tzinfo=UTC)


class Sink(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[bytes] = []

    def write(self, line: Any) -> int:
        self.lines.append(bytes(line))
        return len(line)


class BrokenOut(io.BytesIO):
    def write(self, _line: Any) -> int:
        raise BrokenPipeError


def _line(**overrides: Any) -> bytes:
    params: dict[str, Any] = {
        "run_id": RUN_ID,
        "seq": 1,
        "occurred_at": MOMENT,
        "kind": "step_finished",
        "agent": "site_summary",
        "site_id": SITE_ID,
        "status": "running",
        "step": "read_site",
        "step_cost_micros": 0,
        "run_cost_micros": 5678,
        "run_tokens": 4321,
        "error_code": None,
    }
    params.update(overrides)
    return build_activity_line(**params)


def test_line_has_the_closed_schema() -> None:
    line = _line()
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert json.loads(line) == {
        "event": "agent_activity",
        "run_id": RUN_ID,
        "seq": 1,
        "occurred_at": "2026-10-08T12:00:00.123Z",
        "kind": "step_finished",
        "agent": "site_summary",
        "site_id": SITE_ID,
        "status": "running",
        "step": "read_site",
        "step_cost_micros": 0,
        "run_cost_micros": 5678,
        "run_tokens": 4321,
        "error_code": None,
    }
    assert json.loads(_line(site_id=None, step=None, error_code="llm.rate_limited"))[
        "error_code"
    ] == ("llm.rate_limited")


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": "no-uuid"},
        {"kind": "summary"},
        {"site_id": "sitio"},
        {"agent": "Site Summary"},
        {"status": "terminó"},
        {"step": "lee https://ejemplo.com"},
        {"error_code": "Error: sk-ant-123"},
        {"seq": -1},
        {"seq": MAX_COUNTER + 1},
        {"run_tokens": 1.5},
        {"run_cost_micros": True},
    ],
)
def test_free_text_and_bad_values_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"inválido|fuera de rango"):
        _line(**overrides)


async def test_emitter_numbers_events_per_run() -> None:
    sink = Sink()
    emitter = ActivityEmitter(protocol.ProtocolWriter(sink))
    for _ in range(2):
        assert await emitter.emit(run_id=RUN_ID, kind="run_status", agent="a1", status="running")
    assert await emitter.emit(
        run_id=RUN_2, kind="step_started", agent="a1", status="running", occurred_at=MOMENT
    )
    seqs = [(json.loads(line)["run_id"], json.loads(line)["seq"]) for line in sink.lines]
    assert seqs == [(RUN_ID, 1), (RUN_ID, 2), (RUN_2, 1)]
    emitter.forget(RUN_ID)
    await emitter.emit(run_id=RUN_ID, kind="run_status", agent="a1", status="succeeded")
    assert json.loads(sink.lines[-1])["seq"] == 1


async def test_emitter_without_channel_or_with_broken_stdout(log_stream: io.StringIO) -> None:
    assert (
        await ActivityEmitter(None).emit(
            run_id=RUN_ID, kind="run_status", agent="a1", status="running"
        )
        is False
    )
    broken = ActivityEmitter(protocol.ProtocolWriter(BrokenOut()))
    emitted = await broken.emit(run_id=RUN_ID, kind="run_status", agent="a1", status="running")
    assert emitted is False
    assert "agents.activity_dropped" in log_stream.getvalue()


async def test_emitter_usa_el_seq_de_la_base_si_se_lo_dan() -> None:
    sink = Sink()
    emitter = ActivityEmitter(protocol.ProtocolWriter(sink))
    await emitter.emit(run_id=RUN_ID, kind="run_status", agent="a1", status="queued", seq=7)
    assert json.loads(sink.lines[-1])["seq"] == 7
