"""`site_summary` con la IA simulada (`--fake-llm`) y los topes diarios (spec F1b §4.1 y
§10, pasos 2 y 12; hallazgo §6.3 del informe de T12).

- Con el tope predeterminado (US$5), el "como mucho" simulado cabe con holgura: el estimado
  no bloquea y `startAgentRun` deja la tarea en cola (antes, con el precio de prueba de T6,
  el máximo era US$9,15 y daba 409 `llm.daily_limit_reached`).
- Con el tope al mínimo (US$0,50) el estimado bloquea, y tras gastar lo suficiente con
  tareas simuladas de verdad, la clave queda "Límite de hoy alcanzado" (paso 12).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from faro_engine.agents.registry import dev_fake_replies
from faro_engine.core.app import create_app
from faro_engine.core.audit import AuditLog
from faro_engine.core.config import Settings
from faro_engine.core.db.database import Database
from faro_engine.core.errors import FaroError
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.store.credentials import DEFAULT_DAILY_LIMIT_MICROS, MIN_DAILY_LIMIT_MICROS
from faro_engine.llm.fake import FAKE_MODEL, dev_fake_llm, fake_catalog
from faro_engine.llm.service import LlmService
from faro_engine.llm.settings import change_daily_limit
from tests.agents.site_summary.conftest import KIND, launch, summary_world
from tests.agents.world import GraphWorld
from tests.fakes.core import Core
from tests.fakes.vault import FakeVault
from tests.jobs.world import SITE, UTC_ZONE

# Máximo garantizado de `site_summary` con el precio de prueba (US$25 y US$125 por Mtok):
# 71 454 tokens de entrada x 25 + 4 000 de salida x 125 (dos llamadas con su reintento).
FAKE_MAX_COST_MICROS = 2_286_350


@pytest.fixture
async def fake_core(
    settings: Settings, database: Database, base_url: str, token: str
) -> AsyncIterator[Core]:
    """El motor como lo arranca el núcleo con `--fake-llm`, con una clave de Anthropic."""
    control = AgentsControlState()
    control.handle_message(
        {"event": "agents_control", "paused": False, "llm_providers": ["anthropic"]}
    )
    app = create_app(
        replace(settings, fake_llm=True), database, audit=AuditLog(database), control=control
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base_url) as http:
        yield Core(http, FakeVault(), token)
    await app.state.jobs.shutdown()


async def _estimate(core: Core) -> dict[str, Any]:
    response = await core.call("estimateAgentRun", body={"agent_kind": KIND, "site_id": SITE})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_con_el_tope_predeterminado_el_maximo_simulado_cabe_y_se_puede_lanzar(
    fake_core: Core,
) -> None:
    body = await _estimate(fake_core)
    assert body["provider"] == "anthropic"
    assert body["models"] == [
        {"tier": "economy", "model": FAKE_MODEL},
        {"tier": "premium", "model": FAKE_MODEL},
    ]
    assert body["daily_limit_micros"] == DEFAULT_DAILY_LIMIT_MICROS
    assert body["max_cost_micros"] == FAKE_MAX_COST_MICROS
    # Con holgura: aunque el máximo del agente crezca, no debe volver a rozar el tope.
    assert body["max_cost_micros"] * 2 <= DEFAULT_DAILY_LIMIT_MICROS
    assert 0 < body["expected_cost_micros"] < body["max_cost_micros"]
    assert body["fits_daily_limit"] is True
    assert body["blocking_code"] is None

    response = await fake_core.call(
        "startAgentRun",
        body={
            "agent_kind": KIND,
            "site_id": SITE,
            "accepted_max_cost_micros": FAKE_MAX_COST_MICROS,
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


async def test_con_el_tope_al_minimo_el_estimado_bloquea(fake_core: Core) -> None:
    response = await fake_core.call(
        "setLlmDailyLimit",
        path={"provider": "anthropic"},
        body={"daily_limit_micros": MIN_DAILY_LIMIT_MICROS},
    )
    assert response.status_code == 200

    body = await _estimate(fake_core)
    assert body["daily_limit_micros"] == MIN_DAILY_LIMIT_MICROS
    assert body["fits_daily_limit"] is False
    assert body["blocking_code"] == "llm.daily_limit_reached"

    response = await fake_core.call(
        "startAgentRun",
        body={
            "agent_kind": KIND,
            "site_id": SITE,
            "accepted_max_cost_micros": FAKE_MAX_COST_MICROS,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "llm.daily_limit_reached"


def _fake_world(database: Database) -> GraphWorld:
    """`site_summary` con el `FakeLLM` y el catálogo de `--fake-llm` (respuestas de
    desarrollo del agente), nivel 3 para que cada tarea termine sin esperar."""
    world = summary_world(database, llm_client=dev_fake_llm(dev_fake_replies()))
    world._llm = LlmService(
        database=world.database,
        control=world.control,
        secrets=world.secrets,
        client=world.llm_client,
        catalog=fake_catalog(),
        clock=world.clock,
        local_tz=UTC_ZONE,
    )
    world.set_level(3, kind=KIND)
    return world


async def test_paso_12_tareas_simuladas_hasta_el_tope_minimo(database: Database) -> None:
    """Paso 12 de la verificación manual: con el tope predeterminado se lanzan tareas
    simuladas hasta que el gasto de hoy pasa de US$0,50; al bajar el tope a ese mínimo, la
    clave queda "Límite de hoy alcanzado" y el estimado bloquea."""
    world = _fake_world(database)
    try:
        costs: list[int] = []
        spent = 0
        while spent < MIN_DAILY_LIMIT_MICROS:
            run = await launch(world)  # comprueba que el estimado no bloquea
            await world.until(run, "succeeded")
            record = world.run(run)
            assert record.max_cost_micros == FAKE_MAX_COST_MICROS
            assert {step.model for step in world.steps(run) if step.kind == "llm_call"} == {
                FAKE_MODEL
            }
            costs.append(record.cost_micros)
            spent, _, _ = await world.llm.daily_state("anthropic")
            assert spent == sum(costs)
        # Costo real de una tarea simulada (sitio de ejemplo de cinco elementos): siempre el
        # mismo, muy por debajo del máximo; hacen falta pocas tareas para pasar del mínimo.
        assert len(set(costs)) == 1
        assert costs[0] * 20 < FAKE_MAX_COST_MICROS
        assert len(costs) == -(-MIN_DAILY_LIMIT_MICROS // costs[0]) <= 15

        await change_daily_limit(
            world.llm, AuditLog(world.database), "anthropic", MIN_DAILY_LIMIT_MICROS
        )
        overview = await world.llm.usage_overview()
        [anthropic] = [item for item in overview.providers if item.provider == "anthropic"]
        assert anthropic.daily_limit_micros == MIN_DAILY_LIMIT_MICROS
        assert anthropic.limit_reached is True  # chip "Límite de hoy alcanzado"
        estimate = await world.jobs.orchestrator.estimate(KIND, SITE)
        assert estimate.blocking_code == "llm.daily_limit_reached"
        with pytest.raises(FaroError) as info:
            await world.jobs.orchestrator.start(KIND, SITE, estimate.max_cost_micros)
        assert info.value.code == "llm.daily_limit_reached"
    finally:
        await world.jobs.shutdown()
