"""Sonda que corre en un proceso aparte con la red bloqueada salvo loopback (spec F1b T2).

`python -m tests.deps.offline_probe <escenario> [proveedor]` escribe una línea JSON en stdout
con lo que observó; las pruebas de `tests/deps` la lanzan con el entorno que quieren probar.
Va en un proceso aparte porque importar LiteLLM cambia el estado global (variables de
entorno, loggers, cachés) y porque el bloqueo de red debe estar puesto **antes** de importar.

Las claves son falsas y solo viajan por loopback a un servidor falso local.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypedDict

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
ATTEMPTS: list[str] = []

FAKE_KEYS = {
    "openai": "sk-faro-probe-openai-" + "0" * 20,
    "anthropic": "sk-ant-faro-probe-" + "0" * 20,
    "gemini": "AIzaFaroProbeGemini" + "0" * 20,
}
MODELS = {
    "openai": ("openai/gpt-probe", "/v1"),
    "anthropic": ("anthropic/claude-probe", ""),
    "gemini": ("gemini/gemini-probe", ""),
}


def block_network() -> None:
    """Toda conexión que no sea a loopback falla y queda anotada en `ATTEMPTS`."""
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    def connect(sock: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in LOOPBACK:
            original_connect(sock, address)
            return
        ATTEMPTS.append(host)
        raise OSError("offline_probe: red bloqueada")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is None or host in LOOPBACK:
            return original_getaddrinfo(host, *args, **kwargs)
        ATTEMPTS.append(str(host))
        raise OSError("offline_probe: red bloqueada")

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo


class _FakeProvider(BaseHTTPRequestHandler):
    """Formatos mínimos de OpenAI, Anthropic y Gemini; anota dónde llegó la clave."""

    seen: list[tuple[str, dict[str, str]]] = []  # noqa: RUF012 - estado de la sonda

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002 - heredada
        return

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        usage_openai = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
        body: dict[str, Any]
        if self.path.endswith("/chat/completions"):
            body = {
                "id": "probe",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-probe",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hola"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage_openai,
            }
        elif self.path.endswith("/messages"):
            body = {
                "id": "probe",
                "type": "message",
                "role": "assistant",
                "model": "claude-probe",
                "content": [{"type": "text", "text": "hola"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        else:
            body = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "hola"}]},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 4,
                },
            }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextlib.contextmanager
def fake_provider() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


# --- Escenarios -----------------------------------------------------------------------


def scenario_import_litellm() -> dict[str, Any]:
    """Como el motor: `truststore` inyectado **antes** de importar LiteLLM."""
    import truststore

    truststore.inject_into_ssl()
    import litellm

    report: dict[str, Any] = {"model_cost_entries": len(litellm.model_cost)}
    report.update(_ssl_config())
    return report


def scenario_import_f1b() -> dict[str, Any]:
    import apscheduler.schedulers.asyncio
    import langgraph.checkpoint.base
    import langgraph.graph
    import langgraph.types
    import sqlite_vec
    import truststore

    return {
        "modules": [
            m.__name__
            for m in (
                apscheduler.schedulers.asyncio,
                langgraph.checkpoint.base,
                langgraph.graph,
                langgraph.types,
                sqlite_vec,
                truststore,
            )
        ]
    }


class _State(TypedDict):
    n: int
    answer: str


def _run_graph() -> dict[str, Any]:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    def step(state: _State) -> dict[str, Any]:
        return {"n": state["n"] + 1}

    def ask(state: _State) -> dict[str, Any]:  # LangGraph exige el nombre `state`
        return {"answer": str(interrupt({"pregunta": "¿sí?"}))}

    builder = StateGraph(_State)
    builder.add_node("step", step)
    builder.add_node("ask", ask)
    builder.add_edge(START, "step")
    builder.add_edge("step", "ask")
    builder.add_edge("ask", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config: Any = {"configurable": {"thread_id": "probe"}}

    async def run() -> tuple[Any, Any]:
        first = await graph.ainvoke({"n": 1, "answer": ""}, config)
        second = await graph.ainvoke(Command(resume="sí"), config)
        return first, second

    first, second = asyncio.run(run())
    # LangSmith envía las trazas desde un hilo; se le da tiempo a intentarlo.
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and "tracing" in thread.name:
            thread.join(timeout=5)
    return {"interrupted": "__interrupt__" in first, "resumed": second.get("answer") == "sí"}


def scenario_graph_tracing_off() -> dict[str, Any]:
    import langsmith

    langsmith.configure(enabled=False)
    return _run_graph()


def scenario_graph_default() -> dict[str, Any]:
    return _run_graph()


def _stub_encoding() -> None:
    """Sustituye el vocabulario `cl100k_base` por uno de bytes, sin archivos ni red.

    LiteLLM 1.104 carga `cl100k_base` en las llamadas de Anthropic y Gemini y no lo trae
    incluido. Solo en esta sonda; el motor lo resolverá en T6 (ver informe de T2).
    """
    import tiktoken

    stub = tiktoken.Encoding(
        name="faro_probe_bytes",
        pat_str=r"\S+|\s+",
        mergeable_ranks={bytes([i]): i for i in range(256)},
        special_tokens={},
    )
    main = sys.modules["litellm.main"]
    main.__dict__["encoding"] = stub
    main.__dict__["_encoding_cache"] = stub


def _holds(obj: Any, needle: str, depth: int = 0, seen: set[int] | None = None) -> bool:
    seen = set() if seen is None else seen
    if id(obj) in seen or depth > 6:
        return False
    seen.add(id(obj))
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(
            _holds(k, needle, depth + 1, seen) or _holds(v, needle, depth + 1, seen)
            for k, v in obj.items()
        )
    if isinstance(obj, list | tuple | set):
        return any(_holds(v, needle, depth + 1, seen) for v in obj)
    attributes = getattr(obj, "__dict__", None)
    return attributes is not None and _holds(attributes, needle, depth + 1, seen)


def _heap_holders(needle: str) -> int:
    """Diccionarios vivos con la clave (otros hilos pueden cambiarlos mientras se leen)."""
    count = 0
    for obj in gc.get_objects():
        if obj is FAKE_KEYS or not isinstance(obj, dict):
            continue
        for _ in range(3):
            try:
                values = list(obj.values())
            except RuntimeError:  # cambió de tamaño mientras se copiaba
                continue
            if any(isinstance(v, str) and needle in v for v in values):
                count += 1
            break
    return count


def scenario_litellm_call(provider: str, stub_encoding: bool) -> dict[str, Any]:
    import logging

    # Como el motor (`configure_logging`): el logger raíz en INFO, con salida a stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    import litellm
    from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    litellm.cache = None
    litellm.turn_off_message_logging = True
    litellm.suppress_debug_info = True
    if stub_encoding:
        _stub_encoding()
    model, path = MODELS[provider]
    key = "".join(FAKE_KEYS[provider])  # copia, como la `str` que exige LiteLLM
    cache = litellm.in_memory_llm_clients_cache
    result: dict[str, Any] = {}

    async def run(port: int, key: str) -> None:
        response = await litellm.acompletion(
            model=model,
            api_base=f"http://127.0.0.1:{port}{path}",
            api_key=key,
            messages=[{"role": "user", "content": "hola"}],
            max_tokens=5,
            num_retries=0,
            timeout=10,
        )
        result["text"] = response.choices[0].message.content
        result["tokens"] = [response.usage.prompt_tokens, response.usage.completion_tokens]
        sent_path, headers = _FakeProvider.seen[-1]
        result["key_sent_in"] = sorted(h for h, v in headers.items() if key in v)
        result["key_in_url"] = key in sent_path
        result["key_in_cache_keys"] = any(key in str(k) for k in cache.cache_dict)
        result["key_in_cached_clients"] = sorted(
            {type(v).__name__ for v in cache.cache_dict.values() if _holds(v, key)}
        )
        await asyncio.sleep(1.0)  # tareas de registro de LiteLLM en segundo plano
        await close_litellm_async_clients()  # type: ignore[no-untyped-call]
        cache.flush_cache()  # type: ignore[no-untyped-call]

    with fake_provider() as port:
        try:
            asyncio.run(run(port, key))
        except Exception as exc:  # noqa: BLE001 - la prueba decide con el informe
            result["error"] = type(exc).__name__
    del key
    gc.collect()
    result["cache_entries_after_flush"] = len(cache.cache_dict)
    result["heap_dicts_with_key_after_flush"] = _heap_holders(FAKE_KEYS[provider])
    return result


def _ssl_config() -> dict[str, Any]:
    from litellm.llms.custom_httpx.http_handler import get_ssl_configuration

    config = get_ssl_configuration()
    return {"type": f"{type(config).__module__}.{type(config).__name__}", "value": repr(config)}


def scenario_ssl_late_injection() -> dict[str, Any]:
    """`truststore` inyectado **después** de importar LiteLLM (orden equivocado)."""
    import litellm  # noqa: F401 - al importarse crea y guarda un contexto TLS con certifi
    import truststore

    truststore.inject_into_ssl()
    return _ssl_config()


SCENARIOS: dict[str, Callable[..., dict[str, Any]]] = {
    "import_litellm": scenario_import_litellm,
    "import_f1b": scenario_import_f1b,
    "graph_tracing_off": scenario_graph_tracing_off,
    "graph_default": scenario_graph_default,
    "ssl_late_injection": scenario_ssl_late_injection,
}


def main(argv: list[str]) -> int:
    block_network()
    name = argv[0]
    if name == "litellm_call":
        report = scenario_litellm_call(argv[1], stub_encoding="--stub-encoding" in argv)
    else:
        report = SCENARIOS[name]()
    report["attempts"] = sorted(set(ATTEMPTS))
    sys.stdout.write("FARO_PROBE " + json.dumps(report, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
