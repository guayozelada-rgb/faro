"""Prueba de humo del motor empaquetado (spec F1b §4.7, T2 y T11; ADR 0015 §6 criterios 5 y 6).

Es el punto de entrada que `bundle_smoke_build.py` empaqueta con PyInstaller `--onedir`, con
las mismas opciones que el motor real (`engine_pyinstaller_args`). También corre sin
empaquetar (`uv run python scripts/bundle_smoke.py`). Comprueba, sin red salvo loopback:

- `engine`: la app FastAPI del motor se construye; los scripts `manual_*` y el
  `sitecustomize` del build sin red no están en el paquete.
- `db`: crea una base cifrada de perfil, aplica las migraciones y la vuelve a abrir.
- `vec`: `sqlite-vec` carga en una conexión `sqlcipher3` (si el paquete está incluido).
- `agents` (T8 y T9): el registro de agentes carga con sus prompts `.md` y el catálogo
  `models.json`; `agents/framework/graph.py`, que el trabajador importa con `importlib`,
  está en el paquete y apaga LangSmith aunque el entorno lo active; un grafo con
  `FaroCheckpointSaver` sobre la base cifrada se interrumpe y se reanuda, sin intentos de
  red ni contextos TLS creados por `httpx2`/`httpcore2` (la sonda de T8 en el ejecutable).
- `adapter` (T6-C5): el adaptador real (`faro_engine.llm.litellm_client`) se carga con
  `importlib`, como el motor, aplica su endurecimiento y comprueba el SHA-256 de
  `cl100k_base`; tiktoken lo carga del paquete sin red. Con `respx` en el paquete, llama a
  los seis modelos del catálogo y a los errores de cada proveedor contra sus hosts
  oficiales simulados: falla si a LiteLLM le falta algún módulo perezoso (condición 12 del
  informe de T2: el hook usa una lista permitida).
- `tls`: el contexto único del motor (`net/tls.py::tls_context()`) usa `truststore` con el
  módulo de su plataforma, con el cerrojo por contexto, `CERT_REQUIRED`, `check_hostname`,
  mínimo TLS 1.2, sin renegociación y sin raíces añadidas, y `default_transport()` lo usa
  (ADR 0012, actualización 2026-10-06, prueba 6). Con LiteLLM, además, su contexto TLS usa
  el almacén del sistema con el cerrojo.
- `graph`: un grafo LangGraph con un LLM falso se interrumpe y se reanuda.
- `scheduler`: APScheduler 3 (`AsyncIOScheduler` + `MemoryJobStore`) dispara un trabajo.
- `litellm`: `acompletion` contra un servidor falso local (formatos OpenAI y Gemini) y
  vaciado de la caché de clientes, que no debe conservar la clave.
- `keylog` (condición 17 del informe de T2): con `SSLKEYLOGFILE` definida al lanzar el
  ejecutable, el prólogo de `faro_engine/__main__.py` (lo primero que se importa aquí) la
  quita y una conexión TLS real en loopback no crea el archivo; tampoco existía al entrar
  (ningún hook de ejecución de PyInstaller creó antes un contexto TLS). Con
  `--keylog-control` (sin el prólogo) el mismo ejecutable sí lo crea: la comprobación no es
  vacía.

Sin `--require-f1b`, si las dependencias de F1b no están en el paquete (variante `base`),
sus comprobaciones quedan como `skipped`. Escribe una sola línea JSON en stdout y termina con
código 0 si todo lo exigido pasó. Las claves que usa son falsas y solo viajan por loopback o
a `respx`.
"""

from __future__ import annotations

import os
import sys

# Antes de cualquier otra importación (condición 17 del informe de T2). La ruta se guarda
# para comprobar después que nadie creó el archivo.
KEYLOG_PATH = os.environ.get("SSLKEYLOGFILE")
KEYLOG_EXISTED_AT_ENTRY = KEYLOG_PATH is not None and os.path.exists(KEYLOG_PATH)  # noqa: PTH110
KEYLOG_CONTROL = "--keylog-control" in sys.argv[1:]
if not KEYLOG_CONTROL:
    # El prólogo del motor: quita `SSLKEYLOGFILE`, `SSL_CERT_FILE` y `SSL_CERT_DIR` antes
    # de importar nada más (igual que el ejecutable `faro-engine`, cuya entrada es este
    # mismo archivo `__main__.py`).
    import faro_engine.__main__  # noqa: F401

