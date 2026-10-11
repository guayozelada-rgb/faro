"""Autonomía (ADR 0016 §2-3, spec F1b §9.2): regla del sitio > general > 1; niveles 0-3;
`publish`/`spend` > 1 rebajado; pausa ⇒ nunca `execute`; la decisión queda en el paso."""

from __future__ import annotations

from dataclasses import replace

import pytest

from faro_engine.agents.framework.actions import ActionCatalog
from faro_engine.agents.framework.autonomy import (
    AutonomyDecision,
    AutonomyPolicy,
    decision_for,
    level_cap,
)
from faro_engine.core.db.database import Database
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.store import autonomy_rules, runs
from faro_engine.core.store.runs import NewRun, NewStep
from tests.fakes.graph_agent import SAVE_ACTION
from tests.jobs.world import REVOKED_SITE, SITE, run_id, set_control

PUBLISH = replace(SAVE_ACTION, action_kind="test_graph.publish", side_effect="publish", max_level=1)
LOW = replace(SAVE_ACTION, action_kind="test_graph.low", max_level=1)
CATALOG = ActionCatalog([SAVE_ACTION, PUBLISH, LOW])


def rule(database: Database, level: int, site_id: str | None = None, n: int = 1) -> None:
    database.run_sync(
        lambda c: autonomy_rules.set_rule(
            c,
            rule_id=run_id(500 + n),
            agent_kind="test_graph",
            site_id=site_id,
            level=level,
            now="2026-10-09T10:00:00Z",
        )
    )


def policy(database: Database, *, paused: bool = False) -> AutonomyPolicy:
    control = AgentsControlState()
    set_control(control, paused=paused, providers=["anthropic"])
    return AutonomyPolicy(database=database, control=control, catalog=CATALOG)


@pytest.mark.parametrize(
    ("level", "expected"), [(0, "suggest"), (1, "propose"), (2, "execute"), (3, "execute")]
)
async def test_niveles(database: Database, level: int, expected: str) -> None:
    rule(database, level)
    decision = await policy(database).decide("test_graph", SITE, "test_graph.save")
    assert decision == AutonomyDecision(expected, level)  # type: ignore[arg-type]


async def test_sin_regla_es_nivel_1_y_la_del_sitio_gana(database: Database) -> None:
    p = policy(database)
    assert await p.decide("test_graph", SITE, "test_graph.save") == AutonomyDecision("propose", 1)
    rule(database, 3)
    rule(database, 0, site_id=SITE, n=2)
    assert (await p.decide("test_graph", SITE, "test_graph.save")).decision == "suggest"
    assert (await p.decide("test_graph", REVOKED_SITE, "test_graph.save")).decision == "execute"
    assert (await p.decide("test_graph", None, "test_graph.save")).decision == "execute"


async def test_publicar_o_gastar_nunca_pasa_de_1(database: Database) -> None:
    rule(database, 3)
    p = policy(database)
    assert await p.decide("test_graph", SITE, "test_graph.publish") == AutonomyDecision(
        "propose", 1
    )
    assert await p.decide("test_graph", SITE, "test_graph.low") == AutonomyDecision("propose", 1)
    assert level_cap(CATALOG, "test_graph.publish") == 1
    assert level_cap(CATALOG, "test_graph.save") == 3
    with pytest.raises(KeyError):
        await p.decide("test_graph", SITE, "test_graph.nope")


@pytest.mark.parametrize("level", [2, 3])
async def test_pausa_nunca_ejecuta(database: Database, level: int) -> None:
    rule(database, level)
    decision = await policy(database, paused=True).decide("test_graph", SITE, "test_graph.save")
    assert decision.decision == "propose"


def test_nivel_2_fuera_de_limites_propone() -> None:
    assert decision_for(2, within_limits=False, can_run=True) == "propose"
    assert decision_for(2, within_limits=True, can_run=True) == "execute"
    assert decision_for(3, within_limits=False, can_run=True) == "execute"


async def test_la_decision_queda_en_el_paso(database: Database) -> None:
    rid = run_id(1)

    def write(conn: object) -> None:
        runs.insert_run(
            conn,  # type: ignore[arg-type]
            NewRun(
                id=rid,
                agent_kind="test_graph",
                agent_version=1,
                objective="test_graph.run",
                trigger="user",
                token_budget=1,
                max_cost_micros=1,
                created_at="2026-10-09T15:00:00Z",
            ),
        )
        runs.insert_step(
            conn,  # type: ignore[arg-type]
            NewStep(
                id="s1",
                run_id=rid,
                node="propose",
                kind="approval",
                idempotency_key="k1",
                started_at="2026-10-09T15:00:00Z",
            ),
        )

    database.run_sync(write)
    await policy(database).record("s1", AutonomyDecision("propose", 1))
    step = database.run_sync(lambda c: runs.get_step(c, "s1"))
    assert step is not None
    assert step.autonomy_decision == "propose"
