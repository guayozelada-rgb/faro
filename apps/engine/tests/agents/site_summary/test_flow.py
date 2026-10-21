"""`site_summary` de punta a punta con `FakeLLM` y el lector de sitios simulado (spec F1b
§9.2, fila T9 de §8.3): niveles 0-3, aprobar, descartar, caducar, sitio revocado, sitio
quitado mientras espera, presupuesto agotado, peor caso de tokens sin pasar el máximo,
reinicio del motor en `interrupt` y las condiciones T8-C1 (propuestas cerradas al fallar)
y T8-C2 (el nodo que propone no gasta)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog

from faro_engine.agents.framework.approvals import ApprovalService, accept_suggestion, decide
from faro_engine.agents.framework.autonomy import AutonomyPolicy
from faro_engine.agents.framework.spec import GraphBuilder
from faro_engine.agents.site_summary import graph as summary_graph
from faro_engine.agents.site_summary.estimate import planned_calls, write_input_bound
from faro_engine.agents.site_summary.spec import SPEC
from faro_engine.agents.site_summary.state import SiteSummaryState
from faro_engine.agents.site_summary.tools import make_read_site_content
from faro_engine.core.audit import AuditLog
from faro_engine.core.db.database import open_profile_database
from faro_engine.core.errors import SITE_TIMEOUT, FaroError, site_error
from faro_engine.core.jobs.control import AgentsControlState
from faro_engine.core.jobs.recovery import expire_approvals
from faro_engine.core.secrets import SecretError
from faro_engine.core.store import settings
from faro_engine.core.store.approvals import Decision
from faro_engine.core.store.runs import NewRun
from faro_engine.llm.client import LlmResult, LlmUsage
from faro_engine.llm.fake import FakeFailure
from faro_engine.llm.pricing import max_call_cost
from faro_engine.sites.snapshot import ContentKind
from tests.agents.site_summary.conftest import (
    ACTION,
    CLASSIFY,
    KIND,
    SUMMARY,
    WRITE,
    launch,
    reader_of,
    replies,
    reply,
    summary_world,
)
from tests.agents.world import GraphWorld
from tests.db.helpers import TEST_PROFILE_ID, key
from tests.fakes.agents import Gate
from tests.fakes.llm import FAKE_KEYS, FakeLLM, FakeReply
from tests.fakes.site_reader import FakeSiteReader, snapshot_of
from tests.jobs.world import SITE, eventually, run_id, seed, spend

PER_KIND = 40
NODES = ["read_site", "classify_content", "write_summary", "propose_save"]
EMPTY_RESULT = LlmResult(
    text=None,
    parsed=None,
    tool_calls=(),
    usage=LlmUsage(tokens_in=0, tokens_out=0),
    provider="anthropic",
    model="fake",
    tier="economy",
    cost_micros=0,
    attempts=1,
)


async def decide_on(world: GraphWorld, run: str, decision: Decision) -> None:
    approval = world.approval(run)
    assert approval is not None
    await decide(world.jobs.queue, AuditLog(world.database), approval.id, decision)


def previous_values(world: GraphWorld) -> list[str | None]:
    return [
        row[0] for row in world.query("SELECT previous_value FROM approvals ORDER BY created_at")
    ]


# --- Niveles de autonomía ---------------------------------------------------------------


async def test_nivel_1_propone_espera_y_al_aprobar_guarda(sworld: GraphWorld) -> None:
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")

    approval = sworld.approval(run)
    assert approval is not None
    assert (approval.status, approval.autonomy_level, approval.decided_by) == ("pending", 1, None)
    assert (approval.action_kind, approval.side_effect) == (ACTION, "internal")
    assert json.loads(approval.payload) == {"kind": ACTION, "summary": SUMMARY}
    assert json.loads(approval.evidence) == {"reason_key": "site_summary.show_on_home"}
    assert [s.node for s in sworld.steps(run)] == NODES
    assert [s.kind for s in sworld.steps(run)] == ["tool_call", "llm_call", "llm_call", "approval"]
    # Lee el sitio de la tarea (del contexto), con los tres tipos y 50 de cada uno.
    assert reader_of(sworld).calls == [(SITE, ("page", "post", "product"), 50)]
    assert sworld.prompt_ids() == [CLASSIFY, WRITE]
    tiers = [call.tier for call in sworld.llm_client.calls]
    assert tiers == ["economy", "premium"]
    assert sworld.summaries(run) == []
    await eventually(lambda: sworld.grants.releases == [(run, "waiting_approval")])
    assert sworld.grants.requests[0]["agent"] == KIND
    assert sworld.grants.requests[0]["site_id"] == SITE

    await decide_on(sworld, run, "approve")
    await sworld.until(run, "succeeded")

    assert sworld.summaries(run) == [SUMMARY]
    after = sworld.approval(run)
    assert after is not None
    assert after.status == "executed"
    assert after.previous_value is None  # no había resumen anterior
    assert sworld.prompt_ids() == [CLASSIFY, WRITE]  # reanudar no vuelve a llamar al modelo
    assert len(reader_of(sworld).calls) == 1  # ni a leer el sitio
    assert [s.node for s in sworld.steps(run)][-2:] == ["propose_save", "save_summary"]
    assert sworld.result(run) == {"kind": KIND, "summary": SUMMARY, "outcome": "executed"}
    assert sworld.checkpoint_count(run) == 1
    record = sworld.run(run)
    assert 0 < record.cost_micros <= record.max_cost_micros
    assert record.tokens <= record.token_budget
    await eventually(lambda: len(sworld.grants.releases) == 2)


async def test_un_segundo_resumen_guarda_el_anterior_para_deshacer(sworld: GraphWorld) -> None:
    sworld.set_level(3, kind=KIND)
    first = await launch(sworld)
    await sworld.until(first, "succeeded")
    first_id = sworld.latest_summary_id()
    second = await launch(sworld)
    await sworld.until(second, "succeeded")
    approval = sworld.approval(second)
    assert approval is not None
    assert approval.previous_value is not None
    assert json.loads(approval.previous_value) == {"summary_id": first_id}
    assert sworld.latest_summary_id() != first_id


async def test_descartar_termina_bien_sin_llm_ni_guardar(sworld: GraphWorld) -> None:
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")
    calls = len(sworld.llm_client.calls)

    await decide_on(sworld, run, "reject")
    await sworld.until(run, "succeeded")

    approval = sworld.approval(run)
    assert approval is not None
    assert approval.status == "rejected"
    assert sworld.summaries(run) == []
    assert len(sworld.llm_client.calls) == calls  # la rama de rechazo no gasta
    assert len(reader_of(sworld).calls) == 1
    result = sworld.result(run)
    assert result is not None
    assert result["outcome"] == "rejected"
    assert result["summary"] == SUMMARY  # el resultado sigue visible
    assert "save_summary" not in [s.node for s in sworld.steps(run)]


@pytest.mark.parametrize("level", [2, 3])
async def test_niveles_2_y_3_guardan_por_regla_sin_esperar(sworld: GraphWorld, level: int) -> None:
    sworld.set_level(level, kind=KIND)
    run = await launch(sworld)
    await sworld.until(run, "succeeded")
    approval = sworld.approval(run)
    assert approval is not None
    assert (approval.status, approval.decided_by, approval.autonomy_level) == (
        "executed",
        "rule",
        level,
    )
    assert sworld.summaries(run) == [SUMMARY]
    assert "approval_requested" not in [line["kind"] for line in sworld.activity(run)]
    decisions = [s.autonomy_decision for s in sworld.steps(run) if s.node == "propose_save"]
    assert decisions == ["execute"]


async def test_regla_del_sitio_gana_a_la_general(sworld: GraphWorld) -> None:
    sworld.set_level(3, kind=KIND)
    sworld.set_level(1, site_id=SITE, kind=KIND)
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")
    assert sworld.summaries(run) == []


async def test_nivel_0_sugiere_y_al_aceptar_guarda(sworld: GraphWorld) -> None:
    sworld.set_level(0, kind=KIND)
    run = await launch(sworld)
    await sworld.until(run, "succeeded")

    assert sworld.approval(run) is None
    assert sworld.summaries(run) == []
    result = sworld.result(run)
    assert result is not None
    assert result["outcome"] == "suggested"
    assert result["suggestion"] == {
        "action_kind": ACTION,
        "payload": {"kind": ACTION, "summary": SUMMARY},
        "evidence": {"reason_key": "site_summary.show_on_home"},
    }

    approval = await accept_suggestion(
        sworld.jobs.queue,
        AuditLog(sworld.database),
        run,
        actions=lambda _kind, action: SPEC.action_spec(action),
    )
    assert (approval.status, approval.decided_by, approval.autonomy_level) == (
        "approved",
        "user",
        0,
    )
    await eventually(lambda: sworld.run_statuses(run).count("succeeded") == 2)
    await sworld.until(run, "succeeded")
    assert sworld.summaries(run) == [SUMMARY]
    assert sworld.prompt_ids() == [CLASSIFY, WRITE]  # aplicar no vuelve a llamar al modelo
    after = sworld.result(run)
    assert after is not None
    assert after["outcome"] == "executed"
    assert "suggestion" not in after


# --- Caducar, sitio y cancelación ------------------------------------------------------------


async def test_caducar_cancela_la_tarea_sin_guardar(sworld: GraphWorld) -> None:
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")
    sworld.clock.now += timedelta(days=15)

    expired = await expire_approvals(sworld.jobs.queue)

    assert [item.run_id for item in expired] == [run]
    record = sworld.run(run)
    assert (record.status, record.error_code) == ("cancelled", "approval.expired")
    approval = sworld.approval(run)
    assert approval is not None
    assert approval.status == "expired"
    assert sworld.summaries(run) == []


@pytest.mark.parametrize(
    "code", ["site.revoked", "site.connection_broken", "site.secret_missing", "site.unreachable"]
)
async def test_sitio_revocado_o_caido_en_read_site(database: Any, code: str) -> None:
    world = summary_world(database, reader=FakeSiteReader(error=code))
    try:
        run = await launch(world)
        await world.until(run, "failed")
        assert world.run(run).error_code == code
        assert world.prompt_ids() == []  # no se llamó al modelo
        steps = world.steps(run)
        assert [(s.node, s.status, s.error_code) for s in steps] == [("read_site", "failed", code)]
        assert world.approval(run) is None
    finally:
        await world.jobs.shutdown()


async def test_sitio_quitado_mientras_esperaba(sworld: GraphWorld) -> None:
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")
    sworld.database.run_sync(
        lambda c: c.execute("UPDATE agent_runs SET site_id = NULL WHERE id = ?", (run,))
    )
    requests = len(sworld.grants.requests)
    await decide_on(sworld, run, "approve")
    await sworld.until(run, "cancelled")
    assert sworld.run(run).error_code == "agent.site_removed"
    assert len(sworld.grants.requests) == requests
    approval = sworld.approval(run)
    assert approval is not None
    assert approval.status == "cancelled"
    assert sworld.summaries(run) == []


@pytest.mark.parametrize("level", [0, 1])
@pytest.mark.parametrize("signal", [True, False], ids=["con_aviso", "solo_marca"])
async def test_cancelar_mientras_propone_no_deja_nada_abierto(
    sworld: GraphWorld, monkeypatch: pytest.MonkeyPatch, level: int, signal: bool
) -> None:
    """Hallazgo M1 de la revisión de T13: una cancelación pedida mientras corre el nodo
    que propone (sin otro límite entre pasos detrás) no se pierde al llegar al `interrupt`
    (nivel 1) ni al final con sugerencia (nivel 0). `solo_marca`: la ruta marcó
    `cancel_requested` pero el aviso al trabajador no llegó a tiempo (la transición lo
    comprueba en la base)."""
    sworld.set_level(level, kind=KIND)
    original = AutonomyPolicy.decide
    answers: list[str] = []

    async def decide_and_cancel(self: AutonomyPolicy, *args: Any, **kwargs: Any) -> Any:
        current = sworld.jobs.worker.current_run_id
        assert current is not None
        result = await sworld.jobs.queue.cancel(current)  # como `cancelAgentRun`
        answers.append(result.outcome)
        if signal:
            sworld.jobs.worker.request_cancel(current)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AutonomyPolicy, "decide", decide_and_cancel)
    run = await launch(sworld)
    await sworld.until(run, "cancelled")

    assert answers == ["running"]  # la ruta contestó `running/cancel_requested`
    record = sworld.run(run)
    assert (record.status_reason, record.error_code) == ("user_cancelled", None)
    assert sworld.run_statuses(run)[-1] == "cancelled"
    assert "waiting_approval" not in sworld.run_statuses(run)
    approval = sworld.approval(run)
    if level == 1:
        assert approval is not None
        assert approval.status == "cancelled"  # no queda `pending` en la Bandeja
    else:
        assert approval is None
        with pytest.raises(FaroError) as caught:
            await accept_suggestion(
                sworld.jobs.queue,
                AuditLog(sworld.database),
                run,
                actions=lambda _kind, action: SPEC.action_spec(action),
            )
        assert caught.value.code == "agent.no_suggestion"
    assert sworld.summaries(run) == []
    await eventually(lambda: sworld.grants.releases == [(run, "cancelled")])


async def test_pausa_a_mitad_y_sigue_desde_su_paso(database: Any) -> None:
    gate = Gate()

    async def before() -> None:
        await gate(None)  # type: ignore[arg-type]

    reader = FakeSiteReader(snapshots={SITE: snapshot_of()}, before=before)
    world = summary_world(database, reader=reader)
    try:
        run = await launch(world)
        await gate.entered.wait()
        world.run_control(paused=True)
        gate.opened.set()
        await world.until(run, "paused")
        assert world.prompt_ids() == []  # `classify_content` no empezó
        world.run_control()
        await world.until(run, "waiting_approval")
        assert world.prompt_ids() == [CLASSIFY, WRITE]
        assert len(reader.calls) == 1  # `read_site` no se repitió
    finally:
        await world.jobs.shutdown()


CHINESE_ITEMS: list[tuple[ContentKind, str, str]] = [
    ("product", "儿童纯棉睡衣套装" * 5, f"/producto/pijama-{i}") for i in range(PER_KIND)
]
CHINESE_ITEMS += [
    ("post", "如何为宝宝选择合适的尺码" * 3, f"/blog/talla-{i}") for i in range(PER_KIND)
]


async def test_reanudada_otro_dia_con_tope_casi_agotado_y_sitio_en_chino(database: Any) -> None:
    """Condición T9-C1: una tarea ya empezada no vuelve a comprobar su máximo completo al
    reanudarse; cada llamada reserva con la cota del estimado (un token por byte), no con
    `ceil(caracteres / 3)`, y da `llm.daily_limit_reached` antes de llamar al modelo."""
    gate = Gate()

    async def before() -> None:
        await gate(None)  # type: ignore[arg-type]

    snapshot = snapshot_of(CHINESE_ITEMS, site_name="宝宝服装店")
    reader = FakeSiteReader(snapshots={SITE: snapshot}, before=before)
    world = summary_world(database, reader=reader)
    try:
        run = await launch(world)
        await gate.entered.wait()
        world.run_control(paused=True)
        gate.opened.set()
        await world.until(run, "paused")
        assert world.run(run).provider == "anthropic"

        # Otro día: casi todo el tope ya gastado por otras tareas.
        world.clock.now += timedelta(days=1)
        day = world.llm.today()
        model = world.llm.catalog.model_for("anthropic", "economy")
        classify = planned_calls(len(CHINESE_ITEMS))[0]
        by_chars = max_call_cost(model, classify.max_prompt_chars, classify.max_output_tokens, day)
        by_bound = max_call_cost(
            model,
            classify.max_prompt_chars,
            classify.max_output_tokens,
            day,
            input_tokens_bound=classify.input_tokens_bound,
        )
        # El prompt real en chino tiene muchos más bytes que `caracteres / 3` tokens: con la
        # reserva de antes la llamada habría salido aunque podía pasar el tope.
        state = SiteSummaryState(
            site_name="宝宝服装店",
            items=[{"kind": k, "title": t, "url": u} for k, t, u in CHINESE_ITEMS],
            counts={"pages": 0, "posts": PER_KIND, "products": PER_KIND},
        )
        items = summary_graph.items_block(state)
        assert SPEC.prompts is not None
        request = SPEC.prompts.render(
            CLASSIFY,
            1,
            instructions_vars={"n_items": len(items.items)},
            data=[summary_graph.site_block(state), items],
        ).request()
        assert request.prompt_bytes > 2 * -(-request.prompt_chars // 3)
        assert (
            max_call_cost(model, request.prompt_chars, request.max_output_tokens, day) <= by_chars
        )
        assert by_chars < by_bound
        limit = 1_000_000
        spend(world.database, limit - by_chars, day=day.isoformat(), limit=limit)

        world.run_control()
        await world.until(run, "failed")
        assert world.run(run).error_code == "llm.daily_limit_reached"
        assert world.prompt_ids() == []  # nunca llegó al modelo
        assert world.secrets.requests == []
        classify_steps = [s for s in world.steps(run) if s.node == "classify_content"]
        assert [(s.status, s.error_code) for s in classify_steps] == [
            ("failed", "llm.daily_limit_reached")
        ]
        [(spent,)] = world.database.run_sync(
            lambda c: c.execute(
                "SELECT cost_micros FROM credential_usage WHERE usage_date = ?",
                (day.isoformat(),),
            ).fetchall()
        )
        assert spent == limit - by_chars
    finally:
        await world.jobs.shutdown()


async def test_interrupt_sobrevive_al_reinicio_del_motor(tmp_path: Path) -> None:
    first_db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    seed(first_db)
    first = summary_world(first_db)
    run = await launch(first)
    await first.until(run, "waiting_approval")
    await first.jobs.shutdown()
    first_db.close()

    second_db = open_profile_database(tmp_path, TEST_PROFILE_ID, key())
    second = summary_world(second_db, control=AgentsControlState(), llm_client=FakeLLM(replies()))
    try:
        assert second.run(run).status == "waiting_approval"
        await second.jobs.start()
        second.run_control()
        await decide_on(second, run, "approve")
        await second.until(run, "succeeded")
        assert second.summaries(run) == [SUMMARY]
        assert second.prompt_ids() == []
        assert reader_of(second).calls == []
    finally:
        await second.jobs.shutdown()
        second_db.close()


# --- Presupuesto y costo -----------------------------------------------------------------


async def test_presupuesto_agotado_a_mitad_sin_pasar_el_maximo(sworld: GraphWorld) -> None:
    await sworld.jobs.start()
    sworld.run_control()
    run = run_id(7)
    # Cabe la llamada económica (con su reintento), no la potente.
    record = await sworld.jobs.queue.enqueue(
        NewRun(
            id=run,
            agent_kind=KIND,
            agent_version=1,
            objective="site_summary.run",
            trigger="user",
            token_budget=200_000,
            max_cost_micros=20_000,
            created_at="2026-10-09T15:00:00Z",
            site_id=SITE,
            priority=0,
            provider="anthropic",
        )
    )
    assert record is not None
    await sworld.until(run, "failed")
    after = sworld.run(run)
    assert after.error_code == "agent.budget_exhausted"
    assert after.cost_micros <= after.max_cost_micros
    assert sworld.prompt_ids() == [CLASSIFY]
    write = [s for s in sworld.steps(run) if s.node == "write_summary"]
    assert [(s.status, s.error_code) for s in write] == [("failed", "agent.budget_exhausted")]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_peor_caso_de_tokens_llega_justo_al_maximo(database: Any, provider: str) -> None:
    """Condición T6-C3: aunque cada intento informe tantos tokens de entrada como el límite
    del estimado (un token por byte, más la sobrecarga de la salida estructurada), toda la
    salida posible y un reintento por salida inválida en cada llamada, la tarea termina sin
    `agent.budget_exhausted` y sin pasar el máximo que aceptó el usuario."""
    classify, write = planned_calls(39)
    classification = {
        "version": 1,
        "site_kind": "store",
        "topics": [],
        "offerings": [],
        "audience": "",
        "language": "es",
    }
    worst = {
        CLASSIFY: [
            FakeReply(text="no es json", tokens_in=classify.max_input_tokens, tokens_out=800),
            reply(classification, tokens_in=classify.max_input_tokens, tokens_out=800),
        ],
        WRITE: [
            FakeReply(text="{}", tokens_in=write.max_input_tokens, tokens_out=1_200),
            reply(SUMMARY, tokens_in=write.max_input_tokens, tokens_out=1_200),
        ],
    }
    world = summary_world(database, llm_client=FakeLLM(responses=worst))
    world.database.run_sync(
        lambda c: settings.set_setting(
            c, "llm.preferred_provider", provider, now="2026-10-09T10:00:00Z"
        )
    )
    try:
        run = await launch(world)
        await world.until(run, "waiting_approval")
        record = world.run(run)
        assert record.provider == provider
        assert record.cost_micros == record.max_cost_micros  # justo el máximo, nunca más
        assert record.tokens == record.token_budget
        assert world.prompt_ids() == [CLASSIFY, CLASSIFY, WRITE, WRITE]
    finally:
        await world.jobs.shutdown()


async def test_error_del_proveedor_falla_con_su_codigo(database: Any) -> None:
    world = summary_world(
        database, llm_client=FakeLLM(replies(**{WRITE: FakeFailure("insufficient_quota")}))
    )
    try:
        run = await launch(world)
        await world.until(run, "failed")
        assert world.run(run).error_code == "llm.insufficient_quota"
        assert world.approval(run) is None
    finally:
        await world.jobs.shutdown()


# --- Condición T8-C1: al fallar, ninguna propuesta queda abierta ----------------------------


async def test_nivel_3_y_fallo_entre_proponer_y_ejecutar_cancela_la_aprobacion(
    sworld: GraphWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: ApprovalService, ctx: Any, action_kind: str) -> Any:
        raise site_error(SITE_TIMEOUT)

    monkeypatch.setattr(ApprovalService, "load_for_execution", broken)
    sworld.set_level(3, kind=KIND)
    run = await launch(sworld)
    await sworld.until(run, "failed")

    assert sworld.run(run).error_code == SITE_TIMEOUT
    approval = sworld.approval(run)
    assert approval is not None
    assert approval.decided_by == "rule"  # la regla la aprobó al proponer…
    assert approval.status == "cancelled"  # …y al fallar la tarea quedó cerrada
    assert approval.executed_at is None
    open_ = sworld.query(
        "SELECT COUNT(*) FROM approvals WHERE run_id = ? AND status IN ('pending', 'approved')",
        (run,),
    )
    assert open_ == [(0,)]
    assert sworld.summaries(run) == []


async def test_aprobada_y_fallo_al_reanudar_cancela_la_aprobacion(
    sworld: GraphWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")

    async def broken(self: ApprovalService, ctx: Any, action_kind: str) -> Any:
        raise site_error(SITE_TIMEOUT)

    monkeypatch.setattr(ApprovalService, "load_for_execution", broken)
    await decide_on(sworld, run, "approve")
    await sworld.until(run, "failed")
    approval = sworld.approval(run)
    assert approval is not None
    assert approval.status == "cancelled"
    assert sworld.summaries(run) == []


async def test_aprobada_y_sin_clave_al_reanudar_cancela_la_aprobacion(sworld: GraphWorld) -> None:
    """La tarea falla desde `queued` (sin clave), antes de invocar el grafo."""
    run = await launch(sworld)
    await sworld.until(run, "waiting_approval")
    sworld.run_control(providers=[])
    await decide_on(sworld, run, "approve")
    await sworld.until(run, "failed")
    assert sworld.run(run).error_code == "llm.no_key"
    approval = sworld.approval(run)
    assert approval is not None
    assert approval.status == "cancelled"


# --- Condición T8-C2: el nodo que propone no gasta -------------------------------------------


def _spec_with_spending_proposal(how: str) -> Any:
    """`site_summary` con un `propose_save` que intenta gastar antes de proponer."""
    original = summary_graph.build_graph

    def build(g: GraphBuilder) -> None:
        tool = make_read_site_content(g.services.sites)  # lector de verdad

        async def spending_propose(state: Any, n: Any) -> dict[str, Any]:
            if how == "llm":
                await n.call_llm(
                    n.render(WRITE, 1, data=[summary_graph.site_block(state)]),
                    input_tokens_bound=write_input_bound(),
                )
            elif how == "tool_calls":
                await n.run_tool_calls(EMPTY_RESULT, [tool])
            else:
                await n.tools.invoke(n.ctx, tool, {})
            return await summary_graph.propose_save(state, n)

        class Wrapped:
            START = g.START
            END = g.END
            services = g.services

            def node(self, name: str, kind: Any, fn: Any) -> None:
                g.node(name, kind, spending_propose if name == "propose_save" else fn)

            def edge(self, source: str, target: str) -> None:
                g.edge(source, target)

            def branch(self, source: str, router: Any, targets: Any) -> None:
                g.branch(source, router, targets)

            def proposal(self, source: str, execute_node: str) -> None:
                g.proposal(source, execute_node)

        original(Wrapped())

    return replace(SPEC, build_graph=build)


@pytest.mark.parametrize("how", ["llm", "tool_calls", "tool"])
async def test_el_nodo_que_propone_no_llama_al_modelo_ni_a_herramientas(
    database: Any, how: str
) -> None:
    world = summary_world(database, spec=_spec_with_spending_proposal(how))
    try:
        run = await launch(world)
        await world.until(run, "failed")
        assert world.run(run).error_code == "internal.unexpected"
        assert world.prompt_ids() == [CLASSIFY, WRITE]  # ninguna llamada desde `propose_save`
        assert len(reader_of(world).calls) == 1
        steps = world.steps(run)
        assert (steps[-1].node, steps[-1].status) == ("propose_save", "failed")
        assert world.approval(run) is None
    finally:
        await world.jobs.shutdown()


async def test_concesion_caducada_al_leer_el_sitio_se_renueva_una_vez(database: Any) -> None:
    """El núcleo deniega el token del sitio (concesión caducada o revocada): el trabajador
    pide una concesión nueva y repite `read_site` desde el último estado guardado."""
    denials = ["vault.secret_not_allowed"]

    async def before() -> None:
        if denials:
            raise SecretError(denials.pop())

    reader = FakeSiteReader(snapshots={SITE: snapshot_of()}, before=before)
    world = summary_world(database, reader=reader)
    try:
        run = await launch(world)
        await world.until(run, "waiting_approval")
        assert len(reader.calls) == 2
        assert len(world.grants.requests) == 2
        steps = world.steps(run)
        assert (steps[0].node, steps[0].status, steps[0].error_code) == (
            "read_site",
            "failed",
            "vault.secret_not_allowed",
        )
        assert world.prompt_ids() == [CLASSIFY, WRITE]
    finally:
        await world.jobs.shutdown()


def _dump_database(world: GraphWorld) -> str:
    """Todas las filas de todas las tablas como texto (los BLOB, decodificados)."""
    names = [
        row[0]
        for row in world.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    parts: list[str] = []
    for name in names:
        for row in world.query(f'SELECT * FROM "{name}"'):  # noqa: S608 - nombres de la base
            for value in row:
                if isinstance(value, bytes):
                    parts.append(value.decode("utf-8", "replace"))
                else:
                    parts.append(str(value))
    return "\n".join(parts)


async def test_la_clave_no_queda_en_la_base_los_checkpoints_la_actividad_ni_los_logs(
    database: Any,
) -> None:
    """Spec F1b §9.2 (T12): con un modelo que pide la clave (como el adaptador real), la
    clave falsa no aparece en ninguna tabla (tampoco en `agent_checkpoints` ni en
    `agent_checkpoint_writes`, también mientras la tarea espera el OK), ni en los eventos
    `agent_activity`, ni en los logs, ni en el resultado."""
    world = summary_world(database, llm_client=FakeLLM(responses=replies(), needs_key=True))
    key = FAKE_KEYS["anthropic"]
    try:
        with structlog.testing.capture_logs() as logs:
            run = await launch(world)
            await world.until(run, "waiting_approval")
            waiting = _dump_database(world)
            await decide_on(world, run, "approve")
            await world.until(run, "succeeded")
    finally:
        await world.jobs.shutdown()
    # No es vacía: la clave sí se pidió y los checkpoints guardan el estado en claro.
    assert [ref for ref, _ in world.secrets.requests] == ["llm/anthropic/default"] * 2
    for secret in world.secrets.values:  # sobrescritas al salir del `with`
        with pytest.raises(ValueError, match="ya se borró"):
            _ = secret.buffer
    assert world.query("SELECT COUNT(*) FROM agent_checkpoints") != [(0,)]
    assert SUMMARY["headline"] in waiting
    finished = _dump_database(world)
    for text in (waiting, finished, repr(logs), json.dumps(world.sink.lines)):
        assert key not in text
    assert key not in json.dumps(world.result(run))
