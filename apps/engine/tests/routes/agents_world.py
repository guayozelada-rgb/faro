"""App de pruebas para las rutas de agentes y programaciones: base con sitios, el agente
de pruebas, la pausa global, un reloj fijo y la zona de `America/Santiago`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database
from faro_engine.core.jobs.activity import ActivityEmitter
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.runner import AgentCatalog
from faro_engine.core.jobs.runtime import JobSystem
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.service import LlmService
from tests.fakes.agents import FakeGrants, StepAgent
from tests.fakes.core import Core
from tests.fakes.llm import FakeLLM, FakeSecrets
from tests.fakes.vault import FakeVault
from tests.jobs.world import UTC_ZONE, Clock, FakeTimer


@dataclass
class AgentsApp:
    app: FastAPI
    core: Core
    jobs: JobSystem
    control: AgentsControlState
    agent: StepAgent
    database: Database
    clock: Clock
    timer: FakeTimer

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.database.run_sync(
            lambda c: [tuple(r) for r in c.execute(sql, params).fetchall()]
        )


def build_app(settings: Settings, database: Database) -> tuple[FastAPI, AgentsApp]:
    control = AgentsControlState()
    control.handle_message(
        {"event": "agents_control", "paused": False, "llm_providers": ["anthropic", "openai"]}
    )
    agent = StepAgent()
    clock = Clock()
    timer = FakeTimer()
    llm = LlmService(
        database=database,
        control=control,
        secrets=FakeSecrets(),
        client=FakeLLM(),
        catalog=default_catalog(),
        clock=clock,
        local_tz=UTC_ZONE,
    )
    catalog = AgentCatalog([agent])
    jobs = JobSystem(
        database=database,
        control=control,
        grants=FakeGrants(),
        activity=ActivityEmitter(None),
        llm=llm,
        agents=catalog,
        clock=clock,
        timezone_name=lambda: "America/Santiago",
        timer_factory=lambda: timer,
    )
    app = create_app(
        settings,
        database,
        audit=AuditLog(database),
        control=control,
        llm=llm,
        agents=catalog,
        jobs=jobs,
    )
    world = AgentsApp(
        app=app,
        core=None,  # type: ignore[arg-type]
        jobs=jobs,
        control=control,
        agent=agent,
        database=database,
        clock=clock,
        timer=timer,
    )
    return app, world


async def serve(
    settings: Settings, database: Database, base_url: str, token: str
) -> AsyncIterator[AgentsApp]:
    app, world = build_app(settings, database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        world.core = Core(http, FakeVault(), token)
        yield world
    await world.jobs.shutdown()
