"""Contenido remoto como datos, nunca como instrucciones (ADR 0015 §5, spec F1b §4.2).

Todo texto que viene de fuera del motor (el sitio, buscadores, APIs **y las salidas del
modelo** derivadas de ese contenido) entra como `UntrustedText` y solo puede llegar a un
prompt dentro de un `DataBlock` (`prompts.render`). `UntrustedText` **no** hereda de `str`:
así `mypy --strict` impide usarlo como instrucción, y `prompts.render` lo rechaza también en
tiempo de ejecución.

Normalización al construirlo (cada punto con su prueba):

- Unicode NFC;
- sin caracteres de control salvo el salto de línea (`\\r\\n` y `\\r` pasan a `\\n`; el
  tabulador pasa a espacio);
- sin marcas bidireccionales (U+202A-U+202E, U+2066-U+2069) ni otros caracteres de formato
  invisibles (categoría Unicode `Cf`: U+200B-U+200F, U+FEFF…);
- `<` y `>` pasan a las comillas angulares simples U+2039 y U+203A: un texto no puede
  abrir ni cerrar un bloque `<datos …>`;
- longitud máxima por campo (`max_chars`); lo que sobra se recorta con `TRUNCATION_MARK`.

Un `DataBlock` se renderiza con un **nonce** aleatorio de 12 caracteres nuevo en cada
llamada (`new_nonce`); el nonce no se guarda ni se registra:

    <datos origen="sitio" id="k3J9xQ2mP0aZ">
    - …elementos normalizados…
    </datos id="k3J9xQ2mP0aZ">

Tamaño en bytes (condición T6-C3 de la revisión de T6): `ceil(caracteres / 3)` se queda
corto con un texto en chino, árabe o con emojis, que tiene más tokens por carácter.
Ningún tokenizador de los tres proveedores produce más tokens que bytes UTF-8 (BPE por
bytes, o SentencePiece con respaldo a bytes), así que un agente que quiere un máximo
**garantizado** acota sus datos por bytes con `fit_items`, reserva un token por byte
(`budget.PlannedCall.max_input_tokens`) y pasa esa misma cota en cada llamada
(`NodeContext.call_llm(..., input_tokens_bound=…)`, obligatoria) para que `LlmService`
reserve con ella del tope diario (condición T9-C1). Una petición sin cota reserva como
mínimo los bytes del prompt, del esquema y de las herramientas (revisión de T14, B1).

Esto reduce el riesgo, no lo elimina: lo que publica, gasta o borra nunca depende solo de
la salida del modelo (autonomía y aprobaciones, ADR 0016).
"""

from __future__ import annotations

import json
import re
import secrets
import string
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

Source = Literal["site", "search", "api", "llm"]
SOURCES: Final[tuple[Source, ...]] = ("site", "search", "api", "llm")
# Nombre visible del origen dentro del bloque (las instrucciones están en español).
SOURCE_LABELS: Final[Mapping[Source, str]] = {
    "site": "sitio",
    "search": "buscador",
    "api": "api",
    "llm": "modelo",
}

DEFAULT_MAX_CHARS: Final = 4_000
MAX_FIELD_CHARS: Final = 100_000
MAX_BLOCK_CHARS: Final = 200_000
TRUNCATION_MARK: Final = " […recortado]"
NONCE_LENGTH: Final = 12
NONCE_ALPHABET: Final = string.ascii_letters + string.digits
# Claves de los elementos de un bloque: identificadores del código, nunca del contenido.
ITEM_KEY_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")

BIDI_MARKS: Final = frozenset(
    {*(chr(c) for c in range(0x202A, 0x202F)), *(chr(c) for c in range(0x2066, 0x206A))}
)
_ANGLES: Final = str.maketrans({"<": "\u2039", ">": "\u203a"})


def _keep(char: str) -> bool:
    if char == "\n":
        return True
    if char in BIDI_MARKS:
        return False
    category = unicodedata.category(char)
    # Cc: controles; Cf: formato invisible (incluye bidireccionales y anchura cero);
    # Cs: sustitutos sueltos; Co/Cn: uso privado y sin asignar.
    return category not in {"Cc", "Cf", "Cs", "Co", "Cn"}


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_MARK):
        return text[:max_chars]  # no cabe ni la marca: nunca se pasa del máximo
    return text[: max_chars - len(TRUNCATION_MARK)] + TRUNCATION_MARK


