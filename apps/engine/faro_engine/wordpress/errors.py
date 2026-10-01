"""Respuestas de error del plugin → códigos `site.*` del motor (spec F1a §5.6).

| Respuesta del sitio | Código |
| --- | --- |
| `403 wp.pairing_invalid` | `site.pairing_code_invalid` (`details.attempts_left`) |
| `410 wp.pairing_expired` | `site.pairing_code_expired` |
| `429` (tras el reintento, si lo hubo) | `site.rate_limited` |
| `403 wp.insecure_site` | `site.https_required` |
| `401 wp.revoked` | `site.revoked` (veredicto) |
| `401 wp.connection_broken` | `site.connection_broken` (veredicto) |
| `401 wp.invalid_signature` | `site.auth_failed` (veredicto) |
| `401 wp.stale_request` | `site.clock_skew` |
| otro 401/403 (plugin de seguridad, WAF) | `site.blocked` |
| 404 | `site.plugin_not_found` |
| 3xx | `site.moved` |
| 5xx | `site.server_error` |
| cualquier otra cosa | `site.bad_response` |

Nunca se copia el mensaje del sitio: solo se lee su `code` (y `attempts_left`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from faro_engine.core.errors import (
    SITE_AUTH_FAILED,
    SITE_BAD_RESPONSE,
    SITE_BLOCKED,
    SITE_CLOCK_SKEW,
    SITE_CONNECTION_BROKEN,
    SITE_HTTPS_REQUIRED,
    SITE_MOVED,
    SITE_PAIRING_CODE_EXPIRED,
    SITE_PLUGIN_NOT_FOUND,
    SITE_RATE_LIMITED,
    SITE_REVOKED,
    SITE_SERVER_ERROR,
    FaroError,
    pairing_code_invalid,
    site_error,
)
from faro_engine.net.client import HttpResponse

# Veredictos: el sitio dice que esta conexión ya no vale. No son fallos de red.
VERDICT_CODES: Final = frozenset({SITE_REVOKED, SITE_CONNECTION_BROKEN, SITE_AUTH_FAILED})

_UNAUTHORIZED: Final[Mapping[str, str]] = {
    "wp.revoked": SITE_REVOKED,
    "wp.connection_broken": SITE_CONNECTION_BROKEN,
    "wp.invalid_signature": SITE_AUTH_FAILED,
    "wp.stale_request": SITE_CLOCK_SKEW,
}
MAX_ATTEMPTS: Final = 5


def parse_json(response: HttpResponse) -> Any:
    """Cuerpo JSON o `None` si no lo es."""
    try:
        return json.loads(bytes(response.body))
    except ValueError:  # incluye UnicodeDecodeError y JSONDecodeError
        return None


def _plugin_error(response: HttpResponse) -> tuple[str | None, Mapping[str, Any]]:
    data = parse_json(response)
    if not isinstance(data, dict):
        return None, {}
    code = data.get("code")
    extra = data.get("data")
    return (code if isinstance(code, str) else None), (extra if isinstance(extra, dict) else {})


def _attempts_left(extra: Mapping[str, Any]) -> int:
    value = extra.get("attempts_left")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_ATTEMPTS:
        return value
    return 0


def error_for_response(response: HttpResponse) -> FaroError:
    """Error `site.*` para una respuesta que no es el 200 esperado."""
    status = response.status
    code, extra = _plugin_error(response)
    if 300 <= status < 400:
        return site_error(SITE_MOVED)
    if status == 401:
        return site_error(_UNAUTHORIZED.get(code or "", SITE_BLOCKED))
    if status == 403:
        if code == "wp.pairing_invalid":
            return pairing_code_invalid(_attempts_left(extra))
        if code == "wp.insecure_site":
            return site_error(SITE_HTTPS_REQUIRED)
        return site_error(SITE_BLOCKED)
    if status == 404:
        return site_error(SITE_PLUGIN_NOT_FOUND)
    if status == 410 and code == "wp.pairing_expired":
        return site_error(SITE_PAIRING_CODE_EXPIRED)
    if status == 429:
        return site_error(SITE_RATE_LIMITED)
    if status >= 500:
        return site_error(SITE_SERVER_ERROR)
    return site_error(SITE_BAD_RESPONSE)
