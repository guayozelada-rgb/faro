"""Tipo de tarea → nivel de modelo, y proveedor de la clave que usan los agentes.

| `task_kind` | Nivel |
| --- | --- |
| `classify` | `economy` |
| `extract` | `economy` |
| `write` | `premium` |
| `plan` | `premium` |

El usuario no elige modelos: elige **la clave que usan los agentes**
(`settings.llm.preferred_provider`). Si no eligió, la primera con clave en el orden
Anthropic, OpenAI, Gemini. **Sin cambio automático de proveedor** si uno falla (decisión
del usuario, spec §11-4). Un `task_kind` nuevo se añade aquí y en su prueba.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final

from faro_engine.llm.client import PROVIDERS, Provider, TaskKind, Tier

TASK_TIERS: Final[Mapping[TaskKind, Tier]] = {
    "classify": "economy",
    "extract": "economy",
    "write": "premium",
    "plan": "premium",
}


def tier_for(task_kind: TaskKind) -> Tier:
    return TASK_TIERS[task_kind]


def is_provider(value: object) -> bool:
    return isinstance(value, str) and value in PROVIDERS


def choose_provider(preferred: str | None, with_key: Collection[str]) -> Provider | None:
    """Proveedor de los agentes: el preferido (aunque no tenga clave: entonces `llm.no_key`,
    sin pasar a otro) o, sin preferencia, el primero con clave en el orden fijo."""
    if preferred is not None:
        for provider in PROVIDERS:
            if provider == preferred:
                return provider
        return None
    for provider in PROVIDERS:
        if provider in with_key:
            return provider
    return None


def secret_ref_for(provider: Provider) -> str:
    """La única clave de IA por proveedor en F1b (alias `default`)."""
    return f"llm/{provider}/default"
