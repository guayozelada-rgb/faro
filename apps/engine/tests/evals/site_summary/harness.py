"""Arnés de las evaluaciones sin red de `site_summary` (skill `prompts-y-evals` §5, spec F1b
§9.6).

Cada caso (`cases/<id>.json`) es un sitio **sintético** con la respuesta grabada del modelo
para cada prompt. `run_case` ejecuta el agente de verdad (grafo, `LlmService`, cola y
trabajador) con `FakeLLM` devolviendo esas respuestas y el lector de sitios simulado con el
contenido del caso, y devuelve los prompts **armados** que recibió el modelo, la propuesta y
la tarea. Sin red, sin claves reales, sin LLM real.

Forma de un caso:

    {"id", "description", "agent": "site_summary",
     "prompts": {"classify_content": 1, "write_summary": 1},
     "input": {"site_name", "woocommerce", "counts": {"page", "post", "product"},
               "items": [{"kind", "title", "url"}]},
     "recorded": {"classify_content": {"output": {…}, "tokens_out": N},
                  "write_summary": {"output": {…}, "tokens_out": N}},
     "checks": {"language", "site_language", "must_mention_any": […], "must_not_contain": […]}}

Los caracteres invisibles de los casos se escriben con escapes `\\u…` en el JSON, nunca
literales en el repositorio.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from faro_engine.agents.framework.budget import PlannedCall
from faro_engine.agents.site_summary.estimate import items_for, planned_calls
from faro_engine.core.db.database import Database
from faro_engine.core.store.approvals import ApprovalRecord
from faro_engine.core.store.runs import RunRecord
from faro_engine.llm.client import ResolvedCall
from faro_engine.llm.fake import FakeStep
from faro_engine.sites.repository import get_site
from tests.agents.site_summary.conftest import CLASSIFY, WRITE, launch, summary_world
from tests.fakes.llm import FakeLLM, FakeReply
from tests.fakes.site_reader import FakeSiteReader, snapshot_from_case
from tests.jobs.world import SITE

CASES_DIR: Final = Path(__file__).with_name("cases")
PROMPT_IDS: Final = {"classify_content": CLASSIFY, "write_summary": WRITE}


def load_cases() -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(CASES_DIR.glob("*.json"))
    ]


@dataclass(frozen=True, slots=True)
class CaseRun:
    case: Mapping[str, Any]
    calls: dict[str, list[ResolvedCall]]  # por `prompt_id`, en orden
    planned: dict[str, PlannedCall]  # lo que reservó el estimado para cada prompt
    run: RunRecord
    approval: ApprovalRecord | None
    n_items: int
    protocol: list[dict[str, Any]]  # líneas de stdout del motor (`agent_activity`, concesiones)

    def prompt(self, prompt_id: str) -> ResolvedCall:
        return self.calls[prompt_id][0]


def set_site(database: Database, data: Mapping[str, Any]) -> None:
    """Conteos y WooCommerce del sitio de pruebas como los del caso (de ahí sale el estimado)."""
    counts = data["counts"]
    woo = bool(data["woocommerce"])

    def write(conn: Any) -> None:
        conn.execute(
            "UPDATE site_connections SET pages_count = ?, posts_count = ?, products_count = ?, "
            "woocommerce_active = ? WHERE site_id = ?",
            (counts["page"], counts["post"], counts["product"] if woo else None, int(woo), SITE),
        )

    database.run_sync(write)


def recorded_replies(case: Mapping[str, Any]) -> dict[str, FakeStep]:
    replies: dict[str, FakeStep] = {}
    for name, prompt_id in PROMPT_IDS.items():
        recorded = case["recorded"][name]
        replies[prompt_id] = FakeReply(
            text=json.dumps(recorded["output"], ensure_ascii=False),
            tokens_out=int(recorded["tokens_out"]),
        )
    return replies


async def run_case(
    case: Mapping[str, Any],
    database: Database,
    *,
    replies: Mapping[str, Any] | None = None,
    until: str = "waiting_approval",
    input_data: Mapping[str, Any] | None = None,
) -> CaseRun:
    data = input_data if input_data is not None else case["input"]
    set_site(database, data)
    site = database.run_sync(lambda c: get_site(c, SITE))
    n_items = items_for(site)
    reader = FakeSiteReader(snapshots={SITE: snapshot_from_case(data)})
    client = FakeLLM(responses=dict(replies) if replies is not None else recorded_replies(case))
    world = summary_world(database, reader=reader, llm_client=client)
    try:
        run_id = await launch(world)
        await world.until(run_id, until)
        record = world.run(run_id)
        approval = world.approval(run_id)
        protocol = list(world.sink.lines)
    finally:
        await world.jobs.shutdown()
    calls: dict[str, list[ResolvedCall]] = {}
    for call in client.calls:
        calls.setdefault(call.prompt_id, []).append(call)
    classify, write = planned_calls(n_items)
    return CaseRun(
        case=case,
        calls=calls,
        planned={CLASSIFY: classify, WRITE: write},
        run=record,
        approval=approval,
        n_items=n_items,
        protocol=protocol,
    )


def prompt_chars(call: ResolvedCall) -> int:
    return sum(len(message["content"]) for message in call.messages)


def prompt_bytes(call: ResolvedCall) -> int:
    return sum(len(message["content"].encode("utf-8")) for message in call.messages)
