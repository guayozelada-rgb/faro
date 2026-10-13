"""Reglas de los workflows de GitHub en un repositorio público (ADR 0005, spec F1b §4.7 y §7,
condición T6-C5).

- Los scripts `scripts/manual_*` piden una clave real y solo los ejecuta el usuario en su
  equipo: ningún workflow ni script de npm los lanza.
- Sin `pull_request_target` ni `workflow_run`, sin secretos, sin runners propios,
  `GITHUB_TOKEN` de solo lectura y acciones fijadas por SHA completo.
- `engine-bundle-smoke` solo se lanza a mano, en Windows, y no entra en `ci-ok`.
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
WORKFLOWS = sorted(WORKFLOWS_DIR.glob("*.yml"))
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
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
def test_acciones_fijadas_por_sha_completo(path: Path) -> None:
    for job_name, step in _steps(_load(path)):
        uses = step.get("uses")
        if uses is not None and not uses.startswith("./"):
            assert PINNED_ACTION.match(uses), (job_name, uses)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_sin_credenciales_persistentes(path: Path) -> None:
    for job_name, step in _steps(_load(path)):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False, job_name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_sin_secretos_ni_runners_propios(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "secrets." not in text
    for job in _load(path)["jobs"].values():
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
