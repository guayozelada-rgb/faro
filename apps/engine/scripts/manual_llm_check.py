"""Comprobación MANUAL de HTTPS con la clave real del usuario (ADR 0015 §6 criterio 8, F1b T2).

Solo la ejecuta el usuario, en su equipo, con su clave. Nunca en la CI ni por un agente.

Qué hace, para un proveedor (`anthropic`, `openai` o `gemini`):

1. Limpia el entorno del proceso para que nada desvíe la clave a otro host ni debilite TLS.
   Quita (sin distinguir mayúsculas de minúsculas, como hace Windows):
   - todas las variables que empiezan por `SSL_` (p. ej. `SSL_VERIFY`, `SSL_CERT_FILE`,
     `SSL_CERT_DIR`, `SSL_CERTIFICATE`, `SSL_SECURITY_LEVEL`, `SSL_ECDH_CURVE`), además de
     `SSLKEYLOGFILE`, `REQUESTS_CA_BUNDLE` y `CURL_CA_BUNDLE`;
   - los proxies `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` y `NO_PROXY` (también
     `http_proxy`, `https_proxy`, `all_proxy` y `no_proxy`);
   - `DISABLE_AIOHTTP_TRANSPORT`, las que empiezan por `AIOHTTP_` (p. ej.
     `AIOHTTP_TRUST_ENV`) y `CUSTOM_TIKTOKEN_CACHE_DIR`;
   - `GOOGLE_API_KEY` y las que empiezan por `LANGSMITH_`, `LANGCHAIN_`, `LITELLM_`,
     `OPENAI_`, `ANTHROPIC_` y `GEMINI_` (incluidas las que cambian el host de destino,
     como `OPENAI_BASE_URL`), salvo las `LITELLM_*` que fija el propio script.
   El proxy del registro de Windows no es una variable: lo evita el cliente propio
   (`trust_env=False`, puntos 3 y 4).
2. Pide la clave con `getpass`: no se muestra, no va en argumentos ni variables de entorno,
   no se lee del llavero, no se escribe en ningún archivo y no se conserva tras salir el
   proceso.
3. Lista los modelos del proveedor (GET, **sin costo**) en su host oficial con httpx dos
   veces: con las raíces de `certifi` y con el almacén del sistema (`truststore`). Así se ve
   si el antivirus que intercepta HTTPS rompe `certifi` y si `truststore` lo resuelve.
   El cliente no usa proxies ni CA del entorno ni del registro (`trust_env=False`) y no
   sigue redirecciones.
4. Con `--completion <modelo>`, hace además **una** llamada mínima con LiteLLM
   (`max_tokens=5`, costo de fracciones de centavo) con el almacén del sistema, para probar
   el transporte que usará el motor. LiteLLM se importa en modo `PRODUCTION` (sin leer
   ningún `.env`), el entorno se vuelve a limpiar y comprobar después del import, el
   directorio de trabajo sale de `sys.path` (LiteLLM lo añade al importarse y tiktoken
   ejecutaría cualquier `tiktoken_ext/*.py` que hubiera ahí) y la llamada lleva un
   `api_base` fijo al host oficial y la clave explícita. LiteLLM recibe un cliente HTTP
   propio (parámetro `client`) con `follow_redirects=False`, `trust_env=False` y el
   almacén del sistema: un 307 del host oficial hacia otro origen no reenvía la clave
   (LiteLLM sigue redirecciones por defecto y no quita `x-api-key` ni `x-goog-api-key`)
   y ningún proxy del entorno o del registro de Windows la recibe.

Solo muestra códigos de estado, nombres de error y cuántos modelos devolvió: nunca la clave,
ni las respuestas, ni los mensajes de error del proveedor.

Uso, desde `apps/engine`:

    uv run python scripts/manual_llm_check.py anthropic
    uv run python scripts/manual_llm_check.py openai --completion openai/<modelo barato>
    uv run python scripts/manual_llm_check.py gemini --completion gemini/<modelo barato>
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import ssl
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Provider:
    host: str  # único host al que puede ir la clave
    models_url: str  # listado de modelos (GET, sin costo)
    key_header: str  # cabecera que lleva la clave
    api_base: str  # `api_base` fijo para LiteLLM (nunca el del entorno)


PROVIDERS: Final = {
    "anthropic": Provider(
        host="api.anthropic.com",
        models_url="https://api.anthropic.com/v1/models",
        key_header="x-api-key",
        api_base="https://api.anthropic.com",  # LiteLLM añade /v1/messages
    ),
    "openai": Provider(
        host="api.openai.com",
        models_url="https://api.openai.com/v1/models",
        key_header="authorization",
        api_base="https://api.openai.com/v1",  # LiteLLM añade /chat/completions
    ),
    "gemini": Provider(
        host="generativelanguage.googleapis.com",
        models_url="https://generativelanguage.googleapis.com/v1beta/models",
        key_header="x-goog-api-key",
        # LiteLLM añade /models/<id>:generateContent (AI Studio; la clave va en cabecera).
        api_base="https://generativelanguage.googleapis.com/v1beta",
    ),
}
TIMEOUT_S: Final = 30
COMPLETION_TIMEOUT_S: Final = 60
EXIT_UNSAFE_ENV: Final = 3

# Variables que se fijan antes de importar LiteLLM y son las únicas `LITELLM_*` permitidas.
# PRODUCTION: `import litellm` no llama a `load_dotenv()` (en DEV busca un `.env` hacia
# arriba y reintroduce variables ya limpiadas). Mapa de precios local: sin descargas.
REQUIRED_ENV: Final = {"LITELLM_MODE": "PRODUCTION", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
# Se comparan en mayúsculas: `https_proxy` y `HTTPS_PROXY` son la misma entrada.
SCRUBBED_NAMES: Final = frozenset(
    {
        "SSLKEYLOGFILE",  # escribe las claves de sesión TLS en un archivo
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",  # httpx (con `trust_env`), aiohttp (`AIOHTTP_TRUST_ENV`) y requests
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "DISABLE_AIOHTTP_TRANSPORT",  # cambia a httpx con los proxies del entorno
        "CUSTOM_TIKTOKEN_CACHE_DIR",  # vocabulario de tiktoken desde otra carpeta
        "GOOGLE_API_KEY",  # LiteLLM la usa como clave de Gemini si falta la explícita
    }
)
# `SSL_*`: `SSL_VERIFY`, `SSL_CERT_FILE`, `SSL_CERTIFICATE`, `SSL_SECURITY_LEVEL`…
# `AIOHTTP_*`: `AIOHTTP_TRUST_ENV` activa los proxies del entorno en aiohttp.
# Incluye `*_API_KEY`, `*_API_BASE` y `*_BASE_URL`: cambiarían la clave o el host de destino.
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


class UnsafeEnvironmentError(RuntimeError):
    """Queda en el entorno una variable que podría desviar la clave o debilitar TLS."""


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _is_scrubbed(name: str) -> bool:
    upper = name.upper()  # en Windows el entorno no distingue mayúsculas
    if upper in REQUIRED_ENV:
        return False
    return upper in SCRUBBED_NAMES or upper.startswith(SCRUBBED_PREFIXES)


def unsafe_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """Nombres que no deberían estar, o variables obligatorias con otro valor."""
    env = os.environ if environ is None else environ
    found = sorted(name for name in env if _is_scrubbed(name))
    found += sorted(name for name, value in REQUIRED_ENV.items() if env.get(name) != value)
    return found


def clean_environment() -> None:
    """Quita del entorno lo que podría desviar la clave o debilitar TLS y fija `REQUIRED_ENV`."""
    for name in list(os.environ):
        if _is_scrubbed(name):
            del os.environ[name]
    os.environ.update(REQUIRED_ENV)


def check_environment() -> None:
    leftovers = unsafe_variables()
    if leftovers:
        raise UnsafeEnvironmentError(", ".join(leftovers))


def _official_url(provider: str, url: str) -> str:
    """Solo HTTPS al host oficial del proveedor; cualquier otra cosa es un error de código."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != PROVIDERS[provider].host or parts.port:
        raise UnsafeEnvironmentError(f"destino no oficial para {provider}")
    return url


