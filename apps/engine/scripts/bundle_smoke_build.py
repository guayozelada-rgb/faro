"""Empaqueta `bundle_smoke.py` con PyInstaller `--onedir`, lo ejecuta y mide (spec F1b T2).

Criterios 5, 6 y 7 de ADR 0015 §6. Construye dos variantes con el mismo lock:

- `base`: el motor de F1a. Excluye los módulos de F1b, así que PyInstaller no recorre
  LiteLLM, LangGraph, APScheduler ni `sqlite-vec` ni sus dependencias. `truststore` sí
  entra: desde T2b es del núcleo de red (`faro_engine/net/tls.py`, ADR 0012).
- `full`: el motor con las dependencias de F1b, el hook de LiteLLM
  (`scripts/pyinstaller_hooks`), `sqlite-vec` y el vocabulario `cl100k_base` de tiktoken.

Cada ejecutable debe escribir en stdout **una sola** línea JSON con `ok: true` (stdout es el
canal del protocolo del motor). Falla si alguna prueba falla o si `full` pesa más de
`MAX_GROWTH_MIB` por encima de `base`. Resumen en Markdown en stdout y, en la CI, en
`$GITHUB_STEP_SUMMARY`.

Uso (desde `apps/engine`, con el grupo `bundle`):

    uv sync --locked --group bundle
    uv run --locked --group bundle python scripts/bundle_smoke_build.py

`cl100k_base.tiktoken` se descarga una vez de la URL oficial de tiktoken y se comprueba su
SHA-256 (el mismo que exige tiktoken). Sin red, `--tiktoken-file` usa una copia local.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

ENGINE_DIR: Final = Path(__file__).resolve().parents[1]
SCRIPTS_DIR: Final = ENGINE_DIR / "scripts"
ENTRY: Final = SCRIPTS_DIR / "bundle_smoke.py"
HOOKS_DIR: Final = SCRIPTS_DIR / "pyinstaller_hooks"

MAX_GROWTH_MIB: Final = 150
MIB: Final = 1024 * 1024
RUN_TIMEOUT_S: Final = 300

# Vocabulario que LiteLLM 1.104 pide en las llamadas de Anthropic y Gemini y no incluye.
CL100K_URL: Final = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
CL100K_SHA256: Final = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
# Nombre del archivo en la caché de tiktoken: sha1 de la URL (tiktoken/load.py).
CL100K_CACHE_NAME: Final = hashlib.sha1(CL100K_URL.encode(), usedforsecurity=False).hexdigest()

F1B_TOP_MODULES: Final = (
    "litellm",
    "langgraph",
    "langchain_core",
    "langsmith",
    "apscheduler",
    "sqlite_vec",
)
# Puente nativo de LiteLLM (~45 MB): `acompletion` va por Python en 1.104 (`chat_completions`
# es `PYTHON_ONLY` en `litellm/rust_bridge/catalog.py`) y, si falta, LiteLLM usa Python.
LITELLM_NATIVE: Final = "litellm.rust_bridge._native"


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_tiktoken(out: Path, local_copy: Path | None) -> Path:
    """Carpeta con `cl100k_base` en el formato de caché de tiktoken, hash comprobado."""
    folder = out / "tiktoken"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / CL100K_CACHE_NAME
    if local_copy is not None:
        shutil.copyfile(local_copy, target)
    elif not target.exists() or _sha256(target) != CL100K_SHA256:
        try:
            import truststore  # almacén del sistema (antivirus que intercepta HTTPS)

            truststore.inject_into_ssl()
        except ImportError:  # pragma: no cover - truststore es dependencia del motor
            pass
        import httpx

        response = httpx.get(CL100K_URL, timeout=120, follow_redirects=False)
        response.raise_for_status()
        target.write_bytes(response.content)
    if _sha256(target) != CL100K_SHA256:
        target.unlink()
        raise SystemExit("cl100k_base.tiktoken no coincide con el SHA-256 esperado")
    return folder


def pyinstaller_args(
    variant: str, out: Path, tiktoken_dir: Path | None, *, with_native: bool = False
) -> list[str]:
    name = f"faro-engine-smoke-{variant}"
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(ENTRY),
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
        str(out / "build" / variant),
        "--specpath",
        str(out / "spec"),
        "--paths",
        str(ENGINE_DIR),
        # Migraciones .sql del paquete (README del motor).
        "--collect-data",
        "faro_engine",
    ]
    if variant == "base":
        for module in F1B_TOP_MODULES:
            args += ["--exclude-module", module]
    else:
        args += ["--additional-hooks-dir", str(HOOKS_DIR), "--collect-all", "sqlite_vec"]
        if not with_native:
            args += ["--exclude-module", LITELLM_NATIVE]
        if tiktoken_dir is not None:
            args += ["--add-data", f"{tiktoken_dir}{os.pathsep}faro_tiktoken"]
    return args


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


def executable(out: Path, variant: str) -> Path:
    name = f"faro-engine-smoke-{variant}"
    suffix = ".exe" if sys.platform == "win32" else ""
    return out / "dist" / name / f"{name}{suffix}"


def run_smoke(exe: Path, variant: str) -> dict[str, Any]:
    args = [str(exe)]
    if variant == "full":
        args += ["--require-f1b", "--require-vec"]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    started = time.perf_counter()
    completed = subprocess.run(
        args, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, env=env, check=False
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


def build(
    variant: str, out: Path, tiktoken_dir: Path | None, *, with_native: bool = False
) -> dict[str, Any]:
    started = time.perf_counter()
    _write(f"== PyInstaller ({variant})")
    # PyInstaller importa LiteLLM al analizarlo: sin esto, descarga el mapa de precios.
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    subprocess.run(
        pyinstaller_args(variant, out, tiktoken_dir, with_native=with_native),
        check=True,
        cwd=ENGINE_DIR,
        env=env,
    )
    build_seconds = round(time.perf_counter() - started, 1)
    folder = executable(out, variant).parent
    size, count = folder_size(folder)
    _write(f"== Prueba de humo ({variant})")
    smoke = run_smoke(executable(out, variant), variant)
    return {
        "variant": variant,
        "build_seconds": build_seconds,
        "bytes": size,
        "files": count,
        "largest": largest_entries(folder),
        "smoke": smoke,
    }


def markdown(results: dict[str, dict[str, Any]], growth: int | None) -> str:
    lines = [
        "## engine-bundle-smoke (PyInstaller --onedir)",
        "",
        "| Variante | Tamaño | Archivos | Build | Humo | Duración del humo |",
        "| --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for variant, data in results.items():
        smoke = data["smoke"]
        lines.append(
            f"| `{variant}` | {data['bytes'] / MIB:.1f} MiB | {data['files']} | "
            f"{data['build_seconds']} s | {'ok' if smoke['ok'] else 'FALLO'} | "
            f"{smoke['seconds']} s |"
        )
    if growth is not None:
        verdict = "ok" if growth <= MAX_GROWTH_MIB * MIB else "SUPERA EL LÍMITE"
        lines += [
            "",
            f"Aumento `full` - `base`: **{growth / MIB:.1f} MiB** "
            f"(límite {MAX_GROWTH_MIB} MiB, ADR 0015 §6 criterio 7): {verdict}.",
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
    parser = argparse.ArgumentParser(description="Empaqueta y prueba el motor (F1b T2).")
    parser.add_argument("--out", type=Path, default=ENGINE_DIR / "build" / "bundle-smoke")
    parser.add_argument("--variant", choices=["base", "full", "both"], default="both")
    parser.add_argument("--tiktoken-file", type=Path, help="copia local de cl100k_base.tiktoken")
    parser.add_argument(
        "--with-litellm-native",
        action="store_true",
        help=f"incluye {LITELLM_NATIVE} (por defecto se excluye)",
    )
    args = parser.parse_args(argv)

    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    variants = ["base", "full"] if args.variant == "both" else [args.variant]
    tiktoken_dir = prepare_tiktoken(out, args.tiktoken_file) if "full" in variants else None

    results = {
        variant: build(variant, out, tiktoken_dir, with_native=args.with_litellm_native)
        for variant in variants
    }
    growth = (
        results["full"]["bytes"] - results["base"]["bytes"]
        if {"base", "full"} <= results.keys()
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
