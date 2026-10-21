"""Tamaños de los datos de `site_summary` (spec F1b §4.4; condición T6-C3 de la revisión de
T6). Los usan los nodos (`graph.py`) y el estimado (`estimate.py`): si cambian, cambian el
máximo y el presupuesto de tokens de la tarea.

Cada `DataBlock` del agente lleva un `max_chars` explícito, y los elementos del sitio se
acotan además por **bytes UTF-8** (`untrusted.fit_items`): ningún tokenizador da más tokens
que bytes, así que el estimado reserva un token por byte y el máximo queda garantizado
aunque el sitio esté en chino, en árabe o lleno de emojis (donde `ceil(caracteres / 3)` se
queda corto). Los nodos pasan esa cota a `LlmService`, que reserva con ella (T9-C1).
"""

from __future__ import annotations

from typing import Final

# `read_site`: primera página de cada tipo (spec §4.4: como mucho 50 de cada uno).
PER_KIND_LIMIT: Final = 50
KINDS: Final = ("page", "post", "product")
MAX_ITEMS: Final = PER_KIND_LIMIT * len(KINDS)

# Longitud máxima de cada campo del sitio que entra en un prompt (caracteres).
SITE_NAME_MAX_CHARS: Final = 120
TITLE_MAX_CHARS: Final = 120
PATH_MAX_CHARS: Final = 100

# Bloque de los elementos del sitio en `classify_content`: tope en bytes UTF-8 (y el mismo
# `max_chars`). ~150 elementos típicos ocupan unos 12 000 bytes.
ITEMS_BLOCK_BYTES: Final = 24_000
# Bloque con el nombre y los conteos del sitio (los dos prompts). Nombre ≤ 120 caracteres
# (≤ 480 bytes) más los conteos y las etiquetas: menos de 600 bytes (lo comprueba una
# prueba), así que 600 vale como tope de caracteres y de bytes.
SITE_BLOCK_CHARS: Final = 600
# Bloque de la clasificación en `write_summary`: la salida de `ContentClassificationV1`
# con todos sus campos al máximo ocupa menos de 1 600 caracteres y, con caracteres de
# 4 bytes, menos de 4 200 bytes (lo comprueba una prueba).
CLASSIFICATION_BLOCK_CHARS: Final = 1_600
CLASSIFICATION_BLOCK_BYTES: Final = 4_200
# Lo que añaden la apertura y el cierre de un bloque con su nonce y la separación entre
# bloques (`<datos origen="modelo" id="…">` … `</datos id="…">`): menos de 80 bytes.
BLOCK_WRAPPER_BYTES: Final = 80

# Salida máxima de cada prompt (spec §4.4); el razonamiento interno de los modelos que
# piensan cuenta dentro (informe de T6, decisión 4): se mide en la verificación manual de T12.
CLASSIFY_MAX_OUTPUT_TOKENS: Final = 800
WRITE_MAX_OUTPUT_TOKENS: Final = 1_200
