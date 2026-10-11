"""Agente de pruebas con LangGraph y una herramienta ofrecida al modelo (spec F1b §8.3, T8).

Nunca se registra en `agents/registry.py` (entraría en `agent-grants.json`): las pruebas lo
ponen en su propio `AgentCatalog`.

Grafo (`test_graph`):

    gather (tool_call) → write (llm_call) → propose (approval) ─┬─ save (control) → fin
                                                                 └─ fin (rechazada o sugerencia)

- `gather`: ofrece al modelo la herramienta de **lectura** `read_notes` (`FakeLLM` responde
  con una llamada); `ToolRunner` valida los argumentos con `NotesIn` (`extra="forbid"`) y la
  herramienta lee las notas **del sitio de la tarea** (del `RunContext`) como
  `UntrustedText`. El texto libre del modelo también se guarda, como dato.
- `write`: las notas y el texto del modelo vuelven al prompt **solo** dentro de bloques de
  datos (`DataBlock`, `source="api"` y `"llm"`); salida estructurada `NotesSummaryV1`.
- `propose` / `save`: los nodos estándar del marco (autonomía, propuesta, ejecutor).
- La acción `test_graph.save` (`internal`) inserta en `site_summaries` en la misma
  transacción que marca la aprobación `executed`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from faro_engine.agents.framework.actions import ActionInput, ActionSpec
from faro_engine.agents.framework.budget import PlannedCall, estimate_calls
from faro_engine.agents.framework.graph import AgentGraph, NodeContext
from faro_engine.agents.framework.prompts import PromptLibrary
from faro_engine.agents.framework.state import AgentState, RunContext, require_site
from faro_engine.agents.framework.tools import Tool, ToolInput, ToolOutput
from faro_engine.agents.framework.untrusted import DataBlock, UntrustedText
from faro_engine.agents.registry import AgentSpec
from faro_engine.core.db.connection import Connection
from faro_engine.core.errors import SITE_REVOKED, site_error
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.runner import AgentEstimate, EstimateInput
from faro_engine.core.operations import SecretGrant
from faro_engine.core.store import site_summaries
from faro_engine.core.store.common import to_json
from faro_engine.llm.fake import FakeReply

KIND: Final = "test_graph"
ACTION: Final = "test_graph.save"
PROMPTS_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "prompts" / KIND
GATHER: Final = "test_graph.gather"
WRITE: Final = "test_graph.write"


# --- Esquemas ---------------------------------------------------------------------------


class NotesSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    headline: str = Field(min_length=1, max_length=120)


class SaveNotesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["test_graph.save"] = "test_graph.save"
    summary: NotesSummaryV1


class NotesGraphState(AgentState):
    notes: list[dict[str, str]] = Field(default_factory=list)
    model_note: dict[str, str] | None = None
    summary: dict[str, Any] | None = None


# --- Herramienta ------------------------------------------------------------------------


class NotesIn(ToolInput):
    limit: int = Field(default=3, ge=1, le=5)


class NotesOut(ToolOutput):
    titles: tuple[UntrustedText, ...] = Field(max_length=5)


@dataclass
class FakeNotes:
    """Notas por sitio (doble del lector de un sitio, sin red). Anota cada lectura."""

    by_site: dict[str, list[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    revoked: bool = False

    async def read(self, ctx: RunContext, args: NotesIn) -> NotesOut:
        site_id = require_site(ctx)  # el alcance sale del contexto, nunca del modelo
        self.calls.append(site_id)
        if self.revoked:
            raise site_error(SITE_REVOKED)
        titles = self.by_site.get(site_id, [])[: args.limit]
        return NotesOut(titles=tuple(UntrustedText("api", title) for title in titles))


def notes_tool(notes: FakeNotes) -> Tool[NotesIn, NotesOut]:
    return Tool(
        name="read_notes",
        description="Lee los títulos de las notas del sitio de esta tarea.",
        input_model=NotesIn,
        output_model=NotesOut,
        effect="read",
        timeout_seconds=5,
        max_calls_per_run=2,
        max_output_chars=2_000,
        fn=notes.read,
    )


# --- Acción -----------------------------------------------------------------------------


def save_notes(conn: Connection, action: ActionInput) -> str | None:
    payload = action.payload
    assert isinstance(payload, SaveNotesPayload)
    site_id = action.ctx.site_id
    assert site_id is not None
    previous = site_summaries.latest_site_summary(conn, site_id)
    site_summaries.insert_site_summary(
        conn,
        summary_id=new_id(),
        site_id=site_id,
        content=to_json(payload.summary.model_dump(mode="json")),
        created_at=action.now,
        run_id=action.ctx.run_id,
        approval_id=action.approval_id,
    )
    return None if previous is None else json.dumps({"summary_id": previous.id})


SAVE_ACTION: Final = ActionSpec(
    action_kind=ACTION,
    side_effect="internal",
    payload_schema=SaveNotesPayload,
    max_level=3,
    run=save_notes,
)


# --- Grafo ------------------------------------------------------------------------------


def build(tool: Tool[NotesIn, NotesOut]) -> Any:
    async def gather(state: NotesGraphState, n: NodeContext) -> dict[str, Any]:
        prompt = n.render(GATHER, 1, instructions_vars={"max_notes": 3})
        result = await n.call_llm(prompt, tools=[tool])
        notes = list(state.notes)
        for _tool, output in await n.run_tool_calls(result, [tool]):
            assert isinstance(output, NotesOut)
            notes.extend(title.to_json() for title in output.titles)
        model_note = None if result.text is None else UntrustedText("llm", result.text).to_json()
        return {"notes": notes, "model_note": model_note}

    async def write(state: NotesGraphState, n: NodeContext) -> dict[str, Any]:
        blocks = [DataBlock("api", [UntrustedText.from_json(note) for note in state.notes], 2_000)]
        if state.model_note is not None:
            blocks.append(DataBlock("llm", [UntrustedText.from_json(state.model_note)], 1_000))
        result = await n.call_llm(n.render(WRITE, 1, data=blocks))
        return {"summary": result.parsed}

    async def propose(state: NotesGraphState, n: NodeContext) -> dict[str, Any]:
        payload = {"kind": ACTION, "summary": state.summary}
        return await n.propose(ACTION, payload, evidence={"reason_key": "test_graph.show"})

    async def save(_state: NotesGraphState, n: NodeContext) -> dict[str, Any]:
        return await n.execute(ACTION)

    def build_graph(g: AgentGraph) -> None:
        g.node("gather", "tool_call", gather)
        g.node("write", "llm_call", write)
        g.node("propose", "approval", propose)
        g.node("save", "control", save)
        g.edge(g.START, "gather")
        g.edge("gather", "write")
        g.edge("write", "propose")
        g.proposal("propose", "save")
        g.edge("save", g.END)

    return build_graph


def estimate(data: EstimateInput) -> AgentEstimate:
    calls = [
        PlannedCall("classify", 300, 60, 3_000, 300),
        PlannedCall("write", 500, 80, 6_000, 400),
    ]
    return estimate_calls(calls, catalog=data.catalog, provider=data.provider, day=data.day)


def result_of(state: NotesGraphState) -> dict[str, Any] | None:
    if state.summary is None:
        return None
    return {"kind": KIND, "summary": state.summary, "outcome": state.outcome}


def make_spec(notes: FakeNotes, **over: Any) -> AgentSpec:
    tool = notes_tool(notes)
    spec = AgentSpec(
        kind=KIND,
        version=1,
        requires_site=True,
        secrets=(
            SecretGrant(ref="llm/anthropic/default", access=("get",)),
            SecretGrant(ref="wp/{site_id}/token", access=("get",)),
        ),
        max_grant_seconds=900,
        state_schema=NotesGraphState,
        build_graph=build(tool),
        estimate_fn=estimate,
        action_specs=(SAVE_ACTION,),
        objectives=("test_graph.run",),
        proposal_node="propose",
        prompts=PromptLibrary(PROMPTS_DIR, agent=KIND, schemas={"NotesSummaryV1": NotesSummaryV1}),
        tools=(tool,),
        result=result_of,
    )
    return replace(spec, **over) if over else spec


def tool_call(arguments: str = '{"limit": 2}', name: str = "read_notes") -> FakeReply:
    return FakeReply(text="Voy a leer las notas.", tool_calls=((name, arguments),))


SUMMARY_REPLY: Final = FakeReply(text='{"version": 1, "headline": "Dos notas de prueba"}')


def replies(**over: Any) -> dict[str, Any]:
    """Respuestas de `FakeLLM` por `prompt_id` para el flujo feliz."""
    base: dict[str, Any] = {GATHER: tool_call(), WRITE: SUMMARY_REPLY}
    base.update(over)
    return base
