"""Adaptador de producción sobre LiteLLM 1.104.0: **única** importación de `litellm`.

ADR 0015 §1, skill `capa-llm` §2 y condiciones del informe de T2 (§12). Se importa de forma
perezosa en la primera llamada real (`service.LazyLiteLlmClient`), nunca antes de `ready`:
importar LiteLLM tarda unos 6,5 s. Al importarse aplica, en este orden:

1. `cl100k_base` incluido y con su SHA-256 (`hardening.verify_tiktoken_file`).
2. Entorno limpio y comprobado **antes** del import (condición 1).
3. `install_system_trust_for_libraries()` (condiciones 1 y 20, ADR 0012 condición 13):
   LiteLLM y aiohttp crean al importarse contextos TLS compartidos; así son la subclase
   con cerrojo del motor y verifican con el almacén del sistema. Es la segunda barrera: lo
   que protege es el contexto explícito del cliente propio (punto 7).
4. `import litellm` y lo que hace falta de él.
5. Directorio de trabajo fuera de `sys.path` (condición 14) y entorno limpio y comprobado
   **después** del import (condición 2: un `.env` de un directorio padre no reintroduce
   nada).
6. Ajustes de LiteLLM y loggers (condiciones 4 y 5), comprobados tras asignarlos.

Si algo falla, el módulo lanza la excepción y no se carga: nunca se llama a nadie.

Cada llamada (`LiteLlmClient.complete`):

7. **Cliente HTTP propio** (condiciones 15, 18 y 19): sin redirecciones
   (`follow_redirects=False`), sin proxies ni CA del entorno ni del registro de Windows
   (`trust_env=False`) y con el contexto TLS único del motor (`tls_context()`, ADR 0012).
   Anthropic y Gemini: `AsyncHTTPHandler` con `transport` propio; OpenAI: `AsyncOpenAI` con
   `http_client` propio y `max_retries=0`. Solo `acompletion` (nunca `completion`
   síncrono, `litellm.ssl_verify`, `aclient_session` ni clientes que LiteLLM construya).
8. `api_base` fijo al host oficial y `api_key` explícita (condición 3); `num_retries=0`
   (los reintentos son de Faro); modelo siempre del catálogo.
9. En el `finally`, también con error o cancelación: se cierra el cliente y se vacía la
   caché de clientes de LiteLLM (condición 6; con OpenAI guarda la clave en claro).

La respuesta de LiteLLM no sale de aquí: `raw_from_response` copia texto, llamadas a
herramientas, `finish_reason` y tokens. Las excepciones salen como `LlmCallError` (solo el
tipo de fallo; nunca el cuerpo ni el mensaje del proveedor).
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import structlog

from faro_engine.llm import hardening
from faro_engine.llm.client import RawCompletion, ResolvedCall, raw_from_response
from faro_engine.llm.errors import LlmCallError, classify_exception
from faro_engine.net.tls import install_system_trust_for_libraries, tls_context

log = structlog.get_logger(__name__)

hardening.verify_tiktoken_file()
hardening.clean_environment()
hardening.check_environment()
install_system_trust_for_libraries()

import litellm  # noqa: E402 - única importación de litellm en todo el motor
import openai  # noqa: E402
from litellm.llms.custom_httpx.async_client_cleanup import (  # noqa: E402
    close_litellm_async_clients,
)
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler  # noqa: E402

hardening.drop_cwd_from_sys_path()
hardening.clean_environment()
hardening.check_environment()
hardening.apply_litellm_settings(litellm)
hardening.check_litellm_settings(litellm)
hardening.quiet_litellm_loggers()
hardening.check_litellm_loggers()

TIMEOUT: Final = hardening.CALL_TIMEOUT_SECONDS


def _transport() -> httpx.AsyncHTTPTransport:
    """Sin proxies (tampoco del registro) ni CA del entorno; HTTP/1.1 (ADR 0012, cond. 9)."""
    return httpx.AsyncHTTPTransport(verify=tls_context(), trust_env=False)


def build_client(provider: str, api_key: str) -> Any:
    """Cliente propio de una llamada (parámetro `client` de `acompletion`)."""
    if provider == "openai":
        return openai.AsyncOpenAI(
            api_key=api_key,
            base_url=hardening.API_BASE["openai"],
            max_retries=0,
            timeout=TIMEOUT,
            http_client=httpx.AsyncClient(
                transport=_transport(), follow_redirects=False, trust_env=False, timeout=TIMEOUT
            ),
        )
    return AsyncHTTPHandler(timeout=TIMEOUT, transport=_transport(), follow_redirects=False)


async def drop_client_cache() -> None:
    """Vacía la caché de clientes de LiteLLM (`in_memory_llm_clients_cache`): con OpenAI
    guarda un `AsyncOpenAI` con la clave y la clave en claro dentro de su clave de caché."""
    await close_litellm_async_clients()  # type: ignore[no-untyped-call]
    litellm.in_memory_llm_clients_cache.flush_cache()


class LiteLlmClient:
    """`LlmClient` de producción. Una instancia por motor; sin estado entre llamadas."""

    @property
    def requires_key(self) -> bool:
        return True

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        if not api_key:
            raise LlmCallError("invalid_key")
        api_base = hardening.official_api_base(call.provider)
        extra: dict[str, Any] = {}
        if call.response_format is not None:
            extra["response_format"] = dict(call.response_format)
        if call.tools:
            extra["tools"] = [dict(tool) for tool in call.tools]
        client = build_client(call.provider, api_key)
        failure: LlmCallError | None = None
        response: Any = None
        try:
            response = await litellm.acompletion(
                model=call.litellm_model,
                api_base=api_base,
                api_key=api_key,
                client=client,
                messages=[dict(message) for message in call.messages],
                max_tokens=call.max_output_tokens,
                timeout=TIMEOUT,
                num_retries=0,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 - se traduce a `LlmCallError` sin su contenido
            failure = classify_exception(exc)
            # Solo el nombre de la clase y el tipo de fallo; nunca el mensaje.
            log.info("llm.adapter_error", error_type=type(exc).__name__, kind=failure.kind)
        finally:
            await client.close()
            await drop_client_cache()
        if failure is not None:
            # Fuera del `except`: la excepción original (con el texto del proveedor) no queda
            # encadenada en `__context__`.
            raise failure
        return raw_from_response(response)
