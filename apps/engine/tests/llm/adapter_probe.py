"""Sonda del adaptador real (`faro_engine/llm/litellm_client.py`) en un proceso aparte.

Importar el adaptador cambia el estado global del proceso (entorno, `sys.path`,
`ssl.SSLContext` inyectado, loggers y ajustes de LiteLLM) y tarda unos 7 s: por eso cada
escenario corre en otro proceso, con la red bloqueada salvo loopback **antes** del import
(`tests.deps.offline_probe.block_network`). Escribe una sola línea `FARO_PROBE {json}` en
stdout con lo observado; las pruebas de `test_litellm_adapter.py` deciden con ella.

Se lanza con `python -c` desde un directorio de trabajo temporal (sin `__file__` en
`__main__`, como `tests/deps/manual_check_probe.py`): así un `.env` en el directorio padre
y un `tiktoken_ext/*.py` en el de trabajo serían visibles si el endurecimiento fallara.

Escenarios:

- `matrix`: con `respx`, respuestas grabadas de los tres proveedores (éxito y cada fallo
  del mapeo de `llm/errors.py`) contra el host oficial fijo. Anota el tipo de fallo, el
  `Retry-After`, la URL y la cabecera en la que viajó la clave, y que la caché de clientes
  queda vacía y ningún diccionario vivo guarda la clave.
- `trap`: sin `respx`. Variables `*_BASE_URL`, proxies y un `.env` del directorio padre
  apuntan a una trampa en loopback; `LITELLM_LOG=DEBUG`; `SSLKEYLOGFILE`; un
  `tiktoken_ext/faro_trap.py` en el directorio de trabajo. Solo puede intentarse el host
  oficial (la red bloqueada lo corta).
- `tls_spy`: condición 21 del informe de T2. Las llamadas de los tres proveedores
  completan un handshake TLS real en una red de httpcore en memoria; se espían `wrap_bio`,
  `wrap_socket` y `do_handshake`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import gc
import json
import os
import socket
import ssl
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from tests.deps.offline_probe import ATTEMPTS, block_network

# Claves falsas evidentes, construidas por partes (gitleaks).
KEYS = {
    "openai": "sk-" + "probe-" + "faro7" * 8,
    "anthropic": "sk-ant-" + "probe-" + "faro8" * 8,
    "gemini": "AIza" + "PROBE" + "faro9" * 7,
}
MODELS = {
    "openai": ("gpt-6-luna", "openai/gpt-6-luna"),
    "anthropic": ("claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
    "gemini": ("gemini-3.5-flash-lite", "gemini/gemini-3.5-flash-lite"),
}
URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent"
    ),
}
HOSTS = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "gemini": "generativelanguage.googleapis.com",
}


def ok_body(provider: str, *, finish: str = "stop") -> dict[str, Any]:
    if provider == "openai":
        return {
            "id": "probe",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-6-luna",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hola"},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        }
    if provider == "anthropic":
        return {
            "id": "probe",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "hola"}],
            "stop_reason": "refusal" if finish == "content_filter" else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 2},
        }
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "hola"}]},
                "finishReason": "SAFETY" if finish == "content_filter" else "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 2, "totalTokenCount": 13},
    }


def error_body(provider: str, status: int, code: str, message: str) -> dict[str, Any]:
    if provider == "anthropic":
        return {"type": "error", "error": {"type": code, "message": message}}
    if provider == "gemini":
        status_name = {400: "INVALID_ARGUMENT", 403: "PERMISSION_DENIED", 429: "RESOURCE_EXHAUSTED"}
        return {
            "error": {
                "code": status,
                "message": message,
                "status": code or status_name.get(status, "INTERNAL"),
            }
        }
    # OpenAI manda `code: null` con `type: invalid_request_error` (p. ej. un 401 por una
    # clave mal copiada): es la forma que LiteLLM 1.104 reescribe como un 400 (T6-C6).
    openai_code = None if code == "invalid_request_error" else code
    return {"error": {"message": message, "type": code, "param": None, "code": openai_code}}


# Casos: nombre → (estado, código del proveedor, mensaje, cabeceras, efecto especial).
CASES: dict[str, tuple[int, str, str, dict[str, str], str | None]] = {
    "ok": (200, "", "", {}, None),
    "content_filter": (200, "", "", {}, "content_filter"),
    "unauthorized": (401, "authentication_error", "invalid x-api-key", {}, None),
    "forbidden": (403, "permission_error", "not allowed", {}, None),
    "rate_limited": (429, "rate_limit_error", "slow down", {"retry-after": "3"}, None),
    "server_error": (500, "api_error", "boom", {}, None),
    "unavailable": (503, "overloaded_error", "busy", {}, None),
    "bad_request": (400, "invalid_request_error", "messages: field required", {}, None),
    "timeout": (0, "", "", {}, "timeout"),
    "connect": (0, "", "", {}, "connect"),
    "redirect": (307, "", "", {}, "redirect"),
}
QUOTA_CASES: dict[str, dict[str, tuple[int, str, str]]] = {
    "openai": {
        "quota": (429, "insufficient_quota", "You exceeded your current quota"),
        # 401 real de OpenAI: `type: invalid_request_error`, `code: null` (T6-C6).
        "bad_key": (
            401,
            "invalid_request_error",
            "You didn't provide an API key. You need to provide your API key in an "
            "Authorization header using Bearer auth.",
        ),
    },
    "anthropic": {
        "quota": (400, "invalid_request_error", "Your credit balance is too low to access the API"),
        "billing": (402, "billing_error", "payment issue"),
    },
    "gemini": {
        "quota": (400, "FAILED_PRECONDITION", "Please enable billing"),
        "bad_key": (400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
    },
}


class Trap:
    """Servidor TCP en loopback que solo cuenta conexiones (lo que no debe recibir nada)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = int(self.sock.getsockname()[1])
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.sock.close()


