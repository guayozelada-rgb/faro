"""Dobles de la capa de IA para las pruebas (spec F1b §9.2, skill `pruebas-faro`).

- `FakeLLM`, `FakeReply`, `FakeFailure`: los de `faro_engine/llm/fake.py` (el mismo doble
  que usa el modo `--fake-llm`), sin red.
- `FakeSecrets`: el canal de secretos visto desde `LlmService`: cuenta cada `get` (para
  probar que los rechazos 1-5 no piden la clave) y entrega una clave falsa evidente en un
  `SecretValue` que se sobrescribe al salir.
- Claves falsas con forma evidente, construidas por partes para que gitleaks no las tome
  por secretos reales.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from faro_engine.core.secrets import SecretError, SecretValue
from faro_engine.llm.client import LlmMessage, LlmRequest, TaskKind
from faro_engine.llm.fake import FakeFailure, FakeLLM, FakeReply, count_tokens

__all__ = [
    "FAKE_KEYS",
    "FakeFailure",
    "FakeLLM",
    "FakeReply",
    "FakeSecrets",
    "count_tokens",
    "make_request",
]

FAKE_KEYS = {
    "anthropic": "sk-ant-" + "test-" + "faro0" * 8,
    "openai": "sk-" + "test-" + "faro1" * 8,
    "gemini": "AIza" + "TEST" + "faro2" * 7,
}


@dataclass
class FakeSecrets:
    """`get(ref)` devuelve la clave falsa del proveedor de `ref` (o falla con `error`)."""

    error: str | None = None
    requests: list[tuple[str, float | None]] = field(default_factory=list)
    values: list[SecretValue] = field(default_factory=list)
    value: bytes | None = None

    async def get(self, ref: str, *, max_wait: float | None = None) -> SecretValue:
        self.requests.append((ref, max_wait))
        if self.error is not None:
            raise SecretError(self.error)
        provider = ref.split("/")[1]
        raw = self.value if self.value is not None else FAKE_KEYS[provider].encode("ascii")
        secret = SecretValue(bytearray(raw))
        self.values.append(secret)
        return secret


def make_request(
    task_kind: TaskKind = "classify",
    *,
    content: str = "Hola, clasifica esto.",
    max_output_tokens: int = 100,
    prompt_id: str = "test.prompt",
    **extra: object,
) -> LlmRequest:
    return LlmRequest(
        task_kind=task_kind,
        messages=(LlmMessage(role="user", content=content),),
        max_output_tokens=max_output_tokens,
        prompt_id=prompt_id,
        prompt_version=1,
        **extra,  # type: ignore[arg-type]
    )
