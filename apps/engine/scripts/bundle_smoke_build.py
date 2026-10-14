"""Empaqueta el motor con PyInstaller `--onedir`, lo ejecuta y mide (spec F1b T2 y T11).

Criterios 5, 6 y 7 de ADR 0015 §6 y condiciones de T11 (T6-C5; condiciones 12, 14 y 17 del
informe de T2; T8 y T9). Construye tres variantes con el mismo lock y las mismas opciones
(`engine_pyinstaller_args`, las que debe usar el build de release):

- `engine`: **el motor real**, `faro-engine`, con `faro_engine/__main__.py` como entrada (sin
  otro script de entrada: lo primero que corre es su prólogo, que quita `SSLKEYLOGFILE`).
  Se lanza como lo hará el núcleo (`--host 127.0.0.1 --port 0 --data-dir …`, directorio de
  trabajo = su carpeta de instalación, entorno mínimo, token y `db_key` por stdin) y se
  comprueba: `--fake-llm`, `--allow-local-sites` y `--dev` salen con código 2 sin escribir
  nada en stdout; arranca con `SSLKEYLOGFILE` definida y no crea el archivo; escribe
  `ready`; `/health` responde con la base lista; `/agents` lista `site_summary` (registro y
  prompts en el paquete); sin token, 401; `shutdown` → código 0; stdout solo con `ready`.
- `full`: `scripts/bundle_smoke.py` con todas las dependencias, más `respx`, `trustme` y
  `sqlite-vec` (solo de esta prueba): ver sus comprobaciones. Además, el control de la
  condición 17 (`--keylog-control`).
- `base`: `scripts/bundle_smoke.py` sin la capa de IA ni los agentes (LiteLLM, LangGraph,
  LangChain, LangSmith, tiktoken, el SDK de OpenAI y `sqlite-vec`). APScheduler y `tzlocal`
  entran: desde T7 los importa el arranque del motor (`core/jobs`). Sirve para medir el
  aumento de tamaño (`engine` menos `base`, límite `MAX_GROWTH_MIB`).

**Build sin red** (condición 12): PyInstaller corre con `scripts/pyinstaller_offline` en
`PYTHONPATH` (su `sitecustomize` bloquea, en el proceso y en los subprocesos de análisis,
toda conexión fuera de loopback y la anota) y con los mapas de LiteLLM locales. El build
falla si hubo algún intento. `cl100k_base` ya no se descarga: es el archivo del paquete
(`faro_engine/llm/tiktoken`) y se comprueba su SHA-256 antes de empaquetar.

Cada ejecutable de humo debe escribir en stdout **una sola** línea JSON con `ok: true`
(stdout es el canal del protocolo del motor). Resumen en Markdown en stdout y, en la CI, en
`$GITHUB_STEP_SUMMARY`; `summary.json` en la carpeta de salida.

Uso (desde `apps/engine`). `engine` y `base` se construyen **sin** el grupo `dev`, como el
build de release: con él, PyInstaller arrastra paquetes de desarrollo que algún módulo
importa de forma opcional (p. ej. `cryptography`, que solo trae `trustme`). `full` necesita
`dev` (`respx`, `trustme`, `sqlite-vec`):

    uv sync --locked --no-dev --group bundle
    uv run --locked --no-dev --group bundle python scripts/bundle_smoke_build.py \\
        --variant base --variant engine --out build/bundle-smoke/release
    uv sync --locked --group bundle
    uv run --locked --group bundle python scripts/bundle_smoke_build.py \\
        --variant full --out build/bundle-smoke/full
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Final

from faro_engine.llm.hardening import CL100K_CACHE_NAME, CL100K_SHA256, TIKTOKEN_DIR

ENGINE_DIR: Final = Path(__file__).resolve().parents[1]
SCRIPTS_DIR: Final = ENGINE_DIR / "scripts"
SMOKE_ENTRY: Final = SCRIPTS_DIR / "bundle_smoke.py"
# El motor real: su propio `__main__.py`, sin un script de entrada distinto (condición 17).
ENGINE_ENTRY: Final = ENGINE_DIR / "faro_engine" / "__main__.py"
ENGINE_NAME: Final = "faro-engine"
HOOKS_DIR: Final = SCRIPTS_DIR / "pyinstaller_hooks"
OFFLINE_DIR: Final = SCRIPTS_DIR / "pyinstaller_offline"
TIKTOKEN_FILE: Final = TIKTOKEN_DIR / CL100K_CACHE_NAME

VARIANTS: Final = ("base", "full", "engine")
MAX_GROWTH_MIB: Final = 150
MIB: Final = 1024 * 1024
RUN_TIMEOUT_S: Final = 300
READY_TIMEOUT_S: Final = 60
SHUTDOWN_TIMEOUT_S: Final = 30

# Fuera de `base`: la capa de IA y los agentes (no se importan al arrancar el motor).
LLM_TOP_MODULES: Final = (
    "litellm",
    "langgraph",
    "langchain_core",
    "langsmith",
    "tiktoken",
    "openai",
    "sqlite_vec",
)
# Herramientas de desarrollo que el análisis estático arrastra (≈3 MiB, informe de T2).
DEV_ONLY_MODULES: Final = ("mypy", "mypyc", "pytest", "_pytest")
# Solo para la prueba de humo `full` (dobles y certificado de prueba).
SMOKE_EXTRAS: Final = ("respx", "trustme")
# Puente nativo de LiteLLM (~45 MB): `acompletion` va por Python en 1.104 (`chat_completions`
# es `PYTHON_ONLY` en `litellm/rust_bridge/catalog.py`) y, si falta, LiteLLM usa Python.
LITELLM_NATIVE: Final = "litellm.rust_bridge._native"
# Variables del entorno que el núcleo deja pasar al motor (`ENGINE_ENV_ALLOWLIST`).
ENGINE_ENV_ALLOWLIST: Final = frozenset(
    {
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PROGRAMDATA",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "PATH",
        "TEMP",
        "TMP",
        "HOME",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
    }
)
REJECTED_FLAGS: Final = (
    ("--fake-llm", "config.fake_llm_rejected"),
    ("--allow-local-sites", "config.local_sites_rejected"),
    ("--dev", "config.dev_rejected"),
)
# LangSmith activado en el entorno de `full`: el marco de agentes debe apagarlo (T8).
LANGSMITH_ON: Final = {
    "LANGSMITH_TRACING": "true",
    "LANGSMITH_API_KEY": "lsv2_" + "0" * 20,
    "LANGCHAIN_TRACING_V2": "true",
}


def _write(text: str) -> None:
    # En UTF-8 siempre: la consola de los ejecutores de Windows usa cp1252 y el resumen lleva
    # caracteres como «→» o «ñ» (fallaba con `UnicodeEncodeError` al terminar `engine`).
    sys.stdout.flush()
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_tiktoken() -> None:
    """`cl100k_base` del paquete, con el SHA-256 que exige tiktoken, antes de empaquetar."""
    if not TIKTOKEN_FILE.is_file() or _sha256(TIKTOKEN_FILE) != CL100K_SHA256:
        raise SystemExit("faro_engine/llm/tiktoken: cl100k_base falta o no es el esperado")


def engine_pyinstaller_args(
    entry: Path, name: str, out: Path, *, with_native: bool = False
) -> list[str]:
    """Opciones de PyInstaller del motor. El build de release debe usar estas mismas."""
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(entry),
        "--onedir",
        "--noconfirm",
        "--clean",
        "--console",
        "--log-level",
        "WARN",
        "--name",
        name,
        "--distpath",
        str(out / "dist"),
        "--workpath",
        str(out / "build" / name),
        "--specpath",
        str(out / "spec"),
        "--paths",
        str(ENGINE_DIR),
        # Datos del paquete: migraciones `.sql`, prompts `.md` de los agentes, `models.json`
        # y `cl100k_base` (`llm/tiktoken`). Sin ellos el motor no arranca (T9).
        "--collect-data",
        "faro_engine",
        # `agents/framework/graph.py` y `llm/litellm_client.py` se importan con `importlib`
        # (T8, T6): el análisis estático no los ve.
        "--collect-submodules",
        "faro_engine",
        # LiteLLM (lista permitida, condición 12) y tiktoken (`tiktoken_ext`).
        "--additional-hooks-dir",
        str(HOOKS_DIR),
    ]
    # `site` importa `sitecustomize`: sin esto entraría el del build sin red.
    for module in (*DEV_ONLY_MODULES, "sitecustomize"):
        args += ["--exclude-module", module]
    if not with_native:
        args += ["--exclude-module", LITELLM_NATIVE]
    return args


def pyinstaller_args(variant: str, out: Path, *, with_native: bool = False) -> list[str]:
    if variant == "engine":
        return engine_pyinstaller_args(ENGINE_ENTRY, ENGINE_NAME, out, with_native=with_native)
    args = engine_pyinstaller_args(
        SMOKE_ENTRY, f"faro-engine-smoke-{variant}", out, with_native=with_native
    )
    if variant == "base":
        for module in (*LLM_TOP_MODULES, *SMOKE_EXTRAS):
            args += ["--exclude-module", module]
    else:
        args += ["--collect-all", "sqlite_vec"]
        for module in SMOKE_EXTRAS:
            args += ["--hidden-import", module]
    return args


def executable(out: Path, variant: str) -> Path:
    name = ENGINE_NAME if variant == "engine" else f"faro-engine-smoke-{variant}"
    suffix = ".exe" if sys.platform == "win32" else ""
    return out / "dist" / name / f"{name}{suffix}"


def folder_size(folder: Path) -> tuple[int, int]:
    files = [p for p in folder.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), len(files)


def largest_entries(folder: Path, limit: int = 12) -> list[tuple[str, int]]:
    internal = folder / "_internal"
    root = internal if internal.is_dir() else folder
    sizes: list[tuple[str, int]] = []
    for entry in root.iterdir():
        size = (
            entry.stat().st_size
            if entry.is_file()
            else sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
        )
        sizes.append((entry.name, size))
    return sorted(sizes, key=lambda item: item[1], reverse=True)[:limit]


def build_env(network_log: Path) -> dict[str, str]:
    """Entorno de PyInstaller: sin red (ver `scripts/pyinstaller_offline`) y LiteLLM local."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
    env.update(
        {
            "PYTHONPATH": str(OFFLINE_DIR),
            "FARO_BUILD_NETWORK_LOG": str(network_log),
            # PyInstaller importa LiteLLM al analizarlo: sin esto descarga el mapa de precios
            # y las cabeceras beta de Anthropic, y lee un `.env` (modo DEV).
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
            "LITELLM_MODE": "PRODUCTION",
            "HF_HUB_OFFLINE": "1",
        }
    )
    return env