def resolved(provider: str) -> Any:
    from faro_engine.llm.client import ResolvedCall

    model, litellm_model = MODELS[provider]
    return ResolvedCall(
        provider=provider,  # type: ignore[arg-type]
        tier="economy",
        model=model,
        litellm_model=litellm_model,
        prompt_id="probe.call",
        messages=({"role": "user", "content": "hola"},),
        max_output_tokens=16,
    )


async def attempt(client: Any, provider: str) -> dict[str, Any]:
    return await attempt_call(client, resolved(provider), provider)


async def attempt_call(client: Any, call: Any, provider: str) -> dict[str, Any]:
    from faro_engine.llm.errors import LlmCallError

    key = "".join(KEYS[provider])  # copia, como la `str` del servicio
    try:
        raw = await client.complete(call, key)
    except LlmCallError as exc:
        return {"kind": exc.kind, "retry_after": exc.retry_after}
    return {
        "text": raw.text,
        "tokens": [raw.tokens_in, raw.tokens_out],
        "finish_reason": raw.finish_reason,
    }


def holders(needle: str) -> int:
    """Diccionarios vivos con la clave (salvo `KEYS`)."""
    count = 0
    for obj in gc.get_objects():
        if obj is KEYS or not isinstance(obj, dict):
            continue
        try:
            values = list(obj.values())
        except RuntimeError:
            continue
        if any(isinstance(v, str) and needle in v for v in values):
            count += 1
    return count


def adapter_state(module: Any) -> dict[str, Any]:
    import litellm

    from faro_engine.llm import hardening

    loggers = [
        logging_state(name)
        for name in sorted(
            n for n in __import__("logging").root.manager.loggerDict if n.startswith("LiteLLM")
        )
    ]
    return {
        "unsafe_env": hardening.unsafe_variables(),
        "cwd_in_sys_path": any(
            (not entry) or Path(entry).resolve() == Path.cwd().resolve() for entry in sys.path
        ),
        "callbacks": [getattr(litellm, name) for name in hardening.EMPTY_LIST_SETTINGS],
        "cache": litellm.cache is None,
        "turn_off_message_logging": litellm.turn_off_message_logging,
        "ssl_context_type": type(ssl.create_default_context()).__name__,
        "loggers": loggers,
        "module": module.__name__,
    }


