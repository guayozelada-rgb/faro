"""Reglas de los workflows de GitHub en un repositorio público (ADR 0005, spec F1b §4.7 y §7,
condición T6-C5).

- Los scripts `scripts/manual_*` piden una clave real y solo los ejecuta el usuario en su
  equipo: ningún workflow ni script de npm los lanza.
- Sin `pull_request_target` ni `workflow_run`, sin secretos, sin runners propios,
  `GITHUB_TOKEN` de solo lectura y acciones fijadas por SHA completo.
- `engine-bundle-smoke` solo se lanza a mano, en Windows, y no entra en `ci-ok`.

Revisión de seguridad de T11 (hallazgo 5): también los `*.yaml`; ninguna referencia a
`secrets` (palabra completa, en claves y valores, no en comentarios); ninguna expresión
`${{ … }}` dentro de un `run:` (inyección de comandos: los valores pasan por `env:`); los
workflows reutilizables (`jobs.<id>.uses`) también fijados por SHA. Cada regla tiene su
prueba de control con un workflow inventado que la incumple.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from tests.conftest import ENGINE_DIR

REPO_DIR = ENGINE_DIR.parents[1]
WORKFLOWS_DIR = REPO_DIR / ".github" / "workflows"
WORKFLOWS = sorted([*WORKFLOWS_DIR.glob("*.yml"), *WORKFLOWS_DIR.glob("*.yaml")])
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
SECRETS_WORD = re.compile(r"\bsecrets\b")
# Único permiso de escritura: subir el SARIF de CodeQL desde un trabajo que no ejecuta
# código del repositorio (revisión de seguridad de F0 T12).
ALLOWED_WRITE = {("codeql.yml", "upload", "security-events")}


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) lee la clave `on` como `True`.
    raw = workflow["on"] if "on" in workflow else workflow[True]
    if isinstance(raw, str):
        raw = [raw]
    return raw if isinstance(raw, dict) else dict.fromkeys(raw)


def _steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step)
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
    ]


def _strings(node: Any) -> list[str]:
    """Todas las claves y valores de texto (los comentarios ya no están tras cargar)."""
    if isinstance(node, dict):
        return [t for k, v in node.items() for t in (*_strings(k), *_strings(v))]
    if isinstance(node, list):
        return [t for item in node for t in _strings(item)]
    return [node] if isinstance(node, str) else []


def secret_references(workflow: dict[str, Any]) -> list[str]:
    return [text for text in _strings(workflow) if SECRETS_WORD.search(text)]


def expressions_in_run(workflow: dict[str, Any]) -> list[str]:
    return [
        f"{job}: {step.get('name', step['run'][:40])}"
        for job, step in _steps(workflow)
        if "${{" in str(step.get("run", ""))
    ]


def unpinned_uses(workflow: dict[str, Any]) -> list[str]:
    """`uses` de pasos y de trabajos (workflows reutilizables) sin SHA completo."""
    uses = [str(step["uses"]) for _job, step in _steps(workflow) if "uses" in step]
    uses += [str(job["uses"]) for job in workflow["jobs"].values() if "uses" in job]
    return [u for u in uses if not u.startswith("./") and not PINNED_ACTION.match(u)]


BAD_WORKFLOW: dict[str, Any] = {
    "permissions": {"contents": "read"},
    "jobs": {
        "build": {
            "runs-on": "ubuntu-latest",
            "steps": [
                {"uses": "actions/checkout@v7"},
                {"name": "Saludo", "run": "echo ${{ github.event.pull_request.title }}"},
                {"run": "deploy", "env": {"TOKEN": "${{ secrets.DEPLOY_TOKEN }}"}},
            ],
        },
        "reutilizado": {"uses": "otra/org/.github/workflows/x.yml@main", "secrets": "inherit"},
    },
}


def test_hay_workflows() -> None:
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "codeql.yml", "engine-bundle-smoke.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_ningun_workflow_ejecuta_los_scripts_manuales(path: Path) -> None:
    runs = [step.get("run", "") for _job, step in _steps(_load(path))]
    assert [run for run in runs if "manual_" in run] == []


def test_ningun_script_de_npm_ejecuta_los_scripts_manuales() -> None:
    package = json.loads((REPO_DIR / "package.json").read_text(encoding="utf-8"))
    assert [s for s in package["scripts"].values() if "manual_" in s] == []


def test_los_scripts_manuales_existen_y_no_son_modulos_del_motor() -> None:
    """Viven en `scripts/` (fuera de `faro_engine`): no entran en el motor empaquetado."""
    manual = sorted(p.name for p in (ENGINE_DIR / "scripts").glob("manual_*.py"))
    assert manual == ["manual_llm_check.py", "manual_llm_models_check.py", "manual_tls_check.py"]
    assert list((ENGINE_DIR / "faro_engine").rglob("manual_*")) == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_sin_disparadores_peligrosos(path: Path) -> None:
    triggers = _triggers(_load(path))
    assert "pull_request_target" not in triggers
    assert "workflow_run" not in triggers


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_token_de_solo_lectura(path: Path) -> None:
    workflow = _load(path)
    assert workflow["permissions"] == {"contents": "read"}
    for job_name, job in workflow["jobs"].items():
        for scope, level in (job.get("permissions") or {}).items():
            if level != "read":
                assert (path.name, job_name, scope) in ALLOWED_WRITE, (job_name, scope, level)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_acciones_y_workflows_reutilizables_fijados_por_sha_completo(path: Path) -> None:
    assert unpinned_uses(_load(path)) == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_sin_expresiones_dentro_de_run(path: Path) -> None:
    assert expressions_in_run(_load(path)) == []


def test_las_reglas_detectan_un_workflow_que_las_incumple() -> None:
    """Controles: cada regla encuentra lo que debe encontrar."""
    assert unpinned_uses(BAD_WORKFLOW) == [
        "actions/checkout@v7",
        "otra/org/.github/workflows/x.yml@main",
    ]
    assert expressions_in_run(BAD_WORKFLOW) == ["build: Saludo"]
    assert secret_references(BAD_WORKFLOW) == ["${{ secrets.DEPLOY_TOKEN }}", "secrets"]
    assert secret_references({"jobs": {"x": {"name": "mis_secrets", "x": "secretos"}}}) == []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_sin_credenciales_persistentes(path: Path) -> None:
    for job_name, step in _steps(_load(path)):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, job_name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_sin_secretos_ni_runners_propios(path: Path) -> None:
    workflow = _load(path)
    assert secret_references(workflow) == []
    for job in workflow["jobs"].values():
        assert "self-hosted" not in json.dumps(job.get("runs-on"))


def test_engine_bundle_smoke_solo_a_mano_en_windows_y_fuera_de_ci_ok() -> None:
    workflow = _load(WORKFLOWS_DIR / "engine-bundle-smoke.yml")
    assert _triggers(workflow) == {"workflow_dispatch": None}
    [(name, job)] = workflow["jobs"].items()
    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] <= 45
    ci = _load(WORKFLOWS_DIR / "ci.yml")
    assert name not in ci["jobs"]
    assert name not in ci["jobs"]["ci-ok"]["needs"]
