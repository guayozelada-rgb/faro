"""`FakeLLM`: el `LlmClient` del modo `--fake-llm` y la base de `tests/fakes/llm.py`.

Spec F1b §4.1 y skill `capa-llm` §10. Responde por `prompt_id` con respuestas fijas, cuenta
tokens de forma determinista (`count_tokens`: caracteres / 4, hacia arriba, siempre por
debajo del máximo que reserva el servicio) y puede simular cada fallo del proveedor
(`FakeFailure`), salidas inválidas, llamadas a herramientas y respuestas que "obedecen" a
una inyección (texto libre). Nunca abre conexiones.

En el modo `--fake-llm` (solo desarrollo, doble llave en el núcleo, rechazado con
`sys.frozen`):

- `requires_key = False`: el servicio no pide ninguna clave por `secret_request` (la
  concesión de la ejecución sí se pide igual, la pide el trabajador);
- el catálogo es `fake_catalog()`: el modelo es `"fake"` en los dos niveles de los tres
  proveedores, con un **precio de prueba alto** (`FAKE_PRICES`) para llegar al tope
  diario con pocas tareas;
- las respuestas salen de `DEV_RESPONSES` (vacío en T6; cada agente añade las suyas, p. ej.
  `site_summary` en T9). Un `prompt_id` sin respuesta devuelve `DEFAULT_TEXT`: con salida
  estructurada eso termina en `llm.bad_output`, a propósito.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from faro_engine.llm.catalog import Catalog, CatalogModel
from faro_engine.llm.client import PROVIDERS, TIERS, RawCompletion, ResolvedCall
from faro_engine.llm.errors import FailureKind, LlmCallError

FAKE_MODEL: Final = "fake"
# Precio de prueba alto: US$100 por millón de tokens de entrada y US$500 de salida. Una
# llamada de ~2 000 tokens de entrada y ~500 de salida cuesta unos US$0,45.
FAKE_INPUT_MICROS_PER_MTOK: Final = 100_000_000
FAKE_OUTPUT_MICROS_PER_MTOK: Final = 500_000_000
FAKE_MAX_OUTPUT_TOKENS: Final = 128_000
FAKE_CONTEXT_TOKENS: Final = 1_000_000
DEFAULT_TEXT: Final = "Respuesta simulada de Faro (modo de desarrollo)."
CHARS_PER_FAKE_TOKEN: Final = 4


@dataclass(frozen=True, slots=True)
class FakeReply:
    """Respuesta simulada. Sin tokens explícitos, se cuentan con `count_tokens`."""

    text: str | None = None
    tool_calls: tuple[tuple[str, str], ...] = ()
    finish_reason: str = "stop"
    tokens_in: int | None = None
    tokens_out: int | None = None
    report_usage: bool = True  # `False` simula un proveedor que no informa los tokens


@dataclass(frozen=True, slots=True)
class FakeFailure:
    """Fallo simulado de un intento (cada fila del mapeo de `llm/errors.py`)."""

    kind: FailureKind
    retry_after: float | None = None


FakeStep = FakeReply | FakeFailure

# Respuestas del modo `--fake-llm` por `prompt_id` (los agentes añaden las suyas).
DEV_RESPONSES: dict[str, FakeStep | Sequence[FakeStep]] = {}


def count_tokens(text: str) -> int:
    """Conteo determinista: caracteres / 4, hacia arriba."""
    return -(-len(text) // CHARS_PER_FAKE_TOKEN)


def _prompt_text(call: ResolvedCall) -> str:
    return "".join(message.get("content", "") for message in call.messages)


@dataclass(slots=True)
class FakeLLM:
    """`LlmClient` sin red. `responses` puede ser una respuesta o una secuencia por
    `prompt_id`: cada llamada consume la siguiente y la última se repite."""

    responses: Mapping[str, FakeStep | Sequence[FakeStep]] = field(default_factory=dict)
    default: FakeStep = field(default_factory=lambda: FakeReply(text=DEFAULT_TEXT))
    needs_key: bool = False
    calls: list[ResolvedCall] = field(default_factory=list)
    keys_seen: list[bool] = field(default_factory=list)  # si llegó clave; nunca su valor
    _cursor: dict[str, int] = field(default_factory=dict)

    @property
    def requires_key(self) -> bool:
        return self.needs_key

    def _next(self, prompt_id: str) -> FakeStep:
        configured = self.responses.get(prompt_id)
        if configured is None:
            return self.default
        if isinstance(configured, FakeReply | FakeFailure):
            return configured
        steps = list(configured)
        index = self._cursor.get(prompt_id, 0)
        self._cursor[prompt_id] = index + 1
        return steps[min(index, len(steps) - 1)]

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        self.calls.append(call)
        self.keys_seen.append(bool(api_key))
        step = self._next(call.prompt_id)
        if isinstance(step, FakeFailure):
            raise LlmCallError(step.kind, retry_after=step.retry_after)
        text = step.text
        tokens_in = step.tokens_in if step.tokens_in is not None else count_tokens(
            _prompt_text(call)
        )
        produced = (text or "") + "".join(name + args for name, args in step.tool_calls)
        tokens_out = step.tokens_out if step.tokens_out is not None else count_tokens(produced)
        return RawCompletion(
            text=text,
            tool_calls=step.tool_calls,
            finish_reason=step.finish_reason,
            tokens_in=tokens_in if step.report_usage else None,
            tokens_out=tokens_out if step.report_usage else None,
        )


def dev_fake_llm(extra: Mapping[str, FakeStep | Sequence[FakeStep]] | None = None) -> FakeLLM:
    """El `FakeLLM` del modo `--fake-llm` (respuestas de `DEV_RESPONSES`)."""
    responses: dict[str, FakeStep | Sequence[FakeStep]] = dict(DEV_RESPONSES)
    responses.update(extra or {})
    return FakeLLM(responses=responses)


def _fake_models() -> Iterable[CatalogModel]:
    for provider in PROVIDERS:
        for tier in TIERS:
            # `model_construct`: el modelo "fake" no es de un proveedor real (ni tiene página
            # oficial), así que no pasa por las reglas de `models.json`.
            yield CatalogModel.model_construct(
                provider=provider,
                tier=tier,
                model=FAKE_MODEL,
                litellm_model=f"{provider}/{FAKE_MODEL}",
                context_tokens=FAKE_CONTEXT_TOKENS,
                max_output_tokens=FAKE_MAX_OUTPUT_TOKENS,
                input_micros_per_mtok=FAKE_INPUT_MICROS_PER_MTOK,
                output_micros_per_mtok=FAKE_OUTPUT_MICROS_PER_MTOK,
                long_context=None,
                price_changes=(),
                verified_at=date(2026, 10, 9),
                source="https://ai.google.dev/",
            )


def fake_catalog() -> Catalog:
    """Catálogo del modo `--fake-llm`: modelo `"fake"` con precio de prueba alto."""
    return Catalog.model_construct(version=1, currency="USD", models=tuple(_fake_models()))