def _headers(provider: str, key: str) -> dict[str, str]:
    header = PROVIDERS[provider].key_header
    if header == "authorization":
        return {"authorization": f"Bearer {key}"}
    headers = {header: key}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _describe(exc: BaseException) -> str:
    """Nombre de la excepción y, si es TLS, el motivo; nunca el texto del proveedor."""
    names: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(names) < 4:
        names.append(type(current).__name__)
        if isinstance(current, ssl.SSLCertVerificationError):
            names.append(f"verify_code={current.verify_code}")
        current = current.__cause__ or current.__context__
    return " <- ".join(names)


def list_models(provider: str, key: str, store: str) -> None:
    import httpx

    url = _official_url(provider, PROVIDERS[provider].models_url)
    if store == "certifi":
        verify: Any = True  # httpx usa certifi por defecto
    else:
        import truststore

        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        # `trust_env=False`: ni proxies (tampoco el del registro de Windows, que `getproxies()`
        # lee si no hay variables) ni CA del entorno, como el cliente del motor.
        with httpx.Client(
            verify=verify, timeout=TIMEOUT_S, follow_redirects=False, trust_env=False
        ) as client:
            response = client.get(url, headers=_headers(provider, key))
    except httpx.HTTPError as exc:
        _say(f"  [{store:10s}] FALLO de conexión: {_describe(exc)}")
        return
    count = "?"
    if response.status_code == 200:
        data = response.json()
        models = data.get("data") or data.get("models") or []
        count = str(len(models))
    _say(f"  [{store:10s}] HTTP {response.status_code} (modelos listados: {count})")