# Mapa de precios local: sin él, `import litellm` lo descarga de GitHub (comprobación
# `litellm`; el adaptador real la fija por su cuenta).
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ.pop("SSL_VERIFY", None)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, TypedDict  # noqa: E402

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
FAKE_KEYS = {
    "openai": "sk-faro-smoke-" + "0" * 24,
    "anthropic": "sk-ant-faro-smoke-" + "0" * 20,
    "gemini": "AIzaFaroSmoke" + "0" * 26,
}
F1B_MODULES = ("litellm", "langgraph", "apscheduler", "respx", "trustme")
MANUAL_SCRIPTS = ("manual_llm_check", "manual_llm_models_check", "manual_tls_check")
# Módulos del proxy de LiteLLM que no deben cargarse nunca en el motor (revisión de
# seguridad de T11, hallazgo 2): `proxy_server` hace `sys.path.insert(0, abspath("../.."))`
# al importarse. Excluirlos del paquete con `--exclude-module` queda para release.
FORBIDDEN_LITELLM_MODULES = ("litellm.proxy.proxy_server", "litellm.proxy.prisma_migration")
OFFICIAL_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/",
}


class _NetworkGuard:
    """Bloquea toda conexión que no sea a loopback y anota los intentos."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._connect = socket.socket.connect
        self._getaddrinfo = socket.getaddrinfo

    def install(self) -> None:
        guard = self

        def connect(sock: socket.socket, address: Any) -> None:
            host = address[0] if isinstance(address, tuple) else str(address)
            if host in LOOPBACK:
                guard._connect(sock, address)
                return
            guard.attempts.append(f"connect:{host}")
            raise OSError("bundle_smoke: red bloqueada")

        def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(host, bytes):  # httpcore (anyio) pasa el host en bytes
                host = host.decode("ascii", "replace")
            if host is None or host in LOOPBACK:
                return guard._getaddrinfo(host, *args, **kwargs)
            guard.attempts.append(f"getaddrinfo:{host}")
            raise OSError("bundle_smoke: red bloqueada")

        socket.socket.connect = connect  # type: ignore[method-assign,assignment]
        socket.getaddrinfo = getaddrinfo


class _FakeProvider(BaseHTTPRequestHandler):
    """Responde como OpenAI (`/chat/completions`) o Gemini (`:generateContent`)."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002 - heredada
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        body = _ok_body("openai" if self.path.endswith("/chat/completions") else "gemini")
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _ok_body(provider: str) -> dict[str, Any]:
    """Respuesta correcta mínima de cada proveedor (11 tokens de entrada y 2 de salida)."""
    if provider == "openai":
        return {
            "id": "smoke",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-smoke",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "listo"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13},
        }
    if provider == "anthropic":
        return {
            "id": "smoke",
            "type": "message",
            "role": "assistant",
            "model": "claude-smoke",
            "content": [{"type": "text", "text": "listo"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 2},
        }
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "listo"}]},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 2, "totalTokenCount": 13},
    }


def _error_body(provider: str, status: int, code: str) -> dict[str, Any]:
    if provider == "anthropic":
        return {"type": "error", "error": {"type": code, "message": "smoke"}}
    if provider == "gemini":
        return {"error": {"code": status, "message": "smoke", "status": code}}
    return {"error": {"message": "smoke", "type": code, "param": None, "code": code}}


