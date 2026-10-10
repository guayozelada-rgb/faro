"""Comprobación MANUAL de los modelos del catálogo con la clave real del usuario (F1b T6).

Solo la ejecuta el usuario, en su equipo, con su clave. Nunca en la CI ni por un agente.

Para qué: `models.json` usa modelos que LiteLLM 1.104.0 no tiene en su mapa
(`claude-haiku-5-5` y `gpt-6.1-sol`). Las pruebas automáticas comprueban la forma de las
peticiones con respuestas grabadas, pero solo una llamada real confirma que el proveedor
acepta lo que envía LiteLLM: el modelo, `max_tokens`/`max_completion_tokens` y la salida
estructurada (`response_format`).

Qué hace, para un proveedor (`anthropic`, `openai` o `gemini`):

1. Carga el **adaptador real del motor** (`faro_engine.llm.litellm_client`), con toda su
   configuración endurecida: limpia el entorno, `api_base` fijo al host oficial, cliente
   propio sin redirecciones ni proxies, almacén de certificados del sistema y caché de
   clientes vaciada tras cada llamada.
2. Muestra el **máximo** que puede costar la prueba (precios de `models.json`) y pide la
   clave con `getpass`: no se muestra, no va en argumentos ni variables de entorno, no se
   lee del llavero, no se escribe en ningún archivo y no se conserva tras salir el proceso
   (Python no permite borrar una `str`; `del` solo suelta la referencia).
3. Hace **una** llamada por modelo (económico y premium, o el nivel de `--tier`) pidiendo
   una salida estructurada mínima.

Solo imprime el modelo, el resultado (`ok` o el tipo de fallo), los tokens informados, el
motivo de fin, si el JSON cumple el esquema y el costo con los precios del catálogo. Nunca
la clave, el texto de la respuesta ni los mensajes de error del proveedor.

Uso, desde `apps/engine`:

    uv run python scripts/manual_llm_models_check.py anthropic
    uv run python scripts/manual_llm_models_check.py openai --tier premium
    uv run python scripts/manual_llm_models_check.py gemini
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from faro_engine.llm.catalog import CatalogModel, default_catalog
from faro_engine.llm.client import (
    PROVIDERS,
    TIERS,
    LlmClient,
    LlmUsage,
    ResolvedCall,
    response_format_for,
)
from faro_engine.llm.errors import LlmCallError
from faro_engine.llm.pricing import call_cost, max_call_cost

# Margen para el razonamiento interno de los modelos que piensan por defecto (los tokens
# de "thinking" cuentan como salida).
MAX_OUTPUT_TOKENS: Final = 1024
PROMPT: Final = (
    "Responde solo con un objeto JSON con dos campos: "
    '"ok" (el valor true) y "idioma" (el código del idioma de esta frase).'
)


class CheckOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    idioma: str


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def models_for(provider: str, tier: str | None) -> list[CatalogModel]:
    catalog = default_catalog()
    tiers = [tier] if tier else list(TIERS)
    return [catalog.model_for(provider, t) for t in tiers]  # type: ignore[arg-type]


def resolved_for(model: CatalogModel) -> ResolvedCall:
    return ResolvedCall(
        provider=model.provider,
        tier=model.tier,
        model=model.model,
        litellm_model=model.litellm_model,
        prompt_id="manual.models_check",
        messages=({"role": "user", "content": PROMPT},),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        response_format=response_format_for(CheckOutput),
    )


def _json_ok(text: str | None) -> bool:
    if text is None:
        return False
    try:
        CheckOutput.model_validate_json(text.strip().removeprefix("```json").strip("`\n "))
    except ValidationError:
        return False
    return True


async def check(client: LlmClient, model: CatalogModel, key: str) -> None:
    today = datetime.now(UTC).date()
    try:
        raw = await client.complete(resolved_for(model), key)
    except LlmCallError as exc:
        _say(f"  [{model.model:24s}] FALLO: {exc.kind}")
        return
    cost = "?"
    if raw.tokens_in is not None and raw.tokens_out is not None:
        usage = LlmUsage(tokens_in=raw.tokens_in, tokens_out=raw.tokens_out)
        cost = f"{call_cost(model, usage, today)} micros"
    _say(
        f"  [{model.model:24s}] ok · tokens {raw.tokens_in} de entrada, {raw.tokens_out} de "
        f"salida · fin {raw.finish_reason} · JSON válido: {'sí' if _json_ok(raw.text) else 'no'}"
        f" · costo {cost}"
    )


async def run_all(client: LlmClient, models: list[CatalogModel], key: str) -> None:
    for model in models:
        await check(client, model, key)


def load_client() -> LlmClient:
    from faro_engine.llm.litellm_client import LiteLlmClient

    return LiteLlmClient()


def main(
    argv: list[str] | None = None,
    *,
    read_key: Callable[[str], str] | None = None,
    client_factory: Callable[[], LlmClient] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Comprobación manual de los modelos (T6).")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--tier", choices=TIERS)
    args = parser.parse_args(argv)
    models = models_for(args.provider, args.tier)
    client = (client_factory or load_client)()
    today = datetime.now(UTC).date()
    worst = sum(max_call_cost(m, len(PROMPT), MAX_OUTPUT_TOKENS, today) for m in models)
    _say(f"{args.provider}: {len(models)} llamada(s); costo máximo {worst} micros de USD")
    key = (read_key or getpass.getpass)(f"Clave de {args.provider} (no se mostrará): ").strip()
    if not key:
        _say("Sin clave: no se hace nada.")
        return 2
    try:
        asyncio.run(run_all(client, models, key))
    finally:
        del key
    _say("Copia estas líneas en el PR de T6 (no contienen la clave).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
