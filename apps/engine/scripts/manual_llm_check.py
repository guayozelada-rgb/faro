"""Comprobación MANUAL de HTTPS con la clave real del usuario (ADR 0015 §6 criterio 8, F1b T2).

Solo la ejecuta el usuario, en su equipo, con su clave. Nunca en la CI ni por un agente.

Qué hace, para un proveedor (`anthropic`, `openai` o `gemini`):

1. Pide la clave con `getpass` (no se muestra, no va en argumentos ni variables de entorno,
   no se lee del llavero y no se guarda en ningún archivo).
2. Lista los modelos del proveedor (GET, **sin costo**) con httpx dos veces: con las raíces
   de `certifi` y con el almacén del sistema (`truststore`). Así se ve si el antivirus
   que intercepta HTTPS rompe `certifi` y si `truststore` lo resuelve.
3. Con `--completion <modelo>`, hace además **una** llamada mínima con LiteLLM
   (`max_tokens=5`, costo de fracciones de centavo) con el almacén del sistema, para probar
   el transporte que usará el motor.

Solo muestra códigos de estado, nombres de error y cuántos modelos devolvió: nunca la clave,
ni las respuestas, ni los mensajes de error del proveedor.

Uso, desde `apps/engine`:

    uv run python scripts/manual_llm_check.py anthropic
    uv run python scripts/manual_llm_check.py openai --completion openai/<modelo barato>
    uv run python scripts/manual_llm_check.py gemini --completion gemini/<modelo barato>

Antes de ejecutarlo, cierra otras terminales con variables `SSL_CERT_FILE`, `SSL_VERIFY` o
`REQUESTS_CA_BUNDLE`: el script las ignora a propósito para medir lo que vería el motor.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import ssl
import sys
from typing import Any, Final

PROVIDERS: Final = {
    "anthropic": ("https://api.anthropic.com/v1/models", "x-api-key"),
    "openai": ("https://api.openai.com/v1/models", "authorization"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models", "x-goog-api-key"),
}
TIMEOUT_S: Final = 30


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _clean_environment() -> None:
    """Lo mismo que hará el motor: sin anulaciones de TLS ni trazas de LangSmith."""
    for name in ("SSL_VERIFY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "SSLKEYLOGFILE"):
        os.environ.pop(name, None)
    for name in list(os.environ):
        if name.startswith(("LANGSMITH_", "LANGCHAIN_")):
            del os.environ[name]
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"


def _headers(provider: str, key: str) -> dict[str, str]:
    _, header = PROVIDERS[provider]
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

    url, _ = PROVIDERS[provider]
    if store == "certifi":
        verify: Any = True  # httpx usa certifi por defecto
    else:
        import truststore

        verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        with httpx.Client(verify=verify, timeout=TIMEOUT_S, follow_redirects=False) as client:
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


async def completion(model: str, key: str) -> None:
    import logging

    import truststore

    truststore.inject_into_ssl()
    import litellm

    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    litellm.cache = None
    litellm.turn_off_message_logging = True
    litellm.suppress_debug_info = True
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("LiteLLM"):
            logging.getLogger(name).setLevel(logging.ERROR)
    from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

    try:
        response = await litellm.acompletion(
            model=model,
            api_key=key,
            messages=[{"role": "user", "content": "Responde solo: ok"}],
            max_tokens=5,
            num_retries=0,
            timeout=60,
        )
        usage = response.usage
        _say(
            f"  [litellm   ] OK (tokens: {usage.prompt_tokens} de entrada, "
            f"{usage.completion_tokens} de salida)"
        )
    except Exception as exc:  # noqa: BLE001 - se informa solo el tipo
        _say(f"  [litellm   ] FALLO: {_describe(exc)}")
    finally:
        await close_litellm_async_clients()  # type: ignore[no-untyped-call]
        litellm.in_memory_llm_clients_cache.flush_cache()  # type: ignore[no-untyped-call]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comprobación manual de HTTPS (criterio 8).")
    parser.add_argument("provider", choices=sorted(PROVIDERS))
    parser.add_argument("--completion", metavar="MODELO", help="p. ej. anthropic/<id>")
    args = parser.parse_args(argv)
    if args.completion and not args.completion.startswith(f"{args.provider}/"):
        parser.error(f"--completion debe empezar por '{args.provider}/'")

    _clean_environment()
    key = getpass.getpass(f"Clave de {args.provider} (no se mostrará): ").strip()
    if not key:
        _say("Sin clave: no se hace nada.")
        return 2
    try:
        _say(f"{args.provider}: listado de modelos (sin costo)")
        list_models(args.provider, key, "certifi")
        list_models(args.provider, key, "truststore")
        if args.completion:
            _say(f"{args.provider}: una llamada mínima con LiteLLM ({args.completion})")
            asyncio.run(completion(args.completion, key))
    finally:
        del key
    _say("Copia estas líneas en el informe de T2 (no contienen la clave).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