def drop_cwd_from_sys_path() -> None:
    """Quita de `sys.path` el directorio de trabajo (también `""`, que significa lo mismo).

    `import litellm` ejecuta `sys.path.append(os.getcwd())` (`litellm/proxy/proxy_cli.py`) y
    tiktoken importa todo `tiktoken_ext/*.py` que encuentre en `sys.path`: un archivo puesto
    en el directorio de trabajo se ejecutaría dentro del proceso que tiene la clave.
    """
    cwd = Path.cwd().resolve()
    sys.path[:] = [entry for entry in sys.path if entry and Path(entry).resolve() != cwd]


def import_litellm() -> Any:
    """Importa LiteLLM endurecido: entorno limpio antes **y** después del import.

    `truststore` se inyecta antes: LiteLLM guarda al importarse un contexto TLS con certifi.
    Si tras el import queda alguna variable peligrosa, lanza `UnsafeEnvironmentError`.
    Después del import, el directorio de trabajo sale de `sys.path` (`drop_cwd_from_sys_path`).
    """
    import truststore

    clean_environment()
    check_environment()
    truststore.inject_into_ssl()
    import litellm

    drop_cwd_from_sys_path()
    clean_environment()
    check_environment()
    litellm.api_base = None
    litellm.api_key = None
    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    litellm.input_callback = []
    litellm.service_callback = []
    litellm.cache = None
    litellm.turn_off_message_logging = True
    litellm.suppress_debug_info = True
    litellm.log_raw_request_response = False
    litellm.redact_messages_in_exceptions = True
    litellm.disable_hf_tokenizer_download = True
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("LiteLLM"):  # su handler escribe en stdout
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = False
            logger.disabled = True
    return litellm


def _safe_transport() -> Any:
    """Transporte httpx con el almacén del sistema y sin nada del entorno ni del registro."""
    import httpx
    import truststore

    return httpx.AsyncHTTPTransport(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), trust_env=False
    )


def build_litellm_client(provider: str, key: str, api_base: str) -> Any:
    """Cliente propio para el parámetro `client` de `litellm.acompletion` (LiteLLM 1.104.0).

    Sin él, LiteLLM crea su cliente con `follow_redirects=True` (`AsyncHTTPHandler`,
    `http_handler.py:618`, y el `httpx.AsyncClient` de OpenAI) y, con
    `DISABLE_AIOHTTP_TRANSPORT` o `AIOHTTP_TRUST_ENV`, usa los proxies del entorno: un 307
    del host oficial a otro origen recibiría `x-api-key` o `x-goog-api-key` (httpx solo
    quita `authorization` al cambiar de origen). Con este cliente:

    - `follow_redirects=False`: un 3xx es un error y no se envía nada a otro sitio;
    - `trust_env=False` en el cliente y en el transporte: ni proxies (tampoco el del registro
      de Windows), ni `SSL_CERT_FILE`, ni `.netrc`;
    - verificación TLS con `truststore` (almacén del sistema), sin aiohttp.

    Anthropic y Gemini aceptan un `AsyncHTTPHandler` con `transport` propio (también se usa
    si LiteLLM recrea el cliente tras un error de conexión). OpenAI exige un `AsyncOpenAI`,
    que se construye con la clave, el `api_base` fijo y el `httpx.AsyncClient` propio.
    Hay que cerrarlo con `close_litellm_client`.
    """
    if provider == "openai":
        import httpx
        import openai

        http_client = httpx.AsyncClient(
            transport=_safe_transport(),
            follow_redirects=False,
            trust_env=False,
            timeout=COMPLETION_TIMEOUT_S,
        )
        return openai.AsyncOpenAI(
            api_key=key,
            base_url=api_base,
            http_client=http_client,
            max_retries=0,
            timeout=COMPLETION_TIMEOUT_S,
        )
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    return AsyncHTTPHandler(
        timeout=COMPLETION_TIMEOUT_S, transport=_safe_transport(), follow_redirects=False
    )


