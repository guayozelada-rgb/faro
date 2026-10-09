"""Auditoría fuera del hilo de stdin (F1a §12.2-3, spec F1b T5 y §9.2).

El hilo `faro-protocol` solo valida y encola; el hilo `faro-audit` inserta. Una inserción
lenta no retrasa `secret_response`, `run_grant_response` ni `agents_control`.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from faro_engine import __main__ as entry
from faro_engine.core import protocol
from faro_engine.core.audit import AuditEvent, AuditLog, AuditWriter
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.grants import RunGrantClient
from faro_engine.core.logging import configure_logging
from faro_engine.core.run_id import use_run_id
from faro_engine.core.secrets import SecretBroker
from tests.db.helpers import TEST_PROFILE_ID, key

RUN_ID = "01920000-0000-7000-8000-0000000000aa"
WAIT = 10.0


class Out(io.BytesIO):
    """stdout del motor: avisa cuando hay dos solicitudes escritas."""

    def __init__(self) -> None:
        super().__init__()
        self.two_lines = threading.Event()

    def write(self, line: Any) -> int:
        written = super().write(line)
        if self.getvalue().count(b"\n") >= 2:
            self.two_lines.set()
        return written


def core_event(n: int = 0) -> dict[str, Any]:
    return {
        "event": "audit",
        "occurred_at": "2026-10-08T12:00:00.000Z",
        "actor": "system",
        "action": "agent.grant_issued",
        "secret_ref": None,
        "run_id": RUN_ID,
        "result": "ok",
        "details": {"agent_kind": "site_summary", "reason": f"n{n}"},
    }


class SlowAudit(AuditLog):
    """Inserción que espera a que la prueba la suelte (simula una base lenta)."""

    def __init__(self) -> None:
        super().__init__(Database.unavailable("db.unavailable"))
        self.release = threading.Event()
        self.started = threading.Event()
        self.inserted: list[AuditEvent] = []

    def insert_sync(self, event: AuditEvent) -> bool:
        self.started.set()
        self.release.wait(WAIT)
        self.inserted.append(event)
        return True


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    assert database.is_ready
    yield database
    database.close()


def test_writer_inserts_in_its_own_thread(db: Database) -> None:
    writer = AuditWriter(AuditLog(db))
    writer.start()
    data = core_event()
    assert writer.record_core_event(data) is True
    assert data == {}, "el evento se vacía en el hilo de stdin"
    assert writer.close() is True
    rows = db.run_sync(lambda conn: conn.execute("SELECT action, run_id FROM audit_log").fetchall())
    assert rows == [("agent.grant_issued", RUN_ID)]


def test_invalid_event_is_rejected_before_queueing(log_stream: io.StringIO) -> None:
    writer = AuditWriter(SlowAudit())
    bad = core_event()
    bad["details"] = {"approval_id": RUN_ID}
    assert writer.record_core_event(bad) is False
    assert writer.pending == 0
    assert "audit.invalid_event" in log_stream.getvalue()


def test_full_queue_and_closed_writer_drop_with_warning(log_stream: io.StringIO) -> None:
    writer = AuditWriter(SlowAudit(), max_pending=1)
    assert writer.record_core_event(core_event(1)) is True
    assert writer.record_core_event(core_event(2)) is False
    # Sin hilo arrancado, cerrar no espera nada.
    assert writer.close() is True
    assert writer.close() is True
    assert writer.record_core_event(core_event(3)) is False
    text = log_stream.getvalue()
    assert "queue_full" in text
    assert "closed" in text


def test_close_gives_up_after_its_deadline(log_stream: io.StringIO) -> None:
    slow = SlowAudit()
    writer = AuditWriter(slow, max_pending=1)
    writer.start()
    writer.record_core_event(core_event(1))
    assert slow.started.wait(WAIT)
    writer.record_core_event(core_event(2))  # llena la cola mientras inserta la primera
    assert writer.close(timeout=0.05) is False
    assert writer.close() is False, "sigue ocupado"
    slow.release.set()
    assert "audit.close_timeout" in log_stream.getvalue()


def test_close_waits_for_the_thread_with_its_deadline(log_stream: io.StringIO) -> None:
    slow = SlowAudit()
    writer = AuditWriter(slow, max_pending=4)
    writer.start()
    writer.record_core_event(core_event(1))
    assert slow.started.wait(WAIT)
    assert writer.close(timeout=0.05) is False
    slow.release.set()
    assert "audit.close_timeout" in log_stream.getvalue()


def _server() -> uvicorn.Server:
    return uvicorn.Server(uvicorn.Config(FastAPI(), log_config=None, lifespan="off"))


async def test_slow_audit_does_not_delay_secret_or_grant_responses() -> None:
    """Con la inserción bloqueada, `secret_response` y `run_grant_response` llegan igual."""
    read_fd, write_fd = os.pipe()
    out = Out()
    writer = protocol.ProtocolWriter(out)
    secrets = SecretBroker(writer)
    grants = RunGrantClient(writer)
    control = AgentsControlState()
    slow = SlowAudit()
    audit = AuditWriter(slow)
    audit.start()
    controller = entry.ShutdownController(_server(), grace=WAIT, force_exit=lambda _c: None)
    reader = protocol.StdinReader(read_fd)
    reader.start()
    thread = threading.Thread(
        target=entry.watch_stdin,
        args=(reader, controller),
        kwargs={"secrets": secrets, "audit": audit, "control": control, "grants": grants},
        daemon=True,
    )
    thread.start()
    try:
        with use_run_id(RUN_ID):
            secret_task = asyncio.create_task(secrets.get("llm/openai/default", max_wait=WAIT))
            grant_task = asyncio.create_task(
                grants.request(
                    run_id=RUN_ID,
                    agent="site_summary",
                    site_id=None,
                    provider="openai",
                    trigger="user",
                )
            )
            assert await asyncio.to_thread(out.two_lines.wait, WAIT)
            requests = [json.loads(line) for line in out.getvalue().splitlines()]
            ids = {r["event"]: r["id"] for r in requests}
            # Primero una auditoría que se queda insertando, después las respuestas.
            os.write(write_fd, json.dumps(core_event()).encode() + b"\n")
            assert await asyncio.to_thread(slow.started.wait, WAIT)
            os.write(
                write_fd,
                b'{"event":"secret_response","id":"'
                + ids["secret_request"].encode()
                + b'","value":"test-llm-ficticia-000"}\n'
                + b'{"event":"run_grant_response","id":"'
                + ids["run_grant_request"].encode()
                + b'","ok":true,"expires_in_seconds":900}\n'
                + b'{"event":"agents_control","paused":false,"llm_providers":["openai"]}\n',
            )
            with await asyncio.wait_for(secret_task, WAIT) as secret:
                assert bytes(secret.buffer) == b"test-llm-ficticia-000"
            grant = await asyncio.wait_for(grant_task, WAIT)
            assert grant.expires_in_seconds == 900
            await asyncio.wait_for(control.wait_until_runnable(), WAIT)
            assert slow.inserted == [], "la inserción sigue bloqueada"
    finally:
        slow.release.set()
        os.write(write_fd, b'{"event":"shutdown"}\n')
        thread.join(WAIT)
        controller.cancel()
        os.close(write_fd)
    assert audit.close() is True
    assert len(slow.inserted) == 1
