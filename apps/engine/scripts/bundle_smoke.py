"""Prueba de humo del motor empaquetado (spec F1b §4.7 y T2, ADR 0015 §6 criterios 5 y 6).

Es el punto de entrada que `bundle_smoke_build.py` empaqueta con PyInstaller `--onedir`.
También corre sin empaquetar (`uv run python scripts/bundle_smoke.py`). Comprueba, sin red
salvo loopback:

- `engine`: la app FastAPI del motor se construye.
- `db`: crea una base cifrada de perfil, aplica las migraciones y la vuelve a abrir.
- `vec`: `sqlite-vec` carga en una conexión `sqlcipher3` (si el paquete está incluido).
- `tls`: el contexto único del motor (`net/tls.py::tls_context()`) usa `truststore` con el
  módulo de su plataforma, con el cerrojo por contexto, `CERT_REQUIRED`, `check_hostname`,
  mínimo TLS 1.2, sin renegociación y sin raíces añadidas, y `default_transport()` lo usa
  (ADR 0012, actualización 2026-10-06, prueba 6). Si LiteLLM está incluido, además, su
  contexto TLS (con la inyección de `install_system_trust_for_libraries()`) usa el almacén
  del sistema con el cerrojo.
- `graph`: un grafo LangGraph con un LLM falso se interrumpe y se reanuda.
- `scheduler`: APScheduler 3 (`AsyncIOScheduler` + `MemoryJobStore`) dispara un trabajo.
- `litellm`: `acompletion` contra un servidor falso local (formatos OpenAI y Gemini) y
  vaciado de la caché de clientes, que no debe conservar la clave.

LiteLLM 1.104 no trae el vocabulario `cl100k_base` de tiktoken y lo descarga en la primera
llamada de Anthropic o Gemini. El paquete lo lleva en `faro_tiktoken/` (lo pone
`bundle_smoke_build.py`, con su SHA-256 comprobado) y aquí se apunta
`CUSTOM_TIKTOKEN_CACHE_DIR` a esa carpeta; sin empaquetar, usa `--tiktoken-dir`.

Sin `--require-f1b`, si las dependencias de F1b no están en el paquete (variante `base`),
sus comprobaciones quedan como `skipped`. Escribe una sola línea JSON en stdout y termina con
código 0 si todo lo exigido pasó. Las claves que usa son falsas y solo viajan por loopback.
"""

from __future__ import annotations

import os

# Antes de cualquier importación de LiteLLM o LangSmith (ADR 0015 §1, informe de T2):
# - mapa de precios local: sin él, `import litellm` lo descarga de GitHub;
# - sin variables de LangSmith/LangChain: con `LANGSMITH_TRACING=true` en el entorno,
#   langchain-core envía el estado del grafo a api.smith.langchain.com;
# - sin `SSL_VERIFY`: `SSL_VERIFY=False` desactiva la verificación TLS en LiteLLM.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
for _name in list(os.environ):
    if _name.startswith(("LANGSMITH_", "LANGCHAIN_")):
        del os.environ[_name]
os.environ.pop("SSL_VERIFY", None)
# Como `faro_engine/__main__.py`: sin claves de sesión TLS en archivo ni raíces añadidas.
for _name in ("SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
    os.environ.pop(_name, None)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, TypedDict  # noqa: E402

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
FAKE_KEYS = {
    "openai": "sk-faro-smoke-" + "0" * 24,
    "gemini": "AIzaFaroSmoke" + "0" * 26,
}
F1B_MODULES = ("litellm", "langgraph", "apscheduler")
TIKTOKEN_DIRNAME = "faro_tiktoken"


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
        if self.path.endswith("/chat/completions"):
            body: dict[str, Any] = {
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
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        else:
            body = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "listo"}]},
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
    try:
        __import__(module)
    except ImportError:
        return False
    return True


# --- Comprobaciones -------------------------------------------------------------------


def check_engine(_tmp: Path) -> dict[str, Any]:
    from faro_engine import __version__
    from faro_engine.core.app import create_app
    from faro_engine.core.config import Settings

    token = secrets.token_urlsafe(32).encode()
    app = create_app(Settings(token=token, port=1, version=__version__))
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


CHECKS: tuple[tuple[str, Callable[[Path], dict[str, Any]], tuple[str, ...]], ...] = (
    ("engine", check_engine, ()),
    ("db", check_db, ()),
    ("vec", check_vec, ("sqlite_vec",)),
    ("tls", check_tls, ()),  # truststore es del núcleo de red (T2b): siempre exigida
    ("graph", check_graph, ("langgraph",)),
    ("scheduler", check_scheduler, ("apscheduler",)),
    ("litellm", check_litellm, ("litellm",)),
)


def _bundle_dir() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if isinstance(base, str) else None


def _tiktoken_dir(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    bundle = _bundle_dir()
    if bundle is not None and (bundle / TIKTOKEN_DIRNAME).is_dir():
        return bundle / TIKTOKEN_DIRNAME
    return None


def run_checks(
    require_f1b: bool, require_vec: bool, tiktoken_dir: Path | None = None
) -> dict[str, Any]:
    from faro_engine.core.logging import configure_logging

    configure_logging()  # logs JSON a stderr; stdout queda para el informe
    tiktoken = _tiktoken_dir(tiktoken_dir)
    if tiktoken is not None:
        os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(tiktoken)
    guard = _NetworkGuard()
    guard.install()
    # Mismo orden que T6: contexto del motor y, después, la inyección para LiteLLM.
    from faro_engine.net.tls import install_system_trust_for_libraries

    install_system_trust_for_libraries()
    report: dict[str, Any] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "tiktoken_dir": tiktoken is not None,
        "checks": {},
    }
    ok = True
    with tempfile.TemporaryDirectory(prefix="faro-bundle-smoke-") as tmp_name:
        for name, check, needs in CHECKS:
            required = (
                not needs or (require_f1b and name != "vec") or (name == "vec" and require_vec)
            )
            if needs and not all(_available(m) for m in needs) and not required:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--require-f1b", action="store_true", help="falla si faltan deps de F1b")
    parser.add_argument("--require-vec", action="store_true", help="falla si falta sqlite-vec")
    parser.add_argument(
        "--tiktoken-dir", type=Path, help="carpeta con cl100k_base (sin empaquetar)"
    )
    args = parser.parse_args(argv)
    report = run_checks(args.require_f1b, args.require_vec, args.tiktoken_dir)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