async def close_litellm_client(client: Any) -> None:
    await client.close()


async def send_completion(litellm: Any, provider: str, model: str, key: str, api_base: str) -> Any:
    """`acompletion` con el cliente propio. Las pruebas la llaman con un `api_base` en loopback."""
    client = build_litellm_client(provider, key, api_base)
    try:
        return await litellm.acompletion(
            model=model,
            api_base=api_base,
            api_key=key,
            client=client,
            messages=[{"role": "user", "content": "Responde solo: ok"}],
            max_tokens=5,
            num_retries=0,
            timeout=COMPLETION_TIMEOUT_S,
        )
    finally:
        await close_litellm_client(client)


async def completion(provider: str, model: str, key: str) -> bool:
    """Una llamada mínima. `False` si se abortó antes de enviar nada."""
    try:
        litellm = import_litellm()
    except UnsafeEnvironmentError as exc:
        _say(f"  [litellm   ] ABORTADO: variables de entorno no permitidas ({exc})")
        return False
    from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

    api_base = _official_url(provider, PROVIDERS[provider].api_base)
    try:
        response = await send_completion(litellm, provider, model, key, api_base)
        usage = response.usage
        _say(
            f"  [litellm   ] OK (tokens: {usage.prompt_tokens} de entrada, "
            f"{usage.completion_tokens} de salida)"
        )
    except Exception as exc:  # noqa: BLE001 - se informa solo el tipo
        _say(f"  [litellm   ] FALLO: {_describe(exc)}")
    finally:
        await close_litellm_async_clients()  # type: ignore[no-untyped-call]
        litellm.in_memory_llm_clients_cache.flush_cache()
    return True


def main(argv: list[str] | None = None, *, read_key: Callable[[str], str] | None = None) -> int:
    """`read_key` solo lo cambian las pruebas (clave falsa por stdin); por defecto, `getpass`."""
    parser = argparse.ArgumentParser(description="Comprobación manual de HTTPS (criterio 8).")
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    parser.add_argument("--completion", metavar="MODELO", help="p. ej. anthropic/<id>")
    args = parser.parse_args(argv)
    if args.completion and not args.completion.startswith(f"{args.provider}/"):
        parser.error(f"--completion debe empezar por '{args.provider}/'")

    clean_environment()
    try:
        check_environment()
    except UnsafeEnvironmentError as exc:
        _say(f"ABORTADO: variables de entorno no permitidas ({exc})")
        return EXIT_UNSAFE_ENV
    prompt = f"Clave de {args.provider} (no se mostrará): "
    key = (read_key or getpass.getpass)(prompt).strip()
    if not key:
        _say("Sin clave: no se hace nada.")
        return 2
    try:
        _say(f"{args.provider}: listado de modelos (sin costo)")
        list_models(args.provider, key, "certifi")
        list_models(args.provider, key, "truststore")
        if args.completion:
            _say(f"{args.provider}: una llamada mínima con LiteLLM ({args.completion})")
            if not asyncio.run(completion(args.provider, args.completion, key)):
                return EXIT_UNSAFE_ENV
    finally:
        # `del` solo suelta la referencia: las `str` son inmutables y su contenido no se
        # borra de la memoria. La clave no se conserva tras salir el proceso.
        del key
    _say("Copia estas líneas en el informe de T2 (no contienen la clave).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