@contextlib.contextmanager
def _fake_provider() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _available(module: str) -> bool:
    """¿Está en el paquete? Sin importarlo: el adaptador debe ser el primero en importar
    LiteLLM (su endurecimiento va antes del import)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _open_database(tmp: Path) -> Any:
    from faro_engine.core.db.database import open_profile_database

    db = open_profile_database(tmp, str(uuid.uuid4()), bytearray(secrets.token_hex(32), "ascii"))
    if not db.is_ready:
        raise RuntimeError(f"base no disponible: {db.status.error_code}")
    return db


# --- Comprobaciones -------------------------------------------------------------------


def check_engine(_tmp: Path) -> dict[str, Any]:
    from faro_engine import __version__
    from faro_engine.core.app import create_app
    from faro_engine.core.config import Settings

    token = secrets.token_urlsafe(32).encode()
    app = create_app(Settings(token=token, port=1, version=__version__))
    # Sin empaquetar, `scripts/` es `sys.path[0]` y los scripts manuales sí se encuentran.
    frozen = bool(getattr(sys, "frozen", False))
    present = [m for m in (*MANUAL_SCRIPTS, "sitecustomize") if frozen and _available(m)]
    if present:
        raise RuntimeError(f"módulos que no deben ir en el paquete: {present}")
    return {"routes": len(app.routes), "version": __version__}


def check_db(tmp: Path) -> dict[str, Any]:
    from faro_engine.core.db.database import open_profile_database

    profile = str(uuid.uuid4())
    key_hex = secrets.token_hex(32)
    versions: list[int] = []
    for _ in range(2):  # crear y volver a abrir
        db = open_profile_database(tmp, profile, bytearray(key_hex, "ascii"))
        if not db.is_ready:
            raise RuntimeError(f"base no disponible: {db.status.error_code}")
        rows = db.run_sync(
            lambda conn: conn.execute("SELECT version FROM schema_migrations").fetchall()
        )
        versions = [int(row[0]) for row in rows]
        db.close()
    return {"migrations": versions}


def check_vec(tmp: Path) -> dict[str, Any]:
    import sqlite_vec

    from faro_engine.core.db.connection import open_encrypted

    conn = open_encrypted(tmp / "vec.db", bytearray(secrets.token_hex(32), "ascii"))
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        version = conn.execute("SELECT vec_version()").fetchone()[0]
        conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[2])")
        conn.execute(
            "INSERT INTO v(rowid, embedding) VALUES (1, ?)", (sqlite_vec.serialize_float32([1, 0]),)
        )
        hits = conn.execute(
            "SELECT rowid FROM v WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
            (sqlite_vec.serialize_float32([1, 0]),),
        ).fetchall()
    finally:
        conn.close()
    return {"vec_version": str(version), "hits": len(hits)}


class _CheckpointState(TypedDict):
    n: int
    answer: str


def _run_checkpointed_graph(tmp: Path) -> dict[str, Any]:
    """Un grafo con `interrupt` sobre `FaroCheckpointSaver` y la base cifrada."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    from faro_engine.agents.framework.checkpoint import FaroCheckpointSaver
    from faro_engine.core.ids import new_id
    from faro_engine.core.store.runs import NewRun, insert_run

    db = _open_database(tmp)
    run_id = new_id()
    try:
        db.run_sync(
            lambda conn: insert_run(
                conn,
                NewRun(
                    id=run_id,
                    agent_kind="bundle_smoke",
                    agent_version=1,
                    objective="bundle_smoke.run",
                    trigger="user",
                    token_budget=1_000,
                    max_cost_micros=1_000,
                    created_at="2026-10-13T00:00:00Z",
                ),
                deduplicate=False,
            )
        )

        def step(state: _CheckpointState) -> dict[str, Any]:
            return {"n": state["n"] + 1}

        def ask(state: _CheckpointState) -> dict[str, Any]:  # LangGraph exige `state`
            return {"answer": str(interrupt({"pregunta": f"¿sí? ({state['n']})"}))}

        builder = StateGraph(_CheckpointState)
        builder.add_node("step", step)
        builder.add_node("ask", ask)
        builder.add_edge(START, "step")
        builder.add_edge("step", "ask")
        builder.add_edge("ask", END)
        graph = builder.compile(checkpointer=FaroCheckpointSaver(db))
        config: Any = {"configurable": {"thread_id": run_id}}

        async def run() -> tuple[Any, Any]:
            first = await graph.ainvoke({"n": 1, "answer": ""}, config, durability="sync")
            second = await graph.ainvoke(Command(resume="sí"), config, durability="sync")
            return first, second

        first, second = asyncio.run(run())
        stored = db.run_sync(
            lambda conn: conn.execute(
                "SELECT count(*) FROM agent_checkpoints WHERE thread_id = ?", (run_id,)
            ).fetchone()[0]
        )
    finally:
        db.close()
    # LangSmith enviaría las trazas desde un hilo: se le da tiempo a intentarlo.
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and "tracing" in thread.name:
            thread.join(timeout=5)
    if "__interrupt__" not in first or second.get("answer") != "sí" or not stored:
        raise RuntimeError("el grafo con FaroCheckpointSaver no se interrumpió o no se reanudó")
    return {"interrupted": True, "resumed": True, "checkpoints": int(stored)}


