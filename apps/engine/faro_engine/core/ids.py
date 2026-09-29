"""Identificadores UUID v7 (ordenables por tiempo) como texto."""

from __future__ import annotations

import os
import time
import uuid

_MASK_48 = (1 << 48) - 1
_MASK_62 = (1 << 62) - 1


def uuid7() -> uuid.UUID:
    """UUID versión 7 (RFC 9562): 48 bits de milisegundos Unix + 74 bits aleatorios."""
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68  # 12 bits
    rand_b = rand & _MASK_62  # 62 bits
    value = (millis & _MASK_48) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def new_id() -> str:
    return str(uuid7())
