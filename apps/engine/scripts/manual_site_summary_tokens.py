"""Medición MANUAL de los tokens de `site_summary` con la clave real del usuario (F1b T12).

Solo la ejecuta el usuario, en su equipo, con su clave. Nunca en la CI ni por un agente.

Para qué (informe de T9: decisión 13, condición T6-C3 y §8 "T12"): las evaluaciones sin red
miden los prompts con un tokenizador local y respuestas grabadas. Solo una llamada real dice
cuántos tokens **informa cada proveedor** con los prompts reales del agente y la salida
estructurada (que añade su propio texto), y si el razonamiento interno de los modelos que
piensan llena `max_output_tokens` (800 en `classify_content`, 1 200 en `write_summary`) y
corta la respuesta (en el agente acabaría en `llm.bad_output`).

Qué hace, para un proveedor (`anthropic`, `openai` o `gemini`):

1. Configura los registros con la redacción del motor y los descarta (`quiet_logging`), y
   carga el **adaptador real del motor** (`faro_engine.llm.litellm_client`) con toda su
   configuración endurecida (entorno limpio, `api_base` fijo al host oficial, cliente propio
   sin redirecciones ni proxies, almacén de certificados del sistema, caché vaciada).
2. Arma los **dos prompts reales** del agente (`classify_content.v1` y `write_summary.v1`)
   con las mismas funciones del grafo, a partir de un sitio **sintético** de las
   evaluaciones (`tests/evals/site_summary/cases/<caso>.json`; `tienda-ropa` por defecto) o,
   con `--caso al-tope`, de un sitio inventado con el bloque de elementos lleno (el peor caso
   con texto latino).
3. Muestra el **máximo** que puede costar, en dólares (con Anthropic, "Como mucho
   ~US$0,06 (estimado)…"; `Ctrl+C` cancela sin gastar), y pide la clave con `getpass`: no
   se muestra, no
   va en argumentos ni variables de entorno, no se lee del llavero, no se escribe en ningún
   archivo y no se conserva tras salir el proceso (Python no permite borrar una `str`;
   `del` solo suelta la referencia).
4. Llama al modelo **económico** con `classify_content` y al **potente** con
   `write_summary`, como el agente. La clasificación de la primera llamada entra en la
   segunda como datos; si la primera falla, se usa la del caso, salvo que falle por la
   clave o el saldo (`invalid_key`, `insufficient_quota`): entonces no se hace la segunda.

Por llamada imprime solo números y códigos: modelo, prompt, caracteres del prompt, lo que
reserva el motor (`ceil(caracteres / 3)`), el límite de entrada del estimado del agente,
los tokens de entrada y de salida que informa el proveedor, `max_output_tokens`, el motivo de
fin, si la salida cumple el esquema y el costo con los precios del catálogo, más un aviso si
la salida llegó al máximo (se cortó) o si la entrada pasó el límite del estimado. Nunca la
clave, el prompt, la respuesta ni los mensajes de error del proveedor.

Uso, desde `apps/engine` (en PowerShell igual):

    uv run --native-tls python scripts/manual_site_summary_tokens.py anthropic
    uv run --native-tls python scripts/manual_site_summary_tokens.py openai --caso al-tope
    uv run --native-tls python scripts/manual_site_summary_tokens.py gemini --caso sitio-en-ingles
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from pydantic import BaseModel, ValidationError

from faro_engine.agents.site_summary.estimate import planned_calls
from faro_engine.agents.site_summary.fake import CLASSIFICATION as FALLBACK_CLASSIFICATION
from faro_engine.agents.site_summary.graph import (
    CLASSIFY,
    PROMPT_VERSION,
    WRITE,
    classification_block,
    items_block,
    site_block,
)
from faro_engine.agents.site_summary.limits import KINDS, PER_KIND_LIMIT, TITLE_MAX_CHARS
from faro_engine.agents.site_summary.spec import SPEC
from faro_engine.agents.site_summary.state import SiteSummaryState
from faro_engine.llm.catalog import CatalogModel, default_catalog
from faro_engine.llm.client import (
    PROVIDERS,
    LlmClient,
    LlmRequest,
    LlmUsage,
    Provider,
    ResolvedCall,
    Tier,
)
from faro_engine.llm.errors import LlmCallError
from faro_engine.llm.pricing import call_cost, estimate_input_tokens, max_call_cost
from faro_engine.llm.service import LlmService

ENGINE_DIR: Final = Path(__file__).resolve().parents[1]
CASES_DIR: Final = ENGINE_DIR / "tests" / "evals" / "site_summary" / "cases"
DEFAULT_CASE: Final = "tienda-ropa"
FULL_CASE: Final = "al-tope"
# Fallos de la primera llamada que hacen inútil la segunda (revisión de seguridad de T12,
# hallazgo 5): no se vuelve a mandar una clave que el proveedor ya rechazó.
STOP_ERRORS: Final = frozenset({"invalid_key", "insufficient_quota"})
# Motivos de fin que indican que la respuesta se cortó por `max_output_tokens`.
CUT_REASONS: Final = frozenset({"length", "max_tokens", "MAX_TOKENS"})


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def case_ids() -> list[str]:
    return [*sorted(p.stem for p in CASES_DIR.glob("*.json")), FULL_CASE]


def full_case() -> dict[str, Any]:
    """Sitio inventado con 50 elementos de cada tipo y títulos largos (peor caso latino)."""
    filler = "Producto de prueba con un nombre largo para llenar el bloque de datos "
    items = [
        {
            "kind": kind,
            "title": f"{filler}{index:03d} {filler}"[:TITLE_MAX_CHARS],
            "url": f"https://ejemplo.test/{kind}/{index:03d}-nombre-largo-de-prueba/",
        }
        for index in range(PER_KIND_LIMIT)
        for kind in KINDS
    ]
    return {
        "id": FULL_CASE,
        "input": {
            "site_name": "Tienda de prueba al tope",
            "woocommerce": True,
            "counts": {"page": PER_KIND_LIMIT, "post": PER_KIND_LIMIT, "product": PER_KIND_LIMIT},
            "items": items,
        },
        "recorded": {"classify_content": {"output": FALLBACK_CLASSIFICATION}},
    }


def load_case(case_id: str) -> dict[str, Any]:
    if case_id == FULL_CASE:
        return full_case()
    path = CASES_DIR / f"{case_id}.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def state_for(case: Mapping[str, Any]) -> SiteSummaryState:
    """El estado que deja `read_site` para el sitio del caso."""
    data = case["input"]
    counts = {"pages": data["counts"]["page"], "posts": data["counts"]["post"]}
    if data["woocommerce"]:
        counts["products"] = data["counts"]["product"]
    return SiteSummaryState(
        site_name=data["site_name"],
        items=[dict(item) for item in data["items"]],
        counts=counts,
    )


def classify_request(state: SiteSummaryState) -> LlmRequest:
    """El mismo prompt que arma el nodo `classify_content`."""
    items = items_block(state)
    prompts = SPEC.prompts
    assert prompts is not None  # noqa: S101 - el agente siempre tiene prompts
    return prompts.render(
        CLASSIFY,
        PROMPT_VERSION,
        instructions_vars={"n_items": len(items.items)},
        data=[site_block(state), items],
    ).request()


def write_request(state: SiteSummaryState, classification: Mapping[str, Any]) -> LlmRequest:
    """El mismo prompt que arma el nodo `write_summary`."""
    prompts = SPEC.prompts
    assert prompts is not None  # noqa: S101
    return prompts.render(
        WRITE,
        PROMPT_VERSION,
        instructions_vars={},
        data=[classification_block(classification), site_block(state)],
    ).request()


@dataclass(frozen=True, slots=True)
class Measurement:
    """Lo que se imprime de una llamada (sin textos del prompt ni de la respuesta)."""

    model: str
    prompt: str
    prompt_chars: int
    engine_reserve: int  # `ceil(caracteres / 3)`: lo que reserva `LlmService`
    estimate_bound: int  # entrada que reserva el estimado del agente (`max_input_tokens`)
    max_output_tokens: int
    tokens_in: int | None = None
    tokens_out: int | None = None
    finish_reason: str | None = None
    schema_ok: bool = False
    cost_micros: int | None = None
    error: str | None = None

    @property
    def cut(self) -> bool:
        return self.finish_reason in CUT_REASONS or (
            self.tokens_out is not None and self.tokens_out >= self.max_output_tokens
        )

    @property
    def over_bound(self) -> bool:
        return self.tokens_in is not None and self.tokens_in > self.estimate_bound


def parse_output(text: str | None, schema: type[BaseModel] | None) -> dict[str, Any] | None:
    """Salida validada como la valida `LlmService` (sin vallas de Markdown) o `None`."""
    if text is None or schema is None:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        return schema.model_validate_json(cleaned.strip()).model_dump(mode="json")
    except ValidationError:
        return None


async def measure(
    client: LlmClient,
    model: CatalogModel,
    request: LlmRequest,
    *,
    estimate_bound: int,
    key: str,
) -> tuple[Measurement, dict[str, Any] | None]:
    base = Measurement(
        model=model.model,
        prompt=request.prompt_id,
        prompt_chars=request.prompt_chars,
        engine_reserve=estimate_input_tokens(request.prompt_chars),
        estimate_bound=estimate_bound,
        max_output_tokens=request.max_output_tokens,
    )
    call: ResolvedCall = LlmService._resolve(model.provider, model, request)
    try:
        raw = await client.complete(call, key)
    except LlmCallError as exc:
        return _replace(base, error=exc.kind), None
    parsed = parse_output(raw.text, request.output_schema)
    cost = None
    if raw.tokens_in is not None and raw.tokens_out is not None:
        usage = LlmUsage(tokens_in=raw.tokens_in, tokens_out=raw.tokens_out)
        cost = call_cost(model, usage, datetime.now(UTC).date())
    measured = _replace(
        base,
        tokens_in=raw.tokens_in,
        tokens_out=raw.tokens_out,
        finish_reason=raw.finish_reason,
        schema_ok=parsed is not None,
        cost_micros=cost,
    )
    return measured, parsed


def _replace(base: Measurement, **changes: Any) -> Measurement:
    values = {name: getattr(base, name) for name in Measurement.__slots__}
    values.update(changes)
    return Measurement(**values)


def line_for(m: Measurement) -> str:
    head = (
        f"  [{m.model}] {m.prompt}: prompt {m.prompt_chars} caracteres · reserva del motor "
        f"{m.engine_reserve} · límite del estimado {m.estimate_bound}"
    )
    if m.error is not None:
        return f"{head}\n      FALLO: {m.error}"
    body = (
        f"      entrada {m.tokens_in} tokens · salida {m.tokens_out} de {m.max_output_tokens} · "
        f"fin {m.finish_reason} · esquema: {'sí' if m.schema_ok else 'no'} · "
        f"costo {m.cost_micros if m.cost_micros is not None else '?'} micros"
    )
    notes = []
    if m.cut:
        notes.append("AVISO: la salida llegó al máximo (se cortó)")
    if m.over_bound:
        notes.append("AVISO: la entrada pasó el límite del estimado")
    if m.tokens_in is not None and m.tokens_in > m.engine_reserve:
        notes.append("nota: la entrada pasó la reserva del motor (car./3)")
    return "\n".join([head, body, *(f"      {note}" for note in notes)])


async def run(
    client: LlmClient, provider: str, case: Mapping[str, Any], key: str
) -> list[Measurement]:
    catalog = default_catalog()
    state = state_for(case)
    classify_plan, write_plan = planned_calls(len(state.items))
    economy = catalog.model_for(cast("Provider", provider), "economy")
    premium = catalog.model_for(cast("Provider", provider), "premium")
    first, classification = await measure(
        client,
        economy,
        classify_request(state),
        estimate_bound=classify_plan.max_input_tokens,
        key=key,
    )
    if first.error in STOP_ERRORS:  # clave mala o sin saldo: la segunda fallaría igual
        return [first]
    if classification is None:
        classification = dict(case["recorded"]["classify_content"]["output"])
    second, _ = await measure(
        client,
        premium,
        write_request(state, classification),
        estimate_bound=write_plan.max_input_tokens,
        key=key,
    )
    return [first, second]


def worst_cost(provider: str, case: Mapping[str, Any]) -> int:
    """Lo más que pueden costar las dos llamadas: la entrada con el límite que reserva el
    estimado del agente (un token por byte, más la salida estructurada) y la salida máxima
    de cada prompt."""
    catalog = default_catalog()
    today = datetime.now(UTC).date()
    plans = planned_calls(len(state_for(case).items))
    tiers: tuple[Tier, Tier] = ("economy", "premium")
    total = 0
    for tier, plan in zip(tiers, plans, strict=True):
        model = catalog.model_for(cast("Provider", provider), tier)
        # `max_call_cost` recibe caracteres y reserva `ceil(caracteres / 3)` tokens de entrada.
        total += max_call_cost(model, plan.max_input_tokens * 3, plan.max_output_tokens, today)
    return total


def dollars(micros: int) -> str:
    """Micros de USD en dólares con coma decimal, redondeando hacia arriba al centavo."""
    cents = -(-micros // 10_000)
    return f"US${cents // 100},{cents % 100:02d}"


def quiet_logging() -> None:
    """Registros del motor (con su redacción) y de LiteLLM a un sumidero que no imprime."""
    from faro_engine.core.logging import configure_logging

    sink = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115 - vive lo que el proceso
    configure_logging(level=logging.CRITICAL, stream=sink)


def load_client() -> LlmClient:
    from faro_engine.llm.litellm_client import LiteLlmClient

    return LiteLlmClient()


def main(
    argv: list[str] | None = None,
    *,
    read_key: Callable[[str], str] | None = None,
    client_factory: Callable[[], LlmClient] | None = None,
    setup_logging: Callable[[], None] = quiet_logging,
) -> int:
    parser = argparse.ArgumentParser(description="Medición manual de tokens de site_summary.")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--caso", default=DEFAULT_CASE, choices=case_ids())
    args = parser.parse_args(argv)
    case = load_case(args.caso)
    setup_logging()  # antes de cargar el adaptador (como manual_llm_models_check.py)
    client = (client_factory or load_client)()
    worst = worst_cost(args.provider, case)
    _say(f"{args.provider}, caso {args.caso}: 2 llamadas al modelo.")
    _say(
        f"Como mucho ~{dollars(worst)} (estimado; {worst} micros de USD). "
        "Escribe la clave para seguir o Ctrl+C para cancelar."
    )
    try:
        key = (read_key or getpass.getpass)(f"Clave de {args.provider} (no se mostrará): ").strip()
    except (KeyboardInterrupt, EOFError):
        _say("Cancelado: no se hace nada.")
        return 130
    if not key:
        _say("Sin clave: no se hace nada.")
        return 2
    try:
        measurements = asyncio.run(run(client, args.provider, case, key))
    finally:
        del key
    for measurement in measurements:
        _say(line_for(measurement))
    if len(measurements) == 1:
        _say(
            "La primera llamada falló por la clave o el saldo: no se hace la segunda. "
            "Revisa la clave en el panel del proveedor."
        )
    _say("Copia estas líneas en la lista de verificación manual (no contienen la clave).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
