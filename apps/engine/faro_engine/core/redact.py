"""Redacción de secretos en los logs por **valor** y por **nombre** (ADR 0013).

Es la segunda defensa: la regla sigue siendo no registrar secretos. Mismas reglas que el
núcleo (`apps/desktop/src-tauri/src/logging/redact.rs`); si cambias una, cambia la otra.

- `redact_event`: procesador de `structlog` que recorre todos los valores del evento,
  también anidados. Los campos con nombre sensible pasan a `[redactado]`; cada texto se
  redacta como texto libre (valor y `nombre=valor`) y, si es a su vez JSON, por dentro.
- `redact_rendered`: último procesador, sobre la línea JSON ya serializada. Solo aplica
  los patrones de valor (los de nombre podrían romper el JSON). Cubre lo que el
  serializador convierte con `repr()` y cualquier texto que se haya colado.

Patrones de valor (ADR 0013 §2): `sk-…` (OpenAI, Anthropic `sk-ant-…`), `AIza…`
(Google), `Bearer …`, `Basic …`, JWT, 64 hex seguidos (llave de la base) y exactamente
43 caracteres base64url seguidos (token de sesión, token y secreto del sitio). El código
de vinculación no se filtra por valor (6 dígitos darían falsos positivos), solo por
nombre (`pairing_code`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, MutableMapping
from typing import Any, Final

REDACTED: Final = "[redactado]"

# Nombres de campo sensibles (sin distinguir mayúsculas). Los del núcleo (`SENSITIVE_NAMES`
# de `redact.rs`) + `key_hex` (nombre interno de la llave de la base en el motor).
# `code` no se filtra: son códigos de error.
SENSITIVE_NAMES: Final = frozenset(
    {
        "authorization",
        "headers",
        "token",
        "secret",
        "cookie",
        "password",
        "api_key",
        "hmac_secret",
        "refresh_token",
        "key",
        "db_key",
        "key_hex",
        "value",
        "pairing_code",
        "x-faro-token",
        "x-faro-signature",
    },
)

# Además, son sensibles los nombres que terminan así (`access_token`, `client_secret`,
# `x-api-key`) o que contienen `password`.
SENSITIVE_SUFFIXES: Final = ("_token", "_secret", "_key", "-token", "-secret", "-key")

# Encajarían por sufijo pero no llevan secretos: referencias al llavero, claves públicas o
# de ordenación/caché. Valen en cualquier forma (`secret_ref`, `secretRef`, `secret-ref`).
NON_SENSITIVE_NAMES: Final = frozenset(
    {
        "secret_ref",
        "public_key",
        "cache_key",
        "sort_key",
        "primary_key",
        "foreign_key",
        "idempotency_key",
    },
)

BASE64URL_TOKEN_LEN: Final = 43
HEX_KEY_LEN: Final = 64

_ASCII_LOWER_OR_DIGIT: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_IDENT_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:")
_LOOSE_VALUE_END: Final = frozenset(',;}])"')
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")

# Se sustituyen enteros, en este orden. JWT primero: sus segmentos podrían parecer otros.
_WHOLE_PATTERNS: Final = (
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    # ADR 0013: `Bearer\s+\S+`, sin comillas ni barra invertida para no romper el JSON.
    re.compile(r"Bearer\s+[^\s\"\\]+"),
    re.compile(r"Basic\s+[A-Za-z0-9+/_-]{8,}={0,2}"),
)
# Con límite de palabra (no redacta `task-queue-…`) o justo tras un escape `\n`, `\r`, `\t`.
_SK_KEY: Final = re.compile(r"(^|[^A-Za-z0-9_]|\\[nrt])sk-[A-Za-z0-9_-]{16,}")
_BASE64URL_RUN: Final = re.compile(r"[A-Za-z0-9_-]+")
_WORD_RUN: Final = re.compile(r"[A-Za-z0-9_]+")
_NAME_PREFIX: Final = re.compile(r"(?:^|[^A-Za-z0-9_.-])\"?([A-Za-z0-9_-]+)\"?\s*[=:]\s*")


def _normalized_name(name: str) -> str:
    return name.replace("_", "").replace("-", "").lower()


_NON_SENSITIVE_NORMALIZED: Final = frozenset(_normalized_name(n) for n in NON_SENSITIVE_NAMES)


def _is_camel_case_key(name: str) -> bool:
    """`…Key` con una minúscula o un dígito justo antes (`apiKey`, pero no `hotkey`)."""
    return len(name) > 3 and name.endswith("Key") and name[-4] in _ASCII_LOWER_OR_DIGIT


def is_sensitive_name(name: str) -> bool:
    """¿Un campo con este nombre puede llevar un secreto?"""
    if _normalized_name(name) in _NON_SENSITIVE_NORMALIZED:
        return False
    lower = name.lower()
    return (
        lower in SENSITIVE_NAMES
        or lower.endswith(SENSITIVE_SUFFIXES)
        or lower.endswith(("token", "secret"))
        or _is_camel_case_key(name)
        or "password" in lower
    )


def _replace_runs(run: re.Pattern[str], text: str, is_secret: Callable[[str], bool]) -> str:
    """Sustituye cada secuencia máxima de `run` que cumpla `is_secret`. Tras una barra
    invertida, si empieza por `n`, `r` o `t` (escape JSON), se prueba sin esa letra."""
    out: list[str] = []
    last = 0
    for found in run.finditer(text):
        candidate = found.group()
        start = found.start()
        after_escape = text[:start].endswith("\\") and candidate[0] in "nrt"
        if is_secret(candidate):
            secret_start: int | None = start
        elif after_escape and is_secret(candidate[1:]):
            secret_start = start + 1
        else:
            secret_start = None
        if secret_start is not None:
            out.append(text[last:secret_start])
            out.append(REDACTED)
            last = found.end()
    out.append(text[last:])
    return "".join(out)


def _is_hex_key(run: str) -> bool:
    return len(run) == HEX_KEY_LEN and all(c in _HEX_DIGITS for c in run)


def _is_base64url_token(run: str) -> bool:
    return len(run) == BASE64URL_TOKEN_LEN


def redact_values(text: str) -> str:
    """Solo patrones de valor: no cambian comillas, barras ni llaves (el JSON sigue válido)."""
    for pattern in _WHOLE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _SK_KEY.sub(lambda m: m.group(1) + REDACTED, text)
    text = _replace_runs(_WORD_RUN, text, _is_hex_key)
    return _replace_runs(_BASE64URL_RUN, text, _is_base64url_token)


def _quoted_len(text: str) -> int:
    """Texto entre comillas que empieza en `text[0]`, hasta la comilla de cierre no
    escapada (o el final)."""
    escaped = False
    for i in range(1, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            return i + 1
    return len(text)


def _balanced_len(text: str) -> int:
    """Desde un `{`, `[` o `(` hasta su cierre equilibrado (respetando comillas) o el final."""
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth = max(depth - 1, 0)
            if depth == 0:
                return i + 1
    return len(text)


def _authorization_len(rest: str) -> int:
    """Valor de `authorization`: entre comillas, o hasta el final de la línea o del texto
    entre comillas que lo contiene (cubre `Token x`, `Digest …` y esquemas raros)."""
    if rest.startswith('"'):
        return _quoted_len(rest)
    for i, ch in enumerate(rest):
        if ch in '"\n\r':
            # `\"` de un texto escapado: se corta antes de la barra.
            if i > 0 and ch == '"' and rest[i - 1] == "\\":
                return i - 1
            return i
    return len(rest)


def _value_len(rest: str) -> int:
    """Longitud del valor que empieza al principio de `rest`."""
    if not rest:
        return 0
    first = rest[0]
    if first == '"':
        return _quoted_len(rest)
    if first in "{[(":
        return _balanced_len(rest)
    # `Some("…")`, `Tipo { … }`: identificador y luego `(` o `{` → hasta el cierre.
    ident = next((i for i, c in enumerate(rest) if c not in _IDENT_CHARS), len(rest))
    after = next((i for i in range(ident, len(rest)) if rest[i] != " "), len(rest))
    if ident > 0 and after < len(rest) and rest[after] in "({":
        return after + _balanced_len(rest[after:])
    return next((i for i, c in enumerate(rest) if c.isspace() or c in _LOOSE_VALUE_END), len(rest))


def _redact_named_values(text: str) -> str:
    """Sustituye el valor de cada `nombre=valor`, `nombre: valor` o `"nombre": valor` con
    nombre sensible."""
    out: list[str] = []
    copied = 0
    search = 0
    while search < len(text) and (match := _NAME_PREFIX.search(text, search)) is not None:
        name = match.group(1)
        value_start = match.end()
        value_end = value_start
        if is_sensitive_name(name):
            rest = text[value_start:]
            length = _authorization_len(rest) if name.lower() == "authorization" else None
            value_end = value_start + (_value_len(rest) if length is None else length)
        if value_end > value_start:
            out.append(text[copied:value_start])
            quoted = text.startswith('"', value_start)
            out.append(f'"{REDACTED}"' if quoted else REDACTED)
            copied = value_end
            search = value_end
        else:
            # Sigue justo después del nombre, por si el "valor" contiene otro par.
            search = match.end(1)
    out.append(text[copied:])
    return "".join(out)


def redact_text(text: str) -> str:
    """Texto libre (no JSON): patrones de valor y de nombre."""
    return _redact_named_values(redact_values(text))


def _redact_string(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        else:
            # Empieza por `{` o `[` y es JSON válido: es un objeto o una lista.
            redacted = redact_value(parsed)
            if redacted == parsed:
                return redact_values(text)
            return redact_values(json.dumps(redacted, ensure_ascii=False))
    return redact_text(text)


def redact_value(value: Any) -> Any:
    """Copia de `value` con los secretos redactados (dicts, listas, tuplas y textos)."""
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_name(k) else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact_value(item) for item in value)
    return value


def redact_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Procesador de `structlog`: redacta por nombre y por valor todos los campos."""
    for name in list(event_dict):
        value = event_dict[name]
        event_dict[name] = REDACTED if is_sensitive_name(name) else redact_value(value)
    return event_dict


def redact_rendered(_logger: Any, _method: str, rendered: Any) -> str:
    """Último procesador: patrones de valor sobre la línea ya serializada."""
    return redact_values(str(rendered))