def logging_state(name: str) -> list[Any]:
    import logging

    logger = logging.getLogger(name)
    return [name, len(logger.handlers), logger.propagate, logger.level]


# --- matrix ------------------------------------------------------------------------------


def scenario_matrix() -> dict[str, Any]:
    import httpx
    import respx

    from faro_engine.llm import litellm_client

    trap = Trap()
    seen: dict[str, list[dict[str, Any]]] = {p: [] for p in KEYS}
    current: dict[str, Any] = {}

    def responder(provider: str) -> Callable[[httpx.Request], httpx.Response]:
        def handle(request: httpx.Request) -> httpx.Response:
            key = KEYS[provider]
            seen[provider].append(
                {
                    "url": str(request.url),
                    "key_headers": sorted(n for n, v in request.headers.items() if key in v),
                    "key_in_url": key in str(request.url),
                    "body": json.loads(request.content or b"{}"),
                }
            )
            status, code, message, headers, effect = current["case"]
            if effect == "timeout":
                raise httpx.ReadTimeout("probe", request=request)
            if effect == "connect":
                raise httpx.ConnectError("probe", request=request)
            if effect == "redirect":
                location = f"http://127.0.0.1:{trap.port}/robado"
                return httpx.Response(307, headers={"location": location})
            if status == 200:
                return httpx.Response(200, json=ok_body(provider, finish=effect or "stop"))
            return httpx.Response(
                status, headers=headers, json=error_body(provider, status, code, message)
            )

        return handle

    results: dict[str, dict[str, Any]] = {p: {} for p in KEYS}
    client = litellm_client.LiteLlmClient()

    async def run() -> None:
        with respx.mock(assert_all_called=False) as mock:
            for provider, url in URLS.items():
                mock.post(url).mock(side_effect=responder(provider))
            for provider in KEYS:
                cases = dict(CASES)
                for name, (status, code, message) in QUOTA_CASES[provider].items():
                    cases[name] = (status, code, message, {}, None)
                for name, case in cases.items():
                    current["case"] = case
                    results[provider][name] = await attempt(client, provider)

    async def run_no_key() -> dict[str, Any]:
        from faro_engine.llm.errors import LlmCallError

        try:
            await client.complete(resolved("openai"), None)
        except LlmCallError as exc:
            return {"kind": exc.kind}
        return {}

    async def run_structured() -> dict[str, Any]:
        """Salida estructurada con los seis modelos del catálogo y una llamada con
        herramientas: solo puede salir hacia los hosts oficiales."""
        from dataclasses import replace

        from faro_engine.llm.catalog import default_catalog
        from faro_engine.llm.client import ResolvedCall

        others: list[str] = []
        outcome: dict[str, Any] = {"models": {}}
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        fmt = {"type": "json_schema", "json_schema": {"name": "S", "schema": schema}}
        current["case"] = CASES["ok"]

        def other(request: httpx.Request) -> httpx.Response:
            others.append(str(request.url.host))
            return httpx.Response(404)

        with respx.mock(assert_all_called=False) as mock:
            for provider, url in URLS.items():
                mock.post(url__startswith=url.split("/models/")[0]).mock(
                    side_effect=responder(provider)
                )
            mock.route().mock(side_effect=other)
            for model in default_catalog().models:
                call = ResolvedCall(
                    provider=model.provider,
                    tier=model.tier,
                    model=model.model,
                    litellm_model=model.litellm_model,
                    prompt_id="probe.structured",
                    messages=({"role": "user", "content": "hola"},),
                    max_output_tokens=16,
                    response_format=fmt,
                )
                result = await attempt_call(client, call, model.provider)
                outcome["models"][model.model] = result
            before = sum(len(v) for v in seen.values())
            tools_call = replace(
                resolved("openai"),
                tools=({"type": "function", "function": {"name": "leer", "parameters": {}}},),
            )
            outcome["tools"] = await attempt_call(client, tools_call, "openai")
            outcome["tools_requests"] = sum(len(v) for v in seen.values()) - before
        outcome["other_hosts"] = others
        outcome["requires_key"] = client.requires_key
        return outcome

    asyncio.run(run())
    no_key_result = asyncio.run(run_no_key())
    structured = asyncio.run(run_structured())
    cleanup = asyncio.run(run_cleanup(client, current, responder))
    import litellm

    gc.collect()
    report = {
        "results": results,
        "no_key": no_key_result,
        "structured": structured,
        "cleanup": cleanup,
        "requests": {p: s[:1] + s[-1:] for p, s in seen.items()},
        "redirect_hits": trap.connections,
        "cache_entries": len(litellm.in_memory_llm_clients_cache.cache_dict),
        "holders": {p: holders(k) for p, k in KEYS.items()},
        "state": adapter_state(litellm_client),
    }
    trap.close()
    return report