def read_network_log(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    starts = [line for line in lines if line.startswith("start ")]
    attempts = [line for line in lines if not line.startswith("start ")]
    return {"processes": len(starts), "attempts": attempts}


def smoke_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in {"PYTHONPATH", "FARO_BUILD_NETWORK_LOG", "SSLKEYLOGFILE"}
    }
    env.update(extra or {})
    return env


def engine_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Como `engine_command` del núcleo: solo la lista permitida y las fijas del intérprete."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENGINE_ENV_ALLOWLIST}
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra or {})
    return env


def run_json(args: Sequence[str], env: dict[str, str], cwd: Path) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT_S,
        env=env,
        cwd=cwd,
        check=False,
    )
    elapsed = round(time.perf_counter() - started, 1)
    lines = completed.stdout.splitlines()
    result: dict[str, Any] = {"exit_code": completed.returncode, "seconds": elapsed}
    if len(lines) != 1:
        result["ok"] = False
        result["error"] = f"stdout debe tener una línea JSON y tiene {len(lines)}"
        result["stdout_head"] = completed.stdout[:2000]
        result["stderr_tail"] = completed.stderr[-2000:]
        return result
    report = json.loads(lines[0])
    result["report"] = report
    result["ok"] = completed.returncode == 0 and bool(report.get("ok"))
    if not result["ok"]:
        result["stderr_tail"] = completed.stderr[-2000:]
    return result


