"""Corre `scripts/manual_llm_check.py` en un proceso aparte con la red bloqueada salvo loopback.

Se lanza con `python -c` (ver `tests/llm/test_manual_llm_check.py`) para que `__main__` no
tenga `__file__`: así `python-dotenv`, si LiteLLM llegara a llamarlo, buscaría el `.env`
desde el directorio de trabajo hacia arriba, y la prueba puede ponerlo en un directorio
padre temporal. Lee la clave falsa de stdin en vez de `getpass` y escribe una línea
`FARO_PROBE {json}` en stdout con lo que observó.

Escenarios:
- `run <args del script>`: ejecuta `main` del script.
- `control_dotenv`: importa LiteLLM sin forzar `LITELLM_MODE` (motivo de la prueba del `.env`).
- `client_isolation`: `send_completion` del script (cliente propio) contra un "host oficial"
  falso en loopback que responde 307 hacia otro puerto, con proxies en el entorno.
- `control_client`: lo mismo con el cliente por defecto de LiteLLM (motivo de las pruebas).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.deps.offline_probe import ATTEMPTS, FAKE_KEYS, MODELS, _stub_encoding, block_network

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "manual_llm_check.py"
DOTENV_MARKER = "FARO_DOTENV_PROBE"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manual_llm_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses lo necesitan al definir la clase
    spec.loader.exec_module(module)
    return module


def _run(args: list[str]) -> dict[str, Any]:
    script = load_script()
    code = script.main(args, read_key=lambda _prompt: sys.stdin.readline())
    return {"code": code, "unsafe_after": script.unsafe_variables()}


def _control_dotenv() -> dict[str, Any]:
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm  # noqa: F401 - en modo DEV (por defecto) llama a load_dotenv()

    return {}


class _Recorder(BaseHTTPRequestHandler):
    """Anota quién recibió qué; si `redirect_to` tiene valor, responde 307 hacia allí."""

    redirect_to: str | None = None
    hits: list[dict[str, str]]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002 - heredada
        return

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.hits.append({k.lower(): v for k, v in self.headers.items()} | {":path": self.path})
        if self.redirect_to is not None:
            self.send_response(307)
            self.send_header("location", self.redirect_to + self.path)
        else:
            self.send_response(500)
        self.send_header("content-length", "0")
        self.end_headers()

    do_GET = do_POST  # noqa: N815 - nombre de BaseHTTPRequestHandler


@contextlib.contextmanager
def _recorder(redirect_to: str | None = None) -> Iterator[tuple[int, list[dict[str, str]]]]:
    hits: list[dict[str, str]] = []
    handler = type("Recorder", (_Recorder,), {"redirect_to": redirect_to, "hits": hits})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), hits
    finally:
        server.shutdown()
        server.server_close()


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _keys_in(hits: list[dict[str, str]], key: str) -> list[str]:
    return sorted({name for hit in hits for name, value in hit.items() if key in value})


def _attempt(
    script: ModuleType, litellm: Any, provider: str, *, own_client: bool, proxies: bool
) -> dict[str, Any]:
    """Una llamada a un "host oficial" falso que redirige con 307 a otro puerto.

    Con `proxies`, el entorno apunta todos los proxies a un tercer servidor en loopback y
    activa las variables que hacen que LiteLLM los use.
    """
    model, path = MODELS[provider]
    key = FAKE_KEYS[provider]
    result: dict[str, Any] = {}
    with (
        _recorder() as (proxy_port, proxy_hits),
        _recorder() as (other_port, other_hits),
        _recorder(redirect_to=f"http://127.0.0.1:{other_port}") as (official_port, official_hits),
    ):
        if proxies:
            os.environ.update(dict.fromkeys(_PROXY_VARS, f"http://127.0.0.1:{proxy_port}"))
            os.environ["DISABLE_AIOHTTP_TRANSPORT"] = "True"  # httpx con proxies del entorno
            os.environ["AIOHTTP_TRUST_ENV"] = "True"
        api_base = f"http://127.0.0.1:{official_port}{path}"

        async def run() -> None:
            from litellm.llms.custom_httpx.async_client_cleanup import (
                close_litellm_async_clients,
            )

            try:
                if own_client:
                    await script.send_completion(litellm, provider, model, key, api_base)
                else:
                    await litellm.acompletion(
                        model=model,
                        api_base=api_base,
                        api_key=key,
                        messages=[{"role": "user", "content": "hola"}],
                        max_tokens=5,
                        num_retries=0,
                        timeout=10,
                    )
            except Exception as exc:  # noqa: BLE001 - la prueba decide con el informe
                result["error"] = type(exc).__name__
            finally:
                await close_litellm_async_clients()  # type: ignore[no-untyped-call]
                litellm.in_memory_llm_clients_cache.flush_cache()

        try:
            asyncio.run(run())
        finally:
            for name in (*_PROXY_VARS, "DISABLE_AIOHTTP_TRANSPORT", "AIOHTTP_TRUST_ENV"):
                os.environ.pop(name, None)
        result["official_key_headers"] = _keys_in(official_hits, key)
        result["other_hits"] = len(other_hits)
        result["other_key_headers"] = _keys_in(other_hits, key)
        result["proxy_hits"] = len(proxy_hits)
        result["proxy_key_headers"] = _keys_in(proxy_hits, key)
    return result


def _client_scenario(*, own_client: bool) -> dict[str, Any]:
    """Con el cliente propio basta una pasada con proxies: si el 307 se siguiera o se usara
    el proxy, lo anotarían el otro puerto o el proxy. El control separa los dos motivos."""
    script = load_script()
    litellm = script.import_litellm()
    _stub_encoding()  # sin vocabulario `cl100k_base` ni red (ver `offline_probe`)
    providers = ("openai", "anthropic", "gemini")
    modes = {"proxies": True} if own_client else {"redirect": False, "proxies": True}
    return {
        mode: {
            provider: _attempt(script, litellm, provider, own_client=own_client, proxies=proxies)
            for provider in providers
        }
        for mode, proxies in modes.items()
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    block_network()
    if args[0] == "control_dotenv":
        report = _control_dotenv()
    elif args[0] == "client_isolation":
        report = _client_scenario(own_client=True)
    elif args[0] == "control_client":
        report = _client_scenario(own_client=False)
    else:
        report = _run(args[1:])
    report["dotenv_marker"] = os.environ.get(DOTENV_MARKER)
    report["attempts"] = sorted(set(ATTEMPTS))
    sys.stdout.write("FARO_PROBE " + json.dumps(report, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0
