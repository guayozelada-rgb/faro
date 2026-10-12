"""Tokenizador local para las evaluaciones (condición T6-C3 de la revisión de T6), sin red.

`cl100k_base` (OpenAI) desde el archivo que ya incluye el motor
(`faro_engine/llm/tiktoken`, SHA-256 comprobado por `hardening.verify_tiktoken_file` y por
tiktoken). Es una **aproximación**: Anthropic y Gemini usan otros vocabularios. Por eso el
máximo de `site_summary` no se apoya en él, sino en un límite que vale para los tres (un
token por byte UTF-8); este tokenizador sirve para medir cuánto se queda corto
`ceil(caracteres / 3)` con texto no latino y documentarlo.
"""

from __future__ import annotations

from functools import cache
from typing import Final

import tiktoken
from tiktoken.load import load_tiktoken_bpe

from faro_engine.llm.hardening import CL100K_SHA256, verify_tiktoken_file

# Patrón de `cl100k_base` (`tiktoken_ext/openai_public.py`, tiktoken 0.14).
CL100K_PATTERN: Final = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+"""
    r"""|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)


@cache
def cl100k() -> tiktoken.Encoding:
    path = verify_tiktoken_file()
    ranks = load_tiktoken_bpe(str(path), expected_hash=CL100K_SHA256)
    return tiktoken.Encoding(
        name="cl100k_base_faro", pat_str=CL100K_PATTERN, mergeable_ranks=ranks, special_tokens={}
    )


def count_tokens(text: str) -> int:
    return len(cl100k().encode(text, disallowed_special=()))
