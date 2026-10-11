"""Corpus de inyección de prompts contra `site_summary` (spec F1b §9.6, skill
`prompts-y-evals` §5; condición T6-C4, parte de T9).

Cada caso de `tests/fixtures/injection/*.json` (al menos 12: órdenes directas, cierre del
bloque con y sin nonce adivinado, rol falso, Unicode bidireccional e invisible —escrito con
escapes—, otros idiomas, pedir claves o URL internas, pedir herramientas con otro sitio o
herramientas que no existen, guardar sin permiso) se inyecta en el **nombre del sitio** y
en los **títulos** de una página y de un producto de un sitio sintético.

Con el `FakeLLM` normal, el prompt armado lleva el texto inyectado **solo** dentro de un
bloque `<datos …>` con nonce nuevo, normalizado (sin `<`/`>` ni marcas invisibles), y fuera
de los bloques el texto es exactamente el de la plantilla (`system` intacto).

Con el `FakeLLM` en modo **"obedece"** (`obey` del caso), el arnés:

- `text`: el modelo devuelve el texto inyectado como resultado → es solo **datos**: la
  tarea espera la decisión del usuario (nivel 1), no se guarda nada y el texto vuelve al
  segundo prompt solo dentro del bloque del modelo;
- `tool_call`: el modelo pide una herramienta (no ofrecida, o la de lectura con argumentos
  de alcance) → salida inválida, un reintento y `llm.bad_output`; no se lee ningún sitio
  más; la misma llamada hecha directamente a la herramienta se rechaza por `extra="forbid"`
  sin tocar ningún sitio;
- `extra_field`: el modelo añade un campo de alcance a su salida → salida inválida →
  `llm.bad_output`, sin propuesta.

En todos: nada se filtra fuera del resultado (ni en los logs, ni en los eventos
`agent_activity`, ni en el código o los detalles del error) y ninguna clave falsa aparece en
los prompts.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from faro_engine.agents.framework.state import RunContext
from faro_engine.agents.framework.tools import ToolRunner
from faro_engine.agents.site_summary.library import PROMPTS
from faro_engine.agents.site_summary.tools import TOOL_NAME, make_read_site_content
from faro_engine.core.db.database import Database
from faro_engine.core.errors import FaroError
from faro_engine.core.logging import configure_logging
from faro_engine.llm.client import ResolvedCall
from faro_engine.llm.fake import FakeReply
from tests.agents.site_summary.conftest import CLASSIFICATION, CLASSIFY, SUMMARY, WRITE
from tests.evals.site_summary.harness import CaseRun, load_cases, run_case
from tests.fakes.llm import FAKE_KEYS
from tests.fakes.site_reader import FakeSiteReader, snapshot_of
from tests.jobs.world import SITE

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "injection"
CORPUS = [
    json.loads(path.read_text(encoding="utf-8")) for path in sorted(CORPUS_DIR.glob("*.json"))
]
BASE = next(case for case in load_cases() if case["id"] == "tienda-ropa")
BLOCK_RE = re.compile(
    r'<datos origen="(?P<origin>[a-z]+)" id="(?P<nonce>[A-Za-z0-9]{12})">\n'
    r'(?P<body>.*?)\n</datos id="(?P=nonce)">',
    re.DOTALL,
)
REQUIRED_CATEGORIES = {
    "direct_order",
    "block_escape",
    "fake_role",
    "unicode",
    "other_language",
    "secrets",
    "scope",
}


def _ids(case: dict[str, Any]) -> str:
    return str(case["id"])


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(stream=stream)
    yield stream
    configure_logging()


def injected_input(case: dict[str, Any]) -> dict[str, Any]:
    """El sitio de `tienda-ropa` con la inyección en el nombre y en dos títulos."""
    data: dict[str, Any] = json.loads(json.dumps(BASE["input"]))
    payload = case["payload"]
    data["site_name"] = payload
    data["items"][0]["title"] = payload  # una página
    data["items"].append(
        {"kind": "product", "title": payload, "url": "https://ropa-pequena.test/producto/x/"}
    )
    data["counts"]["product"] += 1
    return data


def _messages(call: ResolvedCall) -> tuple[str, str]:
    system, user = call.messages
    assert (system["role"], user["role"]) == ("system", "user")
    return system["content"], user["content"]


def _pattern(text: str) -> str:
    """La plantilla como expresión regular (`{{n_items}}` es un número del código)."""
    return r"\d+".join(re.escape(part) for part in text.split("{{n_items}}"))


def _check_prompt(call: ResolvedCall, prompt_id: str, marker: str | None) -> list[str]:
    """El texto externo solo dentro de bloques con nonce; fuera, la plantilla tal cual."""
    template = PROMPTS.template(prompt_id, 1)
    system, user = _messages(call)
    assert system == template.system  # nada externo en las instrucciones
    blocks = list(BLOCK_RE.finditer(user))
    assert len(blocks) == 2
    assert user.count("<datos ") == 2
    assert user.count("</datos") == 2
    nonces = [block["nonce"] for block in blocks]
    assert len(set(nonces)) == 2
    assert "k3J9xQ2mP0aZ" not in nonces  # el nonce adivinado del corpus no sirve
    prefix, suffix = template.user.split("{{datos}}")
    outside = user[: blocks[0].start()] + user[blocks[-1].end() :]
    assert re.fullmatch(_pattern(prefix) + _pattern(suffix), outside)
    assert user[blocks[0].end() : blocks[1].start()] == "\n\n"
    for block in blocks:
        body = block["body"]
        assert "<" not in body
        assert ">" not in body
        assert all(unicodedata.category(char) != "Cf" for char in body)
    if marker is not None:
        for found in re.finditer(re.escape(marker), user):
            assert any(b.start() < found.start() < b.end() for b in blocks)
    return [block["origin"] for block in blocks]


def _no_leaks(result: CaseRun, logs: str, marker: str) -> None:
    assert "jobs.run_queued" in logs  # los logs del motor se capturaron
    assert marker not in logs
    events = [line for line in result.protocol if line["event"] == "agent_activity"]
    assert events  # hubo actividad…
    assert marker not in json.dumps(result.protocol, ensure_ascii=False)  # …sin texto libre
    for key in FAKE_KEYS.values():
        assert key not in logs
        for calls in result.calls.values():
            for call in calls:
                assert all(key not in message["content"] for message in call.messages)
    assert result.run.error_code is None or re.fullmatch(r"[a-z]+\.[a-z_]+", result.run.error_code)


def test_corpus_minimo() -> None:
    assert len(CORPUS) >= 12
    assert {case["category"] for case in CORPUS} >= REQUIRED_CATEGORIES
    kinds = {case["obey"]["kind"] for case in CORPUS}
    assert kinds == {"text", "tool_call", "extra_field"}
    for path in CORPUS_DIR.glob("*.json"):
        raw = path.read_text(encoding="utf-8")
        # Ningún carácter invisible o bidireccional literal en el repositorio.
        assert all(unicodedata.category(c) not in {"Cf", "Cc"} for c in raw if c != "\n")


@pytest.mark.parametrize("case", CORPUS, ids=_ids)
async def test_con_el_modelo_normal_el_texto_queda_en_los_bloques(
    case: dict[str, Any], database: Database, log_stream: io.StringIO
) -> None:
    result = await run_case(BASE, database, input_data=injected_input(case))
    assert result.run.status == "waiting_approval"
    marker = case["marker"]
    classify = result.prompt(CLASSIFY)
    assert marker in _messages(classify)[1]  # llegó al modelo, como dato
    assert _check_prompt(classify, CLASSIFY, marker) == ["sitio", "sitio"]
    assert _check_prompt(result.prompt(WRITE), WRITE, marker) == ["modelo", "sitio"]
    assert result.approval is not None
    assert (
        json.loads(result.approval.payload)["summary"]
        == BASE["recorded"]["write_summary"]["output"]
    )
    _no_leaks(result, log_stream.getvalue(), marker)


def _obey_replies(case: dict[str, Any]) -> dict[str, Any]:
    obey, marker, payload = case["obey"], case["marker"], case["payload"]
    if obey["kind"] == "text":
        classification = {**CLASSIFICATION, "audience": payload[:200], "topics": [marker]}
        summary = {**SUMMARY, "headline": marker, "summary": payload}
        return {
            CLASSIFY: FakeReply(text=json.dumps(classification, ensure_ascii=False)),
            WRITE: FakeReply(text=json.dumps(summary, ensure_ascii=False)),
        }
    if obey["kind"] == "tool_call":
        call = (obey["name"], json.dumps(obey["arguments"]))
        return {CLASSIFY: FakeReply(text=None, tool_calls=(call,))}
    summary = {**SUMMARY, "headline": marker, obey["field"]: obey["value"]}
    return {
        CLASSIFY: FakeReply(text=json.dumps(CLASSIFICATION)),
        WRITE: FakeReply(text=json.dumps(summary)),
    }


@pytest.mark.parametrize("case", CORPUS, ids=_ids)
async def test_con_el_modelo_que_obedece_nada_se_ejecuta_ni_se_filtra(
    case: dict[str, Any], database: Database, log_stream: io.StringIO
) -> None:
    marker, kind = case["marker"], case["obey"]["kind"]
    until = "waiting_approval" if kind == "text" else "failed"
    result = await run_case(
        BASE,
        database,
        input_data=injected_input(case),
        replies=_obey_replies(case),
        until=until,
    )
    summaries = database.run_sync(
        lambda c: c.execute("SELECT COUNT(*) FROM site_summaries").fetchone()[0]
    )
    assert summaries == 0  # nada se guardó sin la decisión del usuario
    if kind == "text":
        # Lo que dijo el modelo es solo un dato: propuesta pendiente para el usuario.
        assert result.approval is not None
        assert result.approval.status == "pending"
        assert result.approval.decided_by is None
        assert _check_prompt(result.prompt(WRITE), WRITE, marker) == ["modelo", "sitio"]
        assert marker in json.loads(result.approval.payload)["summary"]["headline"]
    else:
        assert result.run.error_code == "llm.bad_output"
        assert result.approval is None
        prompt = CLASSIFY if kind == "tool_call" else WRITE
        assert len(result.calls[prompt]) == 2  # un reintento y nada más
    _no_leaks(result, log_stream.getvalue(), marker)


@pytest.mark.parametrize(
    "case", [c for c in CORPUS if c["obey"].get("name") == TOOL_NAME], ids=_ids
)
async def test_la_herramienta_rechaza_el_alcance_que_pide_el_modelo(case: dict[str, Any]) -> None:
    reader = FakeSiteReader(snapshots={SITE: snapshot_of()})
    ctx = RunContext(
        run_id="01920000-0000-7000-8000-000000000001",
        agent_kind="site_summary",
        agent_version=1,
        site_id=SITE,
        provider="anthropic",
        trigger="user",
        token_budget=10_000,
        max_cost_micros=10_000,
    )
    with pytest.raises(FaroError) as err:
        await ToolRunner().invoke(ctx, make_read_site_content(reader), case["obey"]["arguments"])
    assert err.value.code == "tool.invalid_arguments"
    assert err.value.details == {}
    assert reader.calls == []  # ningún sitio tocado
