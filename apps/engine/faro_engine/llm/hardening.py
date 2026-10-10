"""Endurecimiento de LiteLLM 1.104.0 (ADR 0015 §1, skill `capa-llm` §2, informe de T2 §12).

Este módulo **no** importa LiteLLM: tiene las listas, comprobaciones y ajustes que
`litellm_client.py` aplica alrededor de su `import litellm`, para poder probarlos sin
cargar LiteLLM en el proceso de pruebas. Si algo no se puede aplicar, se lanza
`UnsafeEnvironmentError` o `HardeningError` y el adaptador no se carga (nunca se envía nada).

Condiciones del informe de T2 (§12) que cubre:

1. Antes del import: `LITELLM_MODE=PRODUCTION` (sin `load_dotenv()` de un `.env` buscado
   hacia arriba), `LITELLM_LOCAL_MODEL_COST_MAP=True` (sin descargar el mapa de precios),
   `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True` (sin descargar la tabla de cabeceras beta),
   `CUSTOM_TIKTOKEN_CACHE_DIR` apuntando al `cl100k_base` incluido en el motor (SHA-256
   comprobado por `verify_tiktoken_file`) y fuera del entorno todo lo de
   `SCRUBBED_NAMES`/`SCRUBBED_PREFIXES` (comparando en mayúsculas: también `http_proxy`).
2. Después del import: la misma limpieza y comprobación (`check_environment`).
4. Loggers `LiteLLM*` sin handlers, `propagate=True`, nivel `WARNING`
   (`quiet_litellm_loggers`): su handler escribe en **stdout**, el canal del protocolo.
5. Callbacks vacíos, sin caché y los indicadores de `LITELLM_SETTINGS`
   (`apply_litellm_settings` + `check_litellm_settings`).
14. El directorio de trabajo fuera de `sys.path` tras el import
    (`drop_cwd_from_sys_path`): `import litellm` hace `sys.path.append(os.getcwd())` y
    tiktoken importaría cualquier `tiktoken_ext/*.py` que hubiera ahí.
3. `api_base` fijo (`API_BASE`, solo `https`, host exacto, sin puerto), nunca del entorno
   ni del usuario (`official_api_base`).

El proxy del registro de Windows no es una variable: lo evita el cliente propio con
`trust_env=False` (condición 15, `litellm_client.py`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from collections.abc import Mapping, MutableMapping, MutableSequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from faro_engine.llm.client import Provider

TIKTOKEN_DIR: Final = Path(__file__).with_name("tiktoken")
# Nombre del archivo en la caché de tiktoken: SHA-1 de la URL oficial (`tiktoken/load.py`).
CL100K_CACHE_NAME: Final = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
# El mismo SHA-256 que exige tiktoken (`tiktoken_ext/openai_public.py`).
CL100K_SHA256: Final = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

API_BASE: Final[Mapping[Provider, str]] = {
    "openai": "https://api.openai.com/v1",  # LiteLLM añade /chat/completions
    "anthropic": "https://api.anthropic.com",  # LiteLLM añade /v1/messages
    # AI Studio, nunca Vertex; LiteLLM añade /models/<id>:generateContent y la clave va en
    # la cabecera `x-goog-api-key`, nunca en la URL (comprobado en T2).
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
OFFICIAL_HOSTS: Final[Mapping[Provider, str]] = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "gemini": "generativelanguage.googleapis.com",
}
CALL_TIMEOUT_SECONDS: Final = 60


def required_env() -> dict[str, str]:
    """Las únicas variables de LiteLLM y tiktoken que existen en el proceso del motor."""
    return {
        "LITELLM_MODE": "PRODUCTION",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        # Sin ella, la salida estructurada de Anthropic descarga con `httpx.get` (proxies del
        # entorno, sin el cliente propio) la tabla de cabeceras beta de LiteLLM desde
        # `raw.githubusercontent.com`: una configuración remota que cambia las cabeceras que
        # acompañan a la clave. Se usa la copia incluida en el paquete (hallazgo de T6).
        "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
        "CUSTOM_TIKTOKEN_CACHE_DIR": str(TIKTOKEN_DIR),
    }


# Se comparan en mayúsculas: en Windows el entorno no distingue y `https_proxy` también
# vale para httpx y aiohttp en Linux y macOS.
SCRUBBED_NAMES: Final = frozenset(
    {
        "SSLKEYLOGFILE",  # escribe las claves de sesión TLS en un archivo
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "DISABLE_AIOHTTP_TRANSPORT",  # httpx de LiteLLM con los proxies del entorno
        "GOOGLE_API_KEY",  # LiteLLM la usa como clave de Gemini si falta la explícita
        "CUSTOM_TIKTOKEN_CACHE_DIR",  # se fija la del motor (`required_env`), nunca se hereda
    }
)
# `SSL_*` (`SSL_VERIFY=False` desactiva TLS en LiteLLM), `AIOHTTP_*` (`AIOHTTP_TRUST_ENV`),
# LangSmith/LangChain y todas las de LiteLLM y los proveedores (`*_API_KEY`, `*_API_BASE`,
# `*_BASE_URL`: cambiarían la clave o el host de destino).
SCRUBBED_PREFIXES: Final = (
    "SSL_",
    "AIOHTTP_",
    "LANGSMITH_",
    "LANGCHAIN_",
    "LITELLM_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
)

# Ajustes de módulo de LiteLLM 1.104.0 (nombres verificados en T2).
EMPTY_LIST_SETTINGS: Final = (
    "success_callback",
    "failure_callback",
    "callbacks",
    "input_callback",
    "service_callback",
    "_async_success_callback",
    "_async_failure_callback",
)
LITELLM_SETTINGS: Final[Mapping[str, object]] = {
    "api_base": None,
    "api_key": None,
    "openai_key": None,
    "anthropic_key": None,
    "cache": None,
    "aclient_session": None,
    "client_session": None,
    "ssl_verify": True,  # nunca un contexto propio de LiteLLM: el TLS lo pone el cliente
    "turn_off_message_logging": True,
    "suppress_debug_info": True,
    "log_raw_request_response": False,
    "redact_messages_in_exceptions": True,
    "disable_hf_tokenizer_download": True,
    "drop_params": False,
}


class UnsafeEnvironmentError(RuntimeError):
    """Queda en el entorno una variable que podría desviar la clave o debilitar TLS."""


class HardeningError(RuntimeError):
    """Un ajuste obligatorio de LiteLLM no quedó aplicado: el adaptador no se carga."""


def is_scrubbed(name: str, required: Mapping[str, str] | None = None) -> bool:
    upper = name.upper()
    if upper in (required if required is not None else required_env()):
        return False
    return upper in SCRUBBED_NAMES or upper.startswith(SCRUBBED_PREFIXES)


def unsafe_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """Nombres que no deberían estar, o variables obligatorias con otro valor."""
    env = os.environ if environ is None else environ
    required = required_env()
    found = sorted(name for name in env if is_scrubbed(name, required))
    found += sorted(name for name, value in required.items() if env.get(name) != value)
    return found


def clean_environment(environ: MutableMapping[str, str] | None = None) -> None:
    """Quita lo que podría desviar la clave o debilitar TLS y fija `required_env()`."""
    env = os.environ if environ is None else environ
    required = required_env()
    for name in list(env):
        if is_scrubbed(name, required):
            del env[name]
    env.update(required)


def check_environment(environ: Mapping[str, str] | None = None) -> None:
    leftovers = unsafe_variables(environ)
    if leftovers:
        # Solo los nombres, nunca los valores.
        raise UnsafeEnvironmentError(", ".join(leftovers))


def drop_cwd_from_sys_path(
    path: MutableSequence[str] | None = None, cwd: Path | None = None
) -> None:
    """Quita de `sys.path` el directorio de trabajo (también `""`, que significa lo mismo)."""
    entries = sys.path if path is None else path
    here = (cwd or Path.cwd()).resolve()
    kept = [entry for entry in entries if entry and Path(entry).resolve() != here]
    entries[:] = kept


def verify_tiktoken_file(folder: Path | None = None) -> Path:
    """`cl100k_base` incluido y con el SHA-256 esperado; si no, `HardeningError`.

    Sin él, LiteLLM lo descargaría de `openaipublic.blob.core.windows.net` en la primera
    llamada de Anthropic o Gemini y lo escribiría en la carpeta del paquete (informe de T2).
    """
    path = (folder or TIKTOKEN_DIR) / CL100K_CACHE_NAME
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HardeningError("falta el vocabulario cl100k_base del motor") from exc
    if digest != CL100K_SHA256:
        raise HardeningError("el vocabulario cl100k_base del motor no es el esperado")
    return path


def apply_litellm_settings(module: Any) -> None:
    for name in EMPTY_LIST_SETTINGS:
        setattr(module, name, [])
    for name, value in LITELLM_SETTINGS.items():
        setattr(module, name, value)


def check_litellm_settings(module: Any) -> None:
    """Comprueba después de asignarlos que quedaron así (si no, el adaptador no se carga)."""
    for name in EMPTY_LIST_SETTINGS:
        if getattr(module, name, None) != []:
            raise HardeningError(name)
    for name, value in LITELLM_SETTINGS.items():
        if getattr(module, name, object()) != value:
            raise HardeningError(name)


def _litellm_loggers() -> list[logging.Logger]:
    names = [name for name in logging.root.manager.loggerDict if name.startswith("LiteLLM")]
    return [logging.getLogger(name) for name in sorted(names)]


def quiet_litellm_loggers() -> None:
    """Sin handlers propios (escriben en stdout): pasan por los del motor (stderr, con
    redacción, ADR 0013), y solo desde `WARNING`."""
    for logger in _litellm_loggers():
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.WARNING)
        logger.disabled = False


def check_litellm_loggers() -> None:
    for logger in _litellm_loggers():
        if logger.handlers or not logger.propagate or logger.level != logging.WARNING:
            raise HardeningError(logger.name)


def official_api_base(provider: Provider) -> str:
    """`api_base` de la tabla fija; comprueba `https`, host exacto y sin puerto."""
    url = API_BASE[provider]
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != OFFICIAL_HOSTS[provider] or parts.port:
        raise HardeningError("api_base no oficial")
    return url