async def run_cleanup(
    client: Any, current: dict[str, Any], responder: Callable[[str], Any]
) -> dict[str, Any]:
    """Hallazgo 1 de la revisión de seguridad de T6: un fallo en cada paso de la limpieza
    (cerrar el cliente propio, vaciar la caché, volver a aplicar los ajustes) no sustituye
    la respuesta ni el fallo original, y los pasos siguientes se ejecutan igual. Una
    cancelación durante `close()` sale como cancelación, también tras vaciar la caché."""
    import litellm
    import respx

    from faro_engine.llm import hardening, litellm_client

    real_build = litellm_client.build_client
    real_drop = litellm_client.drop_client_cache
    real_apply = hardening.apply_litellm_settings
    steps: list[str] = []
    close_mode: dict[str, str | None] = {"mode": None}
    in_close: dict[str, asyncio.Event] = {}

    def build(provider: str, api_key: str) -> Any:
        built = real_build(provider, api_key)
        original_close = built.close

        async def close() -> None:
            steps.append("close")
            await original_close()
            if close_mode["mode"] == "raise":
                raise RuntimeError("close " + KEYS[provider])
            if close_mode["mode"] == "cancel":
                in_close["event"].set()
                await asyncio.Event().wait()  # la prueba cancela aquí

        built.close = close
        return built

    async def drop(*, fail: bool = False) -> None:
        steps.append("cache")
        await real_drop()
        if fail:
            raise RuntimeError("cache")

    def apply(module: Any, *, fail: bool = False) -> None:
        steps.append("settings")
        real_apply(module)
        if fail:
            raise RuntimeError("settings")

    async def one(provider: str, case: str, failing: str) -> dict[str, Any]:
        steps.clear()
        current["case"] = CASES[case]
        close_mode["mode"] = "raise" if failing == "close" else None
        litellm_client.drop_client_cache = functools.partial(drop, fail=failing == "cache")
        hardening.apply_litellm_settings = functools.partial(apply, fail=failing == "settings")
        result = await attempt(client, provider)
        return {"result": result, "steps": list(steps)}

    async def cancelled(provider: str) -> dict[str, Any]:
        steps.clear()
        current["case"] = CASES["ok"]
        close_mode["mode"] = "cancel"
        litellm_client.drop_client_cache = drop
        hardening.apply_litellm_settings = apply
        in_close["event"] = asyncio.Event()
        task = asyncio.create_task(attempt(client, provider))
        await in_close["event"].wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            outcome = "cancelled"
        else:
            outcome = "returned"
        return {"outcome": outcome, "steps": list(steps)}

    outcome: dict[str, Any] = {}
    litellm_client.build_client = build
    try:
        with respx.mock(assert_all_called=False) as mock:
            for provider, url in URLS.items():
                mock.post(url).mock(side_effect=responder(provider))
            for provider in ("openai", "anthropic"):
                for failing in ("close", "cache", "settings"):
                    for case in ("ok", "server_error"):
                        name = f"{provider}:{failing}:{case}"
                        outcome[name] = await one(provider, case, failing)
                outcome[f"{provider}:cancel"] = await cancelled(provider)
    finally:
        litellm_client.build_client = real_build
        litellm_client.drop_client_cache = real_drop
        hardening.apply_litellm_settings = real_apply
    hardening.check_litellm_settings(litellm)
    outcome["cache_entries"] = len(litellm.in_memory_llm_clients_cache.cache_dict)
    return outcome