def check_agents(tmp: Path) -> dict[str, Any]:
    import truststore

    from faro_engine.agents.framework.tracing import tracing_variables

    created: list[str] = []
    original = truststore.SSLContext.__init__

    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        files = " ".join(frame.filename for frame in traceback.extract_stack())
        created.append("httpx2" if "httpx2" in files or "httpcore2" in files else "other")
        original(self, *args, **kwargs)

    truststore.SSLContext.__init__ = spy  # type: ignore[method-assign]
    try:
        env_before = tracing_variables()
        from faro_engine.agents.registry import (
            AGENT_SPECS,
            GRAPH_MODULE,
            dev_fake_replies,
            load_graph_module,
            validate_registry,
        )
        from faro_engine.llm.catalog import default_catalog

        specs = validate_registry(AGENT_SPECS)
        # Como el trabajador: `graph.py` con `importlib` (PyInstaller no lo ve solo).
        graph_module = asyncio.run(load_graph_module())
        env_after = tracing_variables()
        from langsmith import utils as langsmith_utils

        tracing_enabled = bool(langsmith_utils.tracing_is_enabled())
        report = _run_checkpointed_graph(tmp)
    finally:
        truststore.SSLContext.__init__ = original  # type: ignore[method-assign]
    prompts: int = sum(
        (len(list(spec.prompts.templates)) for spec in specs if spec.prompts is not None), 0
    )
    problems = []
    if graph_module.__name__ != GRAPH_MODULE:
        problems.append("graph.py no es el módulo esperado")
    if env_after or tracing_enabled:
        problems.append("LangSmith sigue activado")
    if created.count("httpx2"):
        problems.append("httpx2/httpcore2 crearon contextos TLS")
    if not prompts:
        problems.append("sin prompts")
    if problems:
        raise RuntimeError("; ".join(problems))
    report.update(
        {
            "agents": [spec.kind for spec in specs],
            "prompts": prompts,
            "dev_replies": len(dev_fake_replies()),
            "models": len(default_catalog().models),
            "langsmith_env_before": len(env_before),
            "httpx2_contexts": 0,
        }
    )
    return report


