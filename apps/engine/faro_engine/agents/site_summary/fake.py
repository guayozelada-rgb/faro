"""Respuestas de `site_summary` en el modo `--fake-llm` (solo desarrollo, spec F1b §4.1).

Fijas y marcadas como simuladas: sirven para recorrer la interfaz y los flujos (propuesta,
Bandeja, Inicio) sin gastar. Nunca se usan con una clave real (el modo `--fake-llm` no
existe en el binario empaquetado).
"""

from __future__ import annotations

import json
from typing import Final

from faro_engine.llm.fake import FakeReply, FakeStep

CLASSIFICATION: Final = {
    "version": 1,
    "site_kind": "other",
    "topics": ["Contenido de ejemplo"],
    "offerings": ["Respuesta simulada"],
    "audience": "Personas que prueban Faro en modo de desarrollo.",
    "language": "es",
}
SUMMARY: Final = {
    "version": 1,
    "headline": "Resumen simulado de tu sitio (modo de desarrollo)",
    "summary": (
        "Este resumen lo escribió el modo de desarrollo de Faro, sin usar ninguna IA. "
        "Sirve para probar cómo se guarda y se muestra un resumen."
    ),
    "offerings": ["Respuesta simulada"],
    "audience": "Personas que prueban Faro en modo de desarrollo.",
    "language": "es",
}

DEV_REPLIES: Final[dict[str, FakeStep]] = {
    "site_summary.classify_content": FakeReply(text=json.dumps(CLASSIFICATION, ensure_ascii=False)),
    "site_summary.write_summary": FakeReply(text=json.dumps(SUMMARY, ensure_ascii=False)),
}
