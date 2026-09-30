"""Mundo de pruebas de sitios: base cifrada temporal, llavero en memoria y plugin simulado."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from faro_engine.core.audit import AuditLog
from faro_engine.core.db.connection import Connection
from faro_engine.core.db.database import Database, open_profile_database
from faro_engine.core.run_id import current_run_id
from faro_engine.net.client import Deadline
from faro_engine.sites.service import SitesContext, SitesService
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.net import FakeResolver, RecordingSleep, net_settings
from tests.fakes.vault import FakeVault
from tests.fakes.wordpress import FakeWordPress

RUN_ID = "01920000-0000-7000-8000-0000000000aa"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
NOW_TEXT = "2026-09-30T12:00:00Z"


@dataclass
class World:
    database: Database
    wp: FakeWordPress
    vault: FakeVault
    resolver: FakeResolver
    sleep: RecordingSleep
    events: list[str] = field(default_factory=list)
    # Manejador antes del plugin simulado (redirecciones, fallos de red…).
    front: dict[str, httpx.Response | Exception] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.events.append(f"http {request.method} {request.url.path}")
        forced = self.front.get(str(request.url)) or self.front.get(request.url.host)
        if isinstance(forced, Exception):
            raise forced
        if forced is not None:
            return forced
        return self.wp.handler(request)

    def context(self) -> SitesContext:
        return SitesContext(
            database=self.database,
            secrets=self.vault.broker,
            audit=AuditLog(self.database),
            net=net_settings(self.handler, resolver=self.resolver, sleep=self.sleep),
            app_version="0.1.0",
            now=lambda: NOW,
        )

    def service(self, seconds: float = 55) -> SitesService:
        return SitesService(self.context(), Deadline(seconds))

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        def run(conn: Connection) -> list[tuple[Any, ...]]:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]

        return self.database.run_sync(run)

    def audit_actions(self) -> list[tuple[str, str | None, str | None, str]]:
        rows = self.query("SELECT action, secret_ref, run_id, details FROM audit_log ORDER BY id")
        return [(str(a), r, run, str(d)) for a, r, run, d in rows]


@pytest.fixture(autouse=True)
def run_id() -> Iterator[str]:
    """Las operaciones con secretos llevan `X-Faro-Run-Id` (ADR 0010 §3)."""
    token = current_run_id.set(RUN_ID)
    yield RUN_ID
    current_run_id.reset(token)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    assert db.is_ready
    yield db
    db.close()


@pytest.fixture
def world(database: Database) -> World:
    vault = FakeVault()
    world = World(
        database=database,
        wp=FakeWordPress(),
        vault=vault,
        resolver=FakeResolver({"www.tienda.example": ["93.184.216.35"]}),
        sleep=RecordingSleep(),
    )
    original = vault.answer

    def observed(request: dict[str, Any]) -> dict[str, Any]:
        world.events.append(f"secret {request['op']}")
        return original(request)

    vault.answer = observed  # type: ignore[method-assign]
    return world