def normalize(value: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Texto externo normalizado (ver la cabecera del módulo)."""
    if not isinstance(value, str):
        raise TypeError("el contenido externo debe ser texto")
    if not 1 <= max_chars <= MAX_FIELD_CHARS:
        raise ValueError("max_chars fuera de rango")
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = "".join(char for char in text if _keep(char))
    text = text.translate(_ANGLES)
    return _truncate(text, max_chars)


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """Texto de fuera del motor, ya normalizado. **No** es `str`: no puede ser instrucción."""

    source: Source
    value: str
    max_chars: int = field(default=DEFAULT_MAX_CHARS, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError("origen de contenido desconocido")
        object.__setattr__(self, "value", normalize(self.value, self.max_chars))

    def __str__(self) -> str:  # pragma: no cover - solo para depurar; nunca en prompts
        return f"UntrustedText(source={self.source!r}, chars={len(self.value)})"

    def to_json(self) -> dict[str, str]:
        """Forma JSON para el estado del grafo (solo tipos JSON, skill `agentes-langgraph`)."""
        return {"source": self.source, "value": self.value}

    @classmethod
    def from_json(
        cls, data: Mapping[str, Any], max_chars: int = DEFAULT_MAX_CHARS
    ) -> UntrustedText:
        """Vuelve a envolver (y normalizar) lo que se guardó con `to_json`."""
        source = data.get("source")
        value = data.get("value")
        if set(data) != {"source", "value"} or not isinstance(value, str):
            raise ValueError("forma de texto externo inválida")
        if source not in SOURCES:
            raise ValueError("origen de contenido desconocido")
        return cls(source, value, max_chars)


Item = UntrustedText | Mapping[str, UntrustedText | int]


@dataclass(frozen=True, slots=True)
class DataBlock:
    """Bloque de datos de terceros para la sección `user` de un prompt."""

    source: Source
    items: Sequence[Item]
    max_chars: int = DEFAULT_MAX_CHARS

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError("origen de contenido desconocido")
        if not 1 <= self.max_chars <= MAX_BLOCK_CHARS:
            raise ValueError("max_chars fuera de rango")
        for item in self.items:
            _check_item(item)


def _check_item(item: object) -> None:
    if isinstance(item, UntrustedText):
        return
    if not isinstance(item, Mapping):
        raise TypeError("un elemento de un bloque es UntrustedText o un mapa de ellos")
    for key, value in item.items():
        if not isinstance(key, str) or ITEM_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("clave de elemento inválida")
        if isinstance(value, bool) or not isinstance(value, UntrustedText | int):
            raise TypeError("los valores de un elemento son UntrustedText o enteros")


def llm_item(
    data: Mapping[str, Any], max_chars: int = DEFAULT_MAX_CHARS
) -> dict[str, UntrustedText | int]:
    """Una salida estructurada del modelo (dict JSON ya validado) como elemento de un bloque:
    los textos pasan a `UntrustedText(source="llm")` (condición T6-C4: lo que dijo un modelo
    que leyó el sitio vuelve a un prompt **como datos**). Listas → textos unidos con `; `;
    mapas anidados → su JSON como texto. Solo las claves con forma de identificador."""
    item: dict[str, UntrustedText | int] = {}
    for key, value in data.items():
        if not isinstance(key, str) or ITEM_KEY_PATTERN.fullmatch(key) is None:
            continue
        if isinstance(value, bool) or value is None:
            item[key] = UntrustedText("llm", str(value).lower(), max_chars)
        elif isinstance(value, int):
            item[key] = value
        elif isinstance(value, list):
            item[key] = UntrustedText("llm", "; ".join(str(v) for v in value), max_chars)
        elif isinstance(value, dict):
            item[key] = UntrustedText("llm", json.dumps(value, ensure_ascii=False), max_chars)
        else:
            item[key] = UntrustedText("llm", str(value), max_chars)
    return item


def item_text(item: Item) -> str:
    """El texto de un elemento tal como queda dentro del bloque (sin el salto de línea)."""
    _check_item(item)
    return _render_item(item)


def fit_items(items: Sequence[Item], max_bytes: int) -> list[Item]:
    """Los primeros elementos cuyo texto, con su salto de línea, cabe en `max_bytes` bytes
    UTF-8 (ver «Tamaño en bytes» en la cabecera). Se para en el primero que no cabe: el
    orden lo decide quien llama. Con el resultado, `DataBlock(max_chars=max_bytes)` nunca
    recorta (los caracteres nunca son más que los bytes)."""
    if not 1 <= max_bytes <= MAX_BLOCK_CHARS:
        raise ValueError("max_bytes fuera de rango")
    kept: list[Item] = []
    used = 0
    for item in items:
        size = len(item_text(item).encode("utf-8")) + 1
        if used + size > max_bytes:
            break
        kept.append(item)
        used += size
    return kept


def new_nonce() -> str:
    """Identificador aleatorio de 12 caracteres para cerrar el bloque (nuevo en cada uso)."""
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(NONCE_LENGTH))


def _render_value(value: UntrustedText | int) -> str:
    if isinstance(value, UntrustedText):
        return value.value
    return str(value)


def _render_item(item: Item) -> str:
    if isinstance(item, UntrustedText):
        return f"- {item.value}"
    parts = [f"{key}: {_render_value(value)}" for key, value in item.items()]
    return "- " + "; ".join(parts)


def render_block(block: DataBlock, nonce: str) -> str:
    """El bloque como texto, cerrado con `nonce`. El contenido nunca supera `max_chars`."""
    if len(nonce) != NONCE_LENGTH or any(char not in NONCE_ALPHABET for char in nonce):
        raise ValueError("nonce inválido")
    body = "\n".join(_render_item(item) for item in block.items)
    body = _truncate(body, block.max_chars)
    label = SOURCE_LABELS[block.source]
    return f'<datos origen="{label}" id="{nonce}">\n{body}\n</datos id="{nonce}">'