async def _adapter_calls(client: Any) -> dict[str, Any]:
    """Los seis modelos del catálogo y dos errores por proveedor, con `respx`."""
    import httpx
    import respx

    from faro_engine.llm.catalog import default_catalog
    from faro_engine.llm.client import ResolvedCall
    from faro_engine.llm.errors import LlmCallError

    current: dict[str, tuple[int, str]] = {}
    other_hosts: list[str] = []
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    response_format = {"type": "json_schema", "json_schema": {"name": "S", "schema": schema}}

    def responder(provider: str) -> Callable[[httpx.Request], httpx.Response]:
        def handle(_request: httpx.Request) -> httpx.Response:
            status, code = current["case"]
            if status == 200:
                return httpx.Response(200, json=_ok_body(provider))
            headers = {"retry-after": "3"} if status == 429 else {}
            return httpx.Response(status, headers=headers, json=_error_body(provider, status, code))

        return handle

    def other(request: httpx.Request) -> httpx.Response:
        other_hosts.append(str(request.url.host))
        return httpx.Response(404)

    async def attempt(model: Any) -> dict[str, Any]:
        call = ResolvedCall(
            provider=model.provider,
            tier=model.tier,
            model=model.model,
            litellm_model=model.litellm_model,
            prompt_id="bundle_smoke.call",
            messages=({"role": "user", "content": "hola"},),
            max_output_tokens=16,
            response_format=response_format,
        )
        try:
            raw = await client.complete(call, "".join(FAKE_KEYS[model.provider]))
        except LlmCallError as exc:
            return {"kind": exc.kind}
        return {"tokens": [raw.tokens_in, raw.tokens_out]}

    results: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    catalog = default_catalog()
    with respx.mock(assert_all_called=False) as mock:
        for provider, url in OFFICIAL_URLS.items():
            mock.post(url__startswith=url).mock(side_effect=responder(provider))
        mock.route().mock(side_effect=other)
        current["case"] = (200, "")
        for model in catalog.models:
            results[model.model] = await attempt(model)
        first_by_provider = {m.provider: m for m in reversed(catalog.models)}
        cases = (("401", (401, "authentication_error")), ("429", (429, "rate_limit_error")))
        for name, case in cases:
            current["case"] = case
            for provider, model in sorted(first_by_provider.items()):
                errors[f"{provider}:{name}"] = (await attempt(model)).get("kind")
    return {"models": results, "errors": errors, "other_hosts": other_hosts}


def check_adapter(_tmp: Path) -> dict[str, Any]:
    from faro_engine.llm import hardening
    from faro_engine.llm.service import ADAPTER_MODULE, LazyLiteLlmClient

    lazy = LazyLiteLlmClient()
    asyncio.run(lazy.prepare())  # `importlib`, en un hilo, como el motor
    module = sys.modules[ADAPTER_MODULE]
    path = hardening.verify_tiktoken_file()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    from litellm.litellm_core_utils.default_encoding import encoding

    tokens = len(encoding.encode("Hola, Faro"))
    report: dict[str, Any] = {
        "module": module.__name__,
        "cl100k_sha256": digest,
        "cl100k_in_bundle": bool(getattr(sys, "frozen", False))
        and Path(getattr(sys, "_MEIPASS", "")) in path.parents,
        "encoding_tokens": tokens,
        "unsafe_env": hardening.unsafe_variables(),
    }
    problems = []
    if digest != hardening.CL100K_SHA256:
        problems.append("SHA-256 de cl100k_base distinto")
    if report["unsafe_env"]:
        problems.append("entorno sin limpiar")
    if _available("respx"):
        calls = asyncio.run(_adapter_calls(module.LiteLlmClient()))
        report["calls"] = calls
        bad = {m: r for m, r in calls["models"].items() if r != {"tokens": [11, 2]}}
        expected = {"401": "invalid_key", "429": "rate_limited"}
        wrong = {k: v for k, v in calls["errors"].items() if v != expected[k.split(":")[1]]}
        if bad or wrong or calls["other_hosts"]:
            problems.append(f"llamadas: {bad or ''} {wrong or ''} {calls['other_hosts'] or ''}")
    elif getattr(sys, "frozen", False):
        report["calls"] = "skipped"
    # Después de las llamadas: nada del servidor proxy de LiteLLM se cargó (hallazgo 2).
    loaded = [name for name in FORBIDDEN_LITELLM_MODULES if name in sys.modules]
    report["forbidden_modules_loaded"] = loaded
    if loaded:
        problems.append(f"módulos del proxy de LiteLLM cargados: {loaded}")
    if problems:
        raise RuntimeError("; ".join(problems))
    return report


_TRUSTSTORE_PLATFORM = {"win32": "truststore._windows", "darwin": "truststore._macos"}