def full_expectations(report: dict[str, Any]) -> list[str]:
    """Lo que `full` debe demostrar, además de `ok` (que ninguna comprobación sea vacía)."""
    checks = report.get("checks", {})
    problems = []
    if checks.get("agents", {}).get("langsmith_env_before", 0) < 1:
        problems.append("agents: LangSmith no estaba activado en el entorno")
    adapter = checks.get("adapter", {})
    if adapter.get("cl100k_in_bundle") is not True or not isinstance(adapter.get("calls"), dict):
        problems.append("adapter: sin cl100k_base del paquete o sin llamadas")
    if checks.get("keylog", {}).get("status") != "ok":
        problems.append("keylog: sin comprobar")
    return problems


def run_smoke(exe: Path, variant: str, out: Path) -> dict[str, Any]:
    if variant == "base":
        return run_json([str(exe)], smoke_env(), exe.parent)
    keylog_dir = out / "keylog"
    shutil.rmtree(keylog_dir, ignore_errors=True)
    keylog_dir.mkdir(parents=True)
    env = smoke_env({"SSLKEYLOGFILE": str(keylog_dir / "full.txt"), **LANGSMITH_ON})
    result = run_json([str(exe), "--require-f1b", "--require-vec"], env, exe.parent)
    problems = full_expectations(result.get("report", {}))
    control = run_json(
        [str(exe), "--keylog-control"],
        smoke_env({"SSLKEYLOGFILE": str(keylog_dir / "control.txt")}),
        exe.parent,
    )
    result["keylog_control"] = control
    if not control["ok"]:
        problems.append("keylog: el control no escribió las claves (la prueba sería vacía)")
    if problems:
        result["ok"] = False
        result["problems"] = problems
    return result