# --- trap --------------------------------------------------------------------------------


def scenario_trap() -> dict[str, Any]:
    from faro_engine.llm import litellm_client

    client = litellm_client.LiteLlmClient()

    async def run() -> dict[str, Any]:
        return {provider: await attempt(client, provider) for provider in KEYS}

    results = asyncio.run(run())
    import tiktoken

    encoding = tiktoken.get_encoding("cl100k_base")
    return {
        "results": results,
        "marker_exists": Path("faro_trap_marker.txt").exists(),
        "encoding_tokens": len(encoding.encode("hola mundo")),
        "dotenv_marker": os.environ.get("FARO_DOTENV_PROBE"),
        "state": adapter_state(litellm_client),
    }


# --- tls_spy -------------------------------------------------------------------------------


def _memory_network(certs: dict[str, Any], contexts: dict[str, ssl.SSLContext]) -> Any:
    import httpcore
    from httpcore._backends.anyio import AnyIOStream

    from tests.net.tls_helpers import memory_stream_pair

    class Network(httpcore.AsyncNetworkBackend):
        def __init__(self) -> None:
            self.hosts: list[str] = []
            self.tasks: list[asyncio.Task[None]] = []

        async def connect_tcp(
            self,
            host: str,
            port: int,  # noqa: ARG002
            timeout: float | None = None,  # noqa: ARG002, ASYNC109 - firma de httpcore
            local_address: str | None = None,  # noqa: ARG002
            socket_options: Iterable[Any] | None = None,  # noqa: ARG002
        ) -> Any:
            self.hosts.append(host)
            client_side, server_side = memory_stream_pair()
            provider = next(p for p, h in HOSTS.items() if h == host)
            task = asyncio.get_running_loop().create_task(self.serve(server_side, provider))
            self.tasks.append(task)
            stream: Any = client_side
            return AnyIOStream(stream)

        async def serve(self, stream: Any, provider: str) -> None:
            from anyio.streams.tls import TLSStream

            with contextlib.suppress(Exception):
                tls = await TLSStream.wrap(
                    stream,
                    server_side=True,
                    ssl_context=contexts[provider],
                    standard_compatible=False,
                )
                data = b""
                while b"\r\n\r\n" not in data:
                    data += await tls.receive()
                head, _, rest = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                while len(rest) < length:
                    rest += await tls.receive()
                body = json.dumps(ok_body(provider)).encode()
                await tls.send(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await tls.aclose()

    del certs
    return Network()


@contextlib.contextmanager
def spy(records: list[dict[str, Any]]) -> Iterator[None]:
    import truststore

    loop_thread = threading.get_ident()
    originals: list[tuple[type, str, Any]] = []

    def wrap(cls: type, name: str) -> None:
        original = cls.__dict__[name]

        def spied(self: Any, *args: Any, **kwargs: Any) -> Any:
            server = bool(kwargs.get("server_side") or (len(args) > 2 and args[2] is True))
            if name == "do_handshake":
                server = bool(getattr(self, "server_side", False))
                context = getattr(self, "context", None)
            else:
                context = self
            if not server:
                records.append(
                    {
                        "call": name,
                        "context_id": id(context),
                        "context_type": type(context).__name__,
                        "loop_thread": threading.get_ident() == loop_thread,
                    }
                )
            return original(self, *args, **kwargs)

        originals.append((cls, name, original))
        setattr(cls, name, spied)

    from truststore._api import _original_SSLContext  # type: ignore[attr-defined]

    # La clase inyectada (`ssl.SSLContext` ya es la subclase con cerrojo), la de `truststore`
    # y la original de la biblioteca estándar (contextos internos o propios de LiteLLM).
    for cls in {ssl.SSLContext, truststore.SSLContext, _original_SSLContext}:
        wrap(cls, "wrap_bio")
        wrap(cls, "wrap_socket")
    wrap(ssl.SSLObject, "do_handshake")
    try:
        yield
    finally:
        for cls, name, original in reversed(originals):
            setattr(cls, name, original)


def scenario_tls_spy() -> dict[str, Any]:
    import trustme

    from tests.net.tls_helpers import issue, server_context

    ca = trustme.CA()
    # Servidores antes de importar el adaptador: tras la inyección, `ssl.SSLContext` es la
    # subclase de cliente de `truststore` y no sirve como servidor.
    contexts = {p: server_context(issue(ca, host)) for p, host in HOSTS.items()}

    from faro_engine.llm import litellm_client
    from faro_engine.net import tls

    engine_context, _store = tls._build_tls_context()
    ca.configure_trust(engine_context)  # solo en esta prueba; nunca en producción
    real_context = tls.tls_context()
    network = _memory_network({}, contexts)
    original_transport = litellm_client._transport

    def transport() -> Any:
        built = original_transport()
        built._pool._network_backend = network
        return built

    module: Any = litellm_client
    module.tls_context = lambda: engine_context
    module._transport = transport
    records: list[dict[str, Any]] = []
    client = litellm_client.LiteLlmClient()
    after: list[list[Any]] = []

    async def run() -> dict[str, Any]:
        results: dict[str, Any] = {}
        for provider in KEYS:
            results[provider] = await attempt(client, provider)
            after.append(
                [
                    engine_context.verify_mode == ssl.CERT_REQUIRED,
                    engine_context.check_hostname,
                    real_context.verify_mode == ssl.CERT_REQUIRED,
                    real_context.check_hostname,
                ]
            )
        await asyncio.gather(*network.tasks, return_exceptions=True)
        return results

    with spy(records):
        results = asyncio.run(run())
    from litellm.llms.custom_httpx import http_handler

    locked = tls._locked_class()
    cache_ids = {id(ctx) for ctx in http_handler._ssl_context_cache.values()}
    inner_id = id(engine_context._ctx)  # type: ignore[attr-defined]
    wrap_contexts = [
        r for r in records if r["call"] != "do_handshake" and r["context_id"] != inner_id
    ]
    inner_calls = sum(
        1 for r in records if r["call"] != "do_handshake" and r["context_id"] == inner_id
    )
    return {
        "results": results,
        "hosts": network.hosts,
        "wrap_calls": len(wrap_contexts),
        "inner_wrap_calls": inner_calls,
        "handshakes": sum(1 for r in records if r["call"] == "do_handshake"),
        "all_engine_or_locked": all(
            r["context_id"] == id(engine_context) or r["context_type"] == locked.__name__
            for r in wrap_contexts
        ),
        "plain_truststore": [r for r in wrap_contexts if r["context_type"] == "SSLContext"],
        "engine_context_used": any(r["context_id"] == id(engine_context) for r in wrap_contexts),
        "litellm_cache_used": any(r["context_id"] in cache_ids for r in wrap_contexts),
        "wrap_socket_in_loop": any(
            r["call"] == "wrap_socket" and r["loop_thread"] for r in records
        ),
        "after_each_call": after,
    }


SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "matrix": scenario_matrix,
    "trap": scenario_trap,
    "tls_spy": scenario_tls_spy,
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    from faro_engine.core.logging import configure_logging

    configure_logging()  # como el motor: logs JSON a stderr, stdout solo para el protocolo
    block_network()
    report = SCENARIOS[args[0]]()
    report["attempts"] = sorted(set(ATTEMPTS))
    sys.stdout.write("FARO_PROBE " + json.dumps(report, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return 0