def _check_engine_tls() -> dict[str, Any]:
    """Prueba 1 de ADR 0012 (actualización 2026-10-06) dentro del ejecutable."""
    import ssl

    import truststore

    from faro_engine.net.client import default_transport
    from faro_engine.net.tls import tls_context, tls_store

    context = tls_context()
    problems = []
    if tls_store() != "system" or not isinstance(context, truststore.SSLContext):
        problems.append(f"almacén {tls_store()}")
    if type(context).__name__ != "_LockedContext":
        problems.append("contexto sin cerrojo")
    if context is not tls_context():
        problems.append("contexto no compartido")
    if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
        problems.append("verificación desactivada")
    if context.minimum_version != ssl.TLSVersion.TLSv1_2:
        problems.append(f"mínimo {context.minimum_version!r}")
    if not context.options & ssl.OP_NO_RENEGOTIATION:
        problems.append("renegociación permitida")
    if context._ctx.get_ca_certs():  # type: ignore[attr-defined]
        problems.append("raíces añadidas")
    pool = default_transport()._pool  # type: ignore[attr-defined]
    if pool._ssl_context is not context or pool._http2:
        problems.append("default_transport no usa el contexto compartido")
    # El módulo de plataforma (CryptoAPI o Security.framework) debe estar en el paquete.
    platform_module = _TRUSTSTORE_PLATFORM.get(sys.platform, "truststore._openssl")
    if platform_module not in sys.modules:
        problems.append(f"falta {platform_module}")
    if problems:
        raise RuntimeError("; ".join(problems))
    return {
        "engine_store": tls_store(),
        "engine_platform_module": platform_module,
        "truststore": truststore.__version__,
    }


def check_tls(_tmp: Path) -> dict[str, Any]:
    result = _check_engine_tls()
    if not _available("litellm"):
        result["litellm_ssl_context"] = "skipped"
        return result
    import truststore
    from litellm.llms.custom_httpx.http_handler import get_ssl_configuration

    context = get_ssl_configuration()
    kind = f"{type(context).__module__}.{type(context).__name__}"
    # La inyección pone la subclase con cerrojo (ADR 0012, condiciones 12 y 13).
    locked = isinstance(context, truststore.SSLContext) and (
        type(context).__name__ == "_LockedContext"
    )
    if not locked:
        raise RuntimeError(f"LiteLLM no usa truststore con cerrojo: {kind}")
    result["litellm_ssl_context"] = kind
    return result


class _GraphState(TypedDict):
    question: str
    draft: str
    approved: str


class _FakeLLM:
    """Respuesta fija: el grafo no habla con ningún proveedor."""

    def complete(self, prompt: str) -> str:
        return f"resumen de prueba ({len(prompt)} caracteres)"


