"""Los umbrales de cobertura de F1b siguen exigidos (spec F1b §4.7 y fila T11 de §8.3).

`tests/conftest.py::pytest_runtestloop` exige el 95 % en cada módulo de
`[tool.faro.coverage] strict_modules` de `pyproject.toml`, y el trabajo `engine` de la CI
(dentro de `ci-ok`) ejecuta `pytest` con cobertura. Estas pruebas fallan si alguien quita
un módulo de la lista, baja el umbral o deja de medir la cobertura en la CI. Cambiar la
lista o el umbral es un cambio de spec (`arquitecto`, skill `pruebas-faro`).
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from tests.conftest import ENGINE_DIR

REPO_DIR = ENGINE_DIR.parents[1]
CI_WORKFLOW = REPO_DIR / ".github" / "workflows" / "ci.yml"

# Spec F1b §4.7: módulos que T11 debe exigir al 95 %.
SPEC_F1B_STRICT = (
    "faro_engine/llm/pricing.py",
    "faro_engine/llm/limits.py",
    "faro_engine/llm/service.py",
    "faro_engine/agents/framework/untrusted.py",
    "faro_engine/agents/framework/autonomy.py",
    "faro_engine/agents/framework/approvals.py",
    "faro_engine/agents/framework/checkpoint.py",
    "faro_engine/agents/framework/actions.py",
    "faro_engine/core/jobs/grants.py",
    "faro_engine/core/jobs/control.py",
)

# Los que añadieron T3 a T9 en sus informes (`docs/qa/2026-10-*-f1b-*.md`).
ADDED_BY_TASKS = {
    "T3": ("faro_engine/agents/grants.py",),
    "T4": tuple(
        f"faro_engine/core/store/{name}.py"
        for name in (
            "common",
            "runs",
            "approvals",
            "autonomy_rules",
            "schedules",
            "credentials",
            "settings",
            "site_summaries",
            "checkpoints",
            "run_control",
        )
    ),
    "T5": ("faro_engine/core/jobs/activity.py",),
    "T6": tuple(
        f"faro_engine/llm/{name}.py"
        for name in ("errors", "catalog", "hardening", "litellm_client")
    ),
    "T7": tuple(
        f"faro_engine/core/jobs/{name}.py"
        for name in ("queue", "worker", "scheduler", "recovery", "runner", "runtime")
    ),
    "T8": (
        *(
            f"faro_engine/agents/framework/{name}.py"
            for name in (
                "tools",
                "prompts",
                "steps",
                "state",
                "budget",
                "graph",
                "orchestrator",
                "tracing",
                "spec",
            )
        ),
        "faro_engine/agents/registry.py",
    ),
    "T9": (
        *(
            f"faro_engine/agents/site_summary/{name}.py"
            for name in (
                "actions",
                "estimate",
                "fake",
                "graph",
                "library",
                "limits",
                "saved",
                "schemas",
                "spec",
                "state",
                "tools",
            )
        ),
        "faro_engine/sites/reader.py",
        "faro_engine/sites/snapshot.py",
    ),
}


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ENGINE_DIR / "pyproject.toml").read_text(encoding="utf-8"))


def _strict() -> tuple[list[str], float]:
    section = _pyproject()["tool"]["faro"]["coverage"]
    return list(section["strict_modules"]), float(section["strict_fail_under"])


def test_umbral_estricto_del_95() -> None:
    _modules, threshold = _strict()
    assert threshold >= 95


@pytest.mark.parametrize("module", SPEC_F1B_STRICT)
def test_modulos_de_la_spec_f1b_en_strict_modules(module: str) -> None:
    modules, _threshold = _strict()
    assert module in modules


@pytest.mark.parametrize(
    "module", [m for task_modules in ADDED_BY_TASKS.values() for m in task_modules]
)
def test_modulos_de_t3_a_t9_en_strict_modules(module: str) -> None:
    modules, _threshold = _strict()
    assert module in modules


def test_strict_modules_existen_y_no_se_repiten() -> None:
    """Un nombre mal escrito daría 0 % y fallaría tarde; aquí falla con su nombre."""
    modules, _threshold = _strict()
    assert len(modules) == len(set(modules))
    missing = [m for m in modules if not (ENGINE_DIR / m).is_file()]
    assert missing == []


def test_pytest_mide_la_cobertura_por_defecto() -> None:
    """La CI ejecuta `pytest` sin opciones: la cobertura tiene que estar en `addopts`."""
    addopts = _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--cov=faro_engine" in addopts
    assert "--cov-fail-under=80" in addopts
    assert "--no-cov" not in addopts


def test_la_ci_exige_los_umbrales_dentro_de_ci_ok() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    engine_runs = [step.get("run", "") for step in jobs["engine"]["steps"]]
    pytest_runs = [run for run in engine_runs if "pytest" in run]
    assert pytest_runs == ["uv run --locked pytest"]
    assert set(jobs["engine"]["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    }
    assert "engine" in jobs["ci-ok"]["needs"]
    assert jobs["ci-ok"]["if"] == "always()"