class _Lines:
    """Lee líneas de una tubería en un hilo, con espera acotada."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: IO[bytes]) -> None:
        for line in iter(stream.readline, b""):
            self._queue.put(line)
        self._queue.put(None)

    def next(self, timeout: float) -> bytes | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def rest(self, timeout: float) -> list[bytes]:
        self._thread.join(timeout=timeout)
        lines: list[bytes] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return lines
            if item is not None:
                lines.append(item)


def _get(port: int, path: str, token: str | None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
    finally:
        conn.close()
    try:
        data: Any = json.loads(body)
    except ValueError:
        data = None
    return response.status, data


def check_rejected_flags(exe: Path, data_dir: Path) -> dict[str, Any]:
    """Los modos de desarrollo salen con código 2 en el ejecutable, sin escribir `ready`."""
    results: dict[str, Any] = {}
    for flag, event in REJECTED_FLAGS:
        args = [str(exe), "--host", "127.0.0.1", "--port", "0", "--data-dir", str(data_dir), flag]
        completed = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUN_TIMEOUT_S,
            env=engine_env(),
            cwd=exe.parent,
            check=False,
        )
        results[flag] = {
            "exit_code": completed.returncode,
            "stdout_empty": completed.stdout == "",
            "logged": event in completed.stderr,
        }
    return results


def check_engine_run(exe: Path, work: Path) -> dict[str, Any]:
    """Arranque real como lo hará el núcleo (protocolo de ADR 0004 y 0010)."""
    data_dir = work / "data"
    keylog = work / "keylog-engine.txt"
    stderr_path = work / "engine-stderr.log"
    keylog.unlink(missing_ok=True)
    token = secrets.token_urlsafe(32)
    profile = str(uuid.uuid4())
    key = secrets.token_hex(32)
    args = [str(exe), "--host", "127.0.0.1", "--port", "0", "--data-dir", str(data_dir)]
    result: dict[str, Any] = {}
    with stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=engine_env({"SSLKEYLOGFILE": str(keylog)}),
            # Como el lanzador de release: la carpeta de instalación (condición 14).
            cwd=exe.parent,
        )
        assert proc.stdin is not None  # noqa: S101 - PIPE
        assert proc.stdout is not None  # noqa: S101 - PIPE
        lines = _Lines(proc.stdout)
        try:
            db_key = {"event": "db_key", "profile": profile, "key": key}
            proc.stdin.write(token.encode() + b"\n" + json.dumps(db_key).encode() + b"\n")
            proc.stdin.flush()
            started = time.perf_counter()
            ready_raw = lines.next(READY_TIMEOUT_S)
            result["ready_seconds"] = round(time.perf_counter() - started, 1)
            ready = json.loads(ready_raw) if ready_raw else {}
            result["ready"] = ready.get("event") == "ready"
            port = int(ready.get("port", 0))
            if result["ready"]:
                status, health = _get(port, "/health", token)
                result["health"] = status == 200 and (health or {}).get("status") == "ok"
                result["database"] = (health or {}).get("database", {}).get("state")
                result["unauthorized"] = _get(port, "/health", None)[0] == 401
                status, agents = _get(port, "/agents", token)
                kinds = [item.get("kind") for item in (agents or {}).get("items", [])]
                result["agents"] = kinds if status == 200 else f"HTTP {status}"
            proc.stdin.write(b'{"event":"shutdown"}\n')
            proc.stdin.flush()
            result["exit_code"] = proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        result["extra_stdout_lines"] = len(lines.rest(timeout=5))
    result["keylog_created"] = keylog.exists()
    result["profile_db"] = (data_dir / "profiles" / f"{profile}.db").is_file()
    result["ok"] = "error" not in result and all(
        (
            result.get("ready"),
            result.get("health"),
            result.get("database") == "ready",
            result.get("unauthorized"),
            result.get("agents") == ["site_summary"],
            result.get("exit_code") == 0,
            result["extra_stdout_lines"] == 0,
            not result["keylog_created"],
            result["profile_db"],
        )
    )
    if not result["ok"]:
        result["stderr_tail"] = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    return result


def run_engine(exe: Path, out: Path) -> dict[str, Any]:
    work = out / "engine-run"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    started = time.perf_counter()
    rejected = check_rejected_flags(exe, work / "rejected-data")
    run = check_engine_run(exe, work)
    rejected_ok = all(
        r["exit_code"] == 2 and r["stdout_empty"] and r["logged"] for r in rejected.values()
    )
    return {
        "ok": rejected_ok and run["ok"],
        "seconds": round(time.perf_counter() - started, 1),
        "rejected_flags": rejected,
        "run": run,
    }


def build(variant: str, out: Path, *, with_native: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    _write(f"== PyInstaller ({variant})")
    network_log = out / f"network-{variant}.log"
    network_log.unlink(missing_ok=True)
    subprocess.run(
        pyinstaller_args(variant, out, with_native=with_native),
        check=True,
        cwd=ENGINE_DIR,
        env=build_env(network_log),
    )
    build_seconds = round(time.perf_counter() - started, 1)
    network = read_network_log(network_log)
    exe = executable(out, variant)
    size, count = folder_size(exe.parent)
    _write(f"== Prueba de humo ({variant})")
    smoke = run_engine(exe, out) if variant == "engine" else run_smoke(exe, variant, out)
    # Sin intentos de red y con el bloqueo en al menos dos procesos (PyInstaller y uno de
    # sus subprocesos de análisis): si no, el bloqueo no llegó a donde importa.
    if network["attempts"] or network["processes"] < 2:
        smoke["ok"] = False
    return {
        "variant": variant,
        "build_seconds": build_seconds,
        "build_network": network,
        "bytes": size,
        "files": count,
        "largest": largest_entries(exe.parent),
        "smoke": smoke,
    }


def markdown(results: dict[str, dict[str, Any]], growth: int | None) -> str:
    lines = [
        "## engine-bundle-smoke (PyInstaller --onedir)",
        "",
        "| Variante | Tamaño | Archivos | Build | Red en el build | Humo | Duración del humo |",
        "| --- | ---: | ---: | ---: | --- | --- | ---: |",
    ]
    for variant, data in results.items():
        smoke = data["smoke"]
        network = data["build_network"]
        net = (
            f"{len(network['attempts'])} intentos ({network['processes']} procesos)"
            if network["attempts"]
            else f"ninguna ({network['processes']} procesos vigilados)"
        )
        lines.append(
            f"| `{variant}` | {data['bytes'] / MIB:.1f} MiB | {data['files']} | "
            f"{data['build_seconds']} s | {net} | {'ok' if smoke['ok'] else 'FALLO'} | "
            f"{smoke['seconds']} s |"
        )
    if growth is not None:
        verdict = "ok" if growth <= MAX_GROWTH_MIB * MIB else "SUPERA EL LÍMITE"
        lines += [
            "",
            f"Aumento `engine` - `base`: **{growth / MIB:.1f} MiB** "
            f"(límite {MAX_GROWTH_MIB} MiB, ADR 0015 §6 criterio 7): {verdict}.",
        ]
    engine = results.get("engine", {}).get("smoke")
    if engine is not None:
        run = engine["run"]
        lines += [
            "",
            "Motor real (`faro-engine`): "
            + ", ".join(
                f"`{flag}` → {info['exit_code']}" for flag, info in engine["rejected_flags"].items()
            )
            + f"; `ready` en {run.get('ready_seconds')} s; `/agents` → {run.get('agents')}; "
            f"`shutdown` → {run.get('exit_code')}; `SSLKEYLOGFILE` creado: "
            f"{run.get('keylog_created')}.",
        ]
    for variant, data in results.items():
        lines += ["", f"<details><summary>Lo más grande de `{variant}`</summary>", ""]
        lines += [f"- `{name}`: {size / MIB:.1f} MiB" for name, size in data["largest"]]
        checks = data["smoke"].get("report", {}).get("checks", {})
        if checks:
            lines += [
                "",
                "Comprobaciones: "
                + ", ".join(
                    f"`{name}` {info.get('status')}" for name, info in sorted(checks.items())
                ),
            ]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Empaqueta y prueba el motor (F1b T2 y T11).")
    parser.add_argument("--out", type=Path, default=ENGINE_DIR / "build" / "bundle-smoke")
    parser.add_argument(
        "--variant",
        choices=VARIANTS,
        action="append",
        help="variante a construir (se puede repetir); por defecto, las tres",
    )
    parser.add_argument(
        "--with-litellm-native",
        action="store_true",
        help=f"incluye {LITELLM_NATIVE} (por defecto se excluye)",
    )
    args = parser.parse_args(argv)

    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    check_tiktoken()
    variants = [v for v in VARIANTS if v in (args.variant or VARIANTS)]
    results = {
        variant: build(variant, out, with_native=args.with_litellm_native) for variant in variants
    }
    growth = (
        results["engine"]["bytes"] - results["base"]["bytes"]
        if {"base", "engine"} <= results.keys()
        else None
    )
    summary = markdown(results, growth)
    (out / "summary.json").write_text(
        json.dumps({"results": results, "growth_bytes": growth}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _write(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(summary)

    failed = [v for v, data in results.items() if not data["smoke"]["ok"]]
    for variant in failed:
        _write(f"FALLO en {variant}: {json.dumps(results[variant]['smoke'], ensure_ascii=False)}")
    too_big = growth is not None and growth > MAX_GROWTH_MIB * MIB
    return 1 if failed or too_big else 0


if __name__ == "__main__":
    sys.exit(main())