def check_graph(_tmp: Path) -> dict[str, Any]:
    import langsmith
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    langsmith.configure(enabled=False)
    llm = _FakeLLM()

    def write(state: _GraphState) -> dict[str, str]:
        return {"draft": llm.complete(state["question"])}

    def approve(state: _GraphState) -> dict[str, str]:
        decision = interrupt({"draft": state["draft"]})
        return {"approved": str(decision)}

    builder = StateGraph(_GraphState)
    builder.add_node("write", write)
    builder.add_node("approve", approve)
    builder.add_edge(START, "write")
    builder.add_edge("write", "approve")
    builder.add_edge("approve", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config: Any = {"configurable": {"thread_id": "smoke"}}

    async def run() -> tuple[Any, Any]:
        first = await graph.ainvoke({"question": "hola", "draft": "", "approved": ""}, config)
        second = await graph.ainvoke(Command(resume="approved"), config)
        return first, second

    first, second = asyncio.run(run())
    if "__interrupt__" not in first or second.get("approved") != "approved":
        raise RuntimeError("el grafo no se interrumpió o no se reanudó")
    return {"interrupted": True, "resumed": True}


def check_scheduler(_tmp: Path) -> dict[str, Any]:
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    async def run() -> float:
        fired = asyncio.Event()
        scheduler = AsyncIOScheduler(jobstores={"default": MemoryJobStore()}, timezone=UTC)
        start = time.monotonic()
        scheduler.add_job(fired.set, "date", run_date=datetime.now(UTC) + timedelta(seconds=0.2))
        scheduler.start()
        try:
            await asyncio.wait_for(fired.wait(), timeout=10)
        finally:
            scheduler.shutdown(wait=False)
        return time.monotonic() - start

    return {"fired_after_s": round(asyncio.run(run()), 2)}


def _harden_litellm() -> Any:
    import logging

    import litellm

    # LiteLLM añade a sus loggers un handler que escribe en stdout todo lo que está por
    # debajo de WARNING; stdout es el canal del protocolo. Se quitan sus handlers y sus
    # registros pasan por los del motor (JSON a stderr, con redacción, ADR 0013).
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("LiteLLM"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
            logger.setLevel(logging.WARNING)

    litellm.success_callback = []
    litellm.failure_callback = []
    litellm.callbacks = []
    litellm.cache = None
    litellm.turn_off_message_logging = True
    litellm.suppress_debug_info = True
    litellm.disable_hf_tokenizer_download = True
    return litellm


def check_litellm(_tmp: Path) -> dict[str, Any]:
    litellm = _harden_litellm()
    from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

    models = {"openai": ("openai/gpt-smoke", "/v1"), "gemini": ("gemini/gemini-smoke", "")}

    async def run(port: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for provider, (model, path) in models.items():
            response = await litellm.acompletion(
                model=model,
                api_base=f"http://127.0.0.1:{port}{path}",  # solo en esta prueba de humo
                api_key=FAKE_KEYS[provider],
                messages=[{"role": "user", "content": "hola"}],
                max_tokens=5,
                num_retries=0,
                timeout=10,
            )
            result[provider] = {
                "text": response.choices[0].message.content,
                "tokens": [response.usage.prompt_tokens, response.usage.completion_tokens],
            }
        await asyncio.sleep(0.5)  # tareas de registro de LiteLLM en segundo plano
        await close_litellm_async_clients()  # type: ignore[no-untyped-call]
        litellm.in_memory_llm_clients_cache.flush_cache()
        return result

    with _fake_provider() as port:
        result = asyncio.run(run(port))
    cache = litellm.in_memory_llm_clients_cache.cache_dict
    leaked = [p for p, key in FAKE_KEYS.items() if any(key in str(k) for k in cache)]
    if cache or leaked:
        raise RuntimeError(f"caché de clientes sin vaciar ({len(cache)} entradas)")
    from litellm.rust_bridge import native_bridge_available

    result["native_bridge"] = native_bridge_available()
    return result


def tls_handshake_in_loopback(keylog: str | None) -> str:
    """Conexión TLS real en 127.0.0.1 con un certificado de `trustme`.

    El contexto del cliente es el de la biblioteca estándar (`create_default_context`, que
    aplica `SSLKEYLOGFILE` si el entorno la tiene) y, además, `keylog_filename` si la
    variable sigue en el entorno, como hacen httpx, urllib3 y aiohttp. Devuelve la versión
    de TLS negociada.
    """
    import ssl

    import trustme

    ca = trustme.CA()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("localhost").configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    if keylog:
        client_context.keylog_filename = keylog
    listener = socket.create_server(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])

    def serve() -> None:
        with contextlib.suppress(OSError):
            raw, _ = listener.accept()
            with server_context.wrap_socket(raw, server_side=True) as tls:
                tls.sendall(tls.recv(16))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=10) as raw,
            client_context.wrap_socket(raw, server_hostname="localhost") as tls,
        ):
            tls.sendall(b"faro")
            if tls.recv(16) != b"faro":
                raise RuntimeError("eco TLS incorrecto")
            version = str(tls.version())
    finally:
        listener.close()
        thread.join(timeout=10)
    return version


