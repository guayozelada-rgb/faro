"""`scripts/manual_site_summary_tokens.py` (solo lo ejecuta el usuario con su clave real, T12).

Con un cliente falso, sin red ni claves reales: mide los **mismos** prompts que arma el
agente (comparados con los que recibe el modelo en una ejecución real del grafo con
`FakeLLM`), llama al modelo económico con `classify_content` y al potente con
`write_summary`, avisa si la salida se cortó o si la entrada pasó el límite del estimado,
muestra el costo máximo antes de pedir la clave y nunca imprime la clave, el prompt ni la
respuesta. Que ningún workflow ni script de npm lo ejecute lo comprueba
`tests/test_ci_workflows.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from faro_engine.agents.site_summary.graph import items_block
from faro_engine.agents.site_summary.limits import ITEMS_BLOCK_BYTES, MAX_ITEMS
from faro_engine.core.db.database import Database
from faro_engine.llm.client import RawCompletion, ResolvedCall
from faro_engine.llm.errors import LlmCallError
from tests.conftest import ENGINE_DIR
from tests.evals.site_summary.harness import load_cases, run_case
from tests.fakes.llm import FAKE_KEYS

SCRIPT = ENGINE_DIR / "scripts" / "manual_site_summary_tokens.py"
NONCE_RE = re.compile(r'id="[A-Za-z0-9]{12}"')
CLASSIFICATION = {
    "version": 1,
    "site_kind": "store",
    "topics": ["Clasificacion de prueba"],
    "offerings": ["Oferta de prueba"],
    "audience": "Publico de prueba.",
    "language": "es",
}
SUMMARY = {
    "version": 1,
    "headline": "Titular de prueba",
    "summary": "Resumen de prueba.",
    "offerings": ["Oferta de prueba"],
    "audience": "Publico de prueba.",
    "language": "es",
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manual_site_summary_tokens", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load()


class Client:
    """Responde por `prompt_id`; anota cada llamada y la clave recibida."""

    def __init__(
        self,
        *,
        fail: dict[str, str] | None = None,
        finish: str = "stop",
        tokens: tuple[int, int] = (900, 300),
    ) -> None:
        self.calls: list[ResolvedCall] = []
        self.keys: list[str | None] = []
        self.fail = fail or {}
        self.finish = finish
        self.tokens = tokens

    @property
    def requires_key(self) -> bool:
        return True

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        self.calls.append(call)
        self.keys.append(api_key)
        kind = self.fail.get(call.prompt_id)
        if kind is not None:
            raise LlmCallError(kind)  # type: ignore[arg-type]
        output = CLASSIFICATION if call.prompt_id.endswith("classify_content") else SUMMARY
        return RawCompletion(
            text=json.dumps(output),
            finish_reason=self.finish,
            tokens_in=self.tokens[0],
            tokens_out=self.tokens[1],
        )


def run(argv: list[str], client: Client, key: str, **kwargs: Any) -> int:
    """`main` sin tocar la configuración de logs del proceso de pruebas."""
    kwargs.setdefault("setup_logging", lambda: None)
    return int(script.main(argv, read_key=lambda _p: key, client_factory=lambda: client, **kwargs))


def without_nonces(call: ResolvedCall) -> list[dict[str, str]]:
    return [
        {"role": m["role"], "content": NONCE_RE.sub('id="X"', m["content"])} for m in call.messages
    ]


def test_sin_clave_no_llama_y_muestra_antes_el_costo_maximo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = Client()
    assert run(["anthropic"], client, "  ") == 2
    assert client.calls == []
    out = capsys.readouterr().out
    # Revisión de seguridad de T12, hallazgo 5: el máximo en dólares antes de pedir la clave.
    assert "Como mucho ~US$0,06" in out
    assert "(estimado;" in out
    assert "Ctrl+C para cancelar" in out
    assert "caso tienda-ropa" in out


def test_mide_los_dos_prompts_con_su_modelo_y_sin_imprimir_textos(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = Client()
    key = FAKE_KEYS["anthropic"]
    assert run(["anthropic"], client, key) == 0
    assert [c.model for c in client.calls] == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert [c.prompt_id for c in client.calls] == [
        "site_summary.classify_content",
        "site_summary.write_summary",
    ]
    assert [c.max_output_tokens for c in client.calls] == [800, 1200]
    assert all(c.response_format is not None for c in client.calls)
    assert client.keys == [key, key]
    # La clasificación de la primera llamada entra en la segunda como datos.
    assert "Clasificacion de prueba" in client.calls[1].messages[-1]["content"]
    out = capsys.readouterr().out
    assert key not in out
    assert "Clasificacion de prueba" not in out  # nunca el texto de la respuesta
    assert "Ropa Pequeña" not in out  # ni el del prompt
    assert out.count("entrada 900 tokens · salida 300 de") == 2
    assert "esquema: sí" in out
    assert "AVISO" not in out


def test_si_la_clasificacion_falla_sigue_con_la_del_caso(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = Client(fail={"site_summary.classify_content": "rate_limited"})
    assert run(["openai"], client, FAKE_KEYS["openai"]) == 0
    assert [c.model for c in client.calls] == ["gpt-6-luna", "gpt-6.1-sol"]
    case = next(c for c in load_cases() if c["id"] == "tienda-ropa")
    recorded = case["recorded"]["classify_content"]["output"]["topics"][0]
    assert recorded in client.calls[1].messages[-1]["content"]
    assert "FALLO: rate_limited" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["invalid_key", "insufficient_quota"])
def test_con_la_clave_rechazada_no_hace_la_segunda_llamada(
    capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """Revisión de seguridad de T12, hallazgo 5: no se vuelve a mandar una clave rechazada."""
    client = Client(fail={"site_summary.classify_content": kind})
    key = FAKE_KEYS["anthropic"]
    assert run(["anthropic"], client, key) == 0
    assert [c.prompt_id for c in client.calls] == ["site_summary.classify_content"]
    assert client.keys == [key]
    out = capsys.readouterr().out
    assert f"FALLO: {kind}" in out
    assert "no se hace la segunda" in out
    assert key not in out


@pytest.mark.parametrize("error", [KeyboardInterrupt, EOFError])
def test_cancelar_al_pedir_la_clave_no_llama(
    capsys: pytest.CaptureFixture[str], error: type[BaseException]
) -> None:
    def cancel(_prompt: str) -> str:
        raise error

    client = Client()
    code = script.main(
        ["gemini"], read_key=cancel, client_factory=lambda: client, setup_logging=lambda: None
    )
    assert code == 130
    assert client.calls == []
    assert "Cancelado" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("micros", "text"),
    [(0, "US$0,00"), (1, "US$0,01"), (10_000, "US$0,01"), (117_146, "US$0,12"),
     (2_290_000, "US$2,29"), (9_145_400, "US$9,15")],
)  # fmt: skip
def test_dolares(micros: int, text: str) -> None:
    assert script.dollars(micros) == text


def test_avisa_si_la_salida_se_corta_o_la_entrada_pasa_el_limite(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = Client(finish="length", tokens=(99_999, 1200))
    assert run(["gemini", "--caso", "sitio-en-ingles"], client, FAKE_KEYS["gemini"]) == 0
    out = capsys.readouterr().out
    assert out.count("la salida llegó al máximo (se cortó)") == 2
    assert out.count("la entrada pasó el límite del estimado") == 2
    assert "reserva del motor (car./3)" in out


def test_salida_que_no_cumple_el_esquema_o_sin_tokens() -> None:
    assert script.parse_output("no es json", None) is None
    schema = script.SPEC.prompts.template("site_summary.write_summary", 1).output_schema
    assert script.parse_output('```json\n{"x": 1}\n```', schema) is None
    assert script.parse_output(f"```json\n{json.dumps(SUMMARY)}\n```", schema) == SUMMARY
    measured = script.Measurement(
        model="m", prompt="p", prompt_chars=3, engine_reserve=1, estimate_bound=10,
        max_output_tokens=5,
    )  # fmt: skip
    assert not measured.cut
    assert not measured.over_bound
    assert "costo ? micros" in script.line_for(script._replace(measured, tokens_in=1))


def test_el_caso_al_tope_llena_el_bloque_de_elementos() -> None:
    case = script.load_case(script.FULL_CASE)
    state = script.state_for(case)
    assert len(state.items) == MAX_ITEMS
    request = script.classify_request(state)
    # El bloque se acota por bytes: no caben los 150 y el prompt pasa del tope del bloque.
    assert len(items_block(state).items) < MAX_ITEMS
    assert request.prompt_chars > ITEMS_BLOCK_BYTES
    assert script.worst_cost("anthropic", case) == script.worst_cost("anthropic", {**case})
    assert script.FULL_CASE in script.case_ids()
    assert {c["id"] for c in load_cases()} <= set(script.case_ids())


async def test_los_prompts_son_los_que_arma_el_agente(database: Database) -> None:
    """Mismos mensajes (salvo el nonce, nuevo en cada llamada) que recibe el modelo cuando
    el grafo real ejecuta el caso: la medición es la del agente, no la de una copia."""
    case = next(c for c in load_cases() if c["id"] == "tienda-ropa")
    agent = await run_case(case, database)
    client = Client()
    measured = await script.run(client, "anthropic", case, FAKE_KEYS["anthropic"])
    assert [m.prompt for m in measured] == [c.prompt_id for c in client.calls]
    classify = agent.prompt("site_summary.classify_content")
    assert without_nonces(client.calls[0]) == without_nonces(classify)
    assert client.calls[0].response_format == classify.response_format
    write = agent.prompt("site_summary.write_summary")
    assert client.calls[1].response_format == write.response_format
    # `write_summary` lleva la clasificación del modelo: la de la ejecución grabada es otra.
    assert without_nonces(client.calls[1])[0] == without_nonces(write)[0]  # system


def test_el_script_esta_fuera_del_paquete() -> None:
    assert SCRIPT.parent.name == "scripts"
    assert Path(str(script.__file__)).resolve() == SCRIPT.resolve()
    assert "getpass" in SCRIPT.read_text(encoding="utf-8")
