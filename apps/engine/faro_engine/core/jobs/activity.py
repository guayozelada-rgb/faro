"""Emisor de la actividad de los agentes en vivo (ADR 0014 §3, spec F1b §4.3 y §5.1).

Cada cambio de estado o de paso de una tarea sale por stdout como una línea del
protocolo, de esquema cerrado y **sin texto libre**:

    {"event":"agent_activity","run_id":"<uuid>","seq":12,"occurred_at":"…Z",
     "kind":"run_status"|"step_started"|"step_finished"|"approval_requested",
     "agent":"site_summary","site_id":"<uuid>"|null,"status":"running","step":"read_site"|null,
     "step_cost_micros":0,"run_cost_micros":5678,"run_tokens":4321,"error_code":null}

- `seq` empieza en 1 por tarea y sube en cada evento: la interfaz detecta un salto (por el
  límite de 20/s del núcleo) y vuelve a pedir el estado con `engine_call`.
- Identificadores con forma `^[a-z][a-z0-9_.]{0,47}$`, UUID canónicos y enteros de 0 a
  10^12: las mismas reglas que valida el núcleo (`src/agents/activity.rs`). Un dato con
  otra forma es un error de programación y lanza `ValueError` sin escribir nada.
- Resúmenes, títulos del sitio y salidas del LLM nunca van aquí: se leen con `getAgentRun`.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from typing import Final, Literal

import anyio.to_thread
import structlog

from faro_engine.core.audit import format_timestamp
from faro_engine.core.protocol import ProtocolWriter
from faro_engine.core.run_id import RUN_ID_PATTERN, is_valid_run_id

log = structlog.get_logger(__name__)

EVENT_AGENT_ACTIVITY: Final = "agent_activity"
ActivityKind = Literal["run_status", "step_started", "step_finished", "approval_requested"]
ACTIVITY_KINDS: Final = frozenset(
    {"run_status", "step_started", "step_finished", "approval_requested"}
)
IDENTIFIER_PATTERN: Final = re.compile(r"[a-z][a-z0-9_.]{0,47}")
MAX_COUNTER: Final = 10**12
MAX_LINE_BYTES: Final = 4096


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} inválido")
    return value


def _counter(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError(f"{field} fuera de rango")
    return value


def build_activity_line(
    *,
    run_id: str,
    seq: int,
    occurred_at: datetime,
    kind: ActivityKind,
    agent: str,
    site_id: str | None,
    status: str,
    step: str | None,
    step_cost_micros: int,
    run_cost_micros: int,
    run_tokens: int,
    error_code: str | None,
) -> bytes:
    """Línea `agent_activity` validada (≤ 4 KB, termina en `\\n`)."""
    if not is_valid_run_id(run_id):
        raise ValueError("run_id inválido")
    if kind not in ACTIVITY_KINDS:
        raise ValueError("kind inválido")
    if site_id is not None and RUN_ID_PATTERN.fullmatch(site_id) is None:
        raise ValueError("site_id inválido")
    fields = {
        "event": EVENT_AGENT_ACTIVITY,
        "run_id": run_id,
        "seq": _counter(seq, "seq"),
        "occurred_at": format_timestamp(occurred_at),
        "kind": kind,
        "agent": _identifier(agent, "agent"),
        "site_id": site_id,
        "status": _identifier(status, "status"),
        "step": None if step is None else _identifier(step, "step"),
        "step_cost_micros": _counter(step_cost_micros, "step_cost_micros"),
        "run_cost_micros": _counter(run_cost_micros, "run_cost_micros"),
        "run_tokens": _counter(run_tokens, "run_tokens"),
        "error_code": None if error_code is None else _identifier(error_code, "error_code"),
    }
    line = json.dumps(fields, separators=(",", ":")).encode("ascii") + b"\n"
    if len(line) > MAX_LINE_BYTES:  # pragma: no cover - imposible con las formas de arriba
        raise ValueError("línea demasiado larga")
    return line


class ActivityEmitter:
    """Escribe `agent_activity` por el `ProtocolWriter` del motor, con `seq` por tarea."""

    def __init__(self, writer: ProtocolWriter | None) -> None:
        self._writer = writer
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def next_seq(self, run_id: str) -> int:
        with self._lock:
            value = self._seq.get(run_id, 0) + 1
            self._seq[run_id] = value
            return value

    def forget(self, run_id: str) -> None:
        """La tarea terminó: se suelta su contador."""
        with self._lock:
            self._seq.pop(run_id, None)

    async def emit(
        self,
        *,
        run_id: str,
        kind: ActivityKind,
        agent: str,
        status: str,
        site_id: str | None = None,
        step: str | None = None,
        step_cost_micros: int = 0,
        run_cost_micros: int = 0,
        run_tokens: int = 0,
        error_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> bool:
        """Emite un evento. `False` si no hay canal (`--dev`) o stdout está cerrado."""
        if self._writer is None:
            return False
        line = build_activity_line(
            run_id=run_id,
            seq=self.next_seq(run_id),
            occurred_at=occurred_at if occurred_at is not None else datetime.now(UTC),
            kind=kind,
            agent=agent,
            site_id=site_id,
            status=status,
            step=step,
            step_cost_micros=step_cost_micros,
            run_cost_micros=run_cost_micros,
            run_tokens=run_tokens,
            error_code=error_code,
        )
        try:
            await anyio.to_thread.run_sync(self._writer.write_line, line)
        except OSError:
            log.warning("agents.activity_dropped", run_id=run_id)
            return False
        return True