def check_keylog(_tmp: Path) -> dict[str, Any]:
    if not KEYLOG_PATH:
        raise RuntimeError("lanza la prueba con SSLKEYLOGFILE definida")
    version = tls_handshake_in_loopback(os.environ.get("SSLKEYLOGFILE"))
    report = {
        "tls_version": version,
        "existed_at_entry": KEYLOG_EXISTED_AT_ENTRY,
        "env_removed": "SSLKEYLOGFILE" not in os.environ,
        "created": Path(KEYLOG_PATH).exists(),
        "ignore_environment": bool(sys.flags.ignore_environment),
    }
    if report["existed_at_entry"] or report["created"] or not report["env_removed"]:
        raise RuntimeError(f"claves TLS en archivo: {report}")
    return report


CHECKS: tuple[tuple[str, Callable[[Path], dict[str, Any]], tuple[str, ...]], ...] = (
    ("engine", check_engine, ()),
    ("db", check_db, ()),
    ("vec", check_vec, ("sqlite_vec",)),
    # Antes que `graph`: importar el marco apaga LangSmith para todo el proceso.
    ("agents", check_agents, ("langgraph",)),
    # Antes que `tls` y `litellm`: el adaptador es el primero que importa LiteLLM.
    ("adapter", check_adapter, ("litellm",)),
    ("tls", check_tls, ()),  # truststore es del núcleo de red (T2b): siempre exigida
    ("graph", check_graph, ("langgraph",)),
    ("scheduler", check_scheduler, ("apscheduler",)),
    ("litellm", check_litellm, ("litellm",)),
    ("keylog", check_keylog, ("trustme",)),
)


def run_checks(require_f1b: bool, require_vec: bool) -> dict[str, Any]:
    from faro_engine.core.logging import configure_logging

    configure_logging()  # logs JSON a stderr; stdout queda para el informe
    guard = _NetworkGuard()
    guard.install()
    # Mismo orden que el motor: contexto del motor y, después, la inyección para LiteLLM.
    from faro_engine.net.tls import install_system_trust_for_libraries

    install_system_trust_for_libraries()
    report: dict[str, Any] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "checks": {},
    }
    ok = True
    with tempfile.TemporaryDirectory(prefix="faro-bundle-smoke-") as tmp_name:
        for name, check, needs in CHECKS:
            required = (
                not needs or (require_f1b and name != "vec") or (name == "vec" and require_vec)
            )
            missing = [m for m in needs if not _available(m)]
            if name == "keylog" and not KEYLOG_PATH:
                missing.append("SSLKEYLOGFILE")
            if missing and not required:
                report["checks"][name] = {"status": "skipped"}
                continue
            started = time.perf_counter()
            try:
                detail = check(Path(tmp_name))
            except Exception as exc:  # noqa: BLE001 - se informa y se sigue con las demás
                ok = False
                detail = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                status = "failed"
            else:
                status = "ok"
            detail["status"] = status
            detail["seconds"] = round(time.perf_counter() - started, 2)
            report["checks"][name] = detail
    report["network_attempts"] = sorted(set(guard.attempts))
    report["f1b_modules"] = {m: _available(m) for m in F1B_MODULES}
    report["ok"] = ok and not guard.attempts
    return report


def run_keylog_control() -> dict[str, Any]:
    """Sin el prólogo del motor: el mismo ejecutable sí escribe las claves (control)."""
    if not KEYLOG_PATH:
        return {"ok": False, "error": "lanza el control con SSLKEYLOGFILE definida"}
    version = tls_handshake_in_loopback(os.environ.get("SSLKEYLOGFILE"))
    created = Path(KEYLOG_PATH).exists()
    return {"ok": created, "control": True, "created": created, "tls_version": version}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--require-f1b", action="store_true", help="falla si faltan deps de F1b")
    parser.add_argument("--require-vec", action="store_true", help="falla si falta sqlite-vec")
    parser.add_argument(
        "--keylog-control",
        action="store_true",
        help="control de la condición 17: sin el prólogo del motor, solo la conexión TLS",
    )
    args = parser.parse_args(argv)
    if args.keylog_control:
        report = run_keylog_control()
    else:
        report = run_checks(args.require_f1b, args.require_vec)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
