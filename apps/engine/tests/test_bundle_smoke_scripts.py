"""Scripts del humo del empaquetado (`scripts/bundle_smoke*.py`, condición T11-C3).

- `ENGINE_ENV_ALLOWLIST` de `bundle_smoke_build.py` es la misma lista que la del núcleo
  (`apps/desktop/src-tauri/src/engine/launcher.rs`) y las variables fijas del intérprete
  coinciden: el humo lanza el motor real con el mismo entorno que el núcleo.
- La lectura del PYZ del motor real falla con `sitecustomize`, `manual_*`, `respx`,
  `trustme`, `pytest` o `tests`, y con una lectura vacía (sin el motor dentro).
- `bundle_smoke.py` usa el gancho de auditoría de `tests/network_guard.py` (no parchea
  `socket`) y su control demuestra que bloquea; con un registro que no anota, el control
  falla.
- La lista de scripts manuales que el humo busca en el paquete está al día.

El empaquetado real (PyInstaller) no corre aquí: lo hace `engine-bundle-smoke` (manual, en
Windows) y, en local, `bundle_smoke_build.py` (`apps/engine/README.md`).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import ENGINE_DIR
from tests.network_guard import CHILD_ENV_VARS, unguarded_child

SCRIPTS = ENGINE_DIR / "scripts"
LAUNCHER_RS = ENGINE_DIR.parents[1] / "apps/desktop/src-tauri/src/engine/launcher.rs"
ALLOWLIST_RE = re.compile(
    r"pub const ENGINE_ENV_ALLOWLIST: &\[&str\] = &\[(?P<body>.*?)\];", re.DOTALL
)
FIXED_ENV_RE = re.compile(r'\.env\("(?P<name>[A-Z_]+)", "(?P<value>[^"]*)"\)')


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build = load("bundle_smoke_build")


def rust_allowlist(source: str) -> list[str]:
    match = ALLOWLIST_RE.search(source)
    assert match is not None, "no se encontró ENGINE_ENV_ALLOWLIST en launcher.rs"
    body = re.sub(r"//[^\n]*", "", match["body"])  # sin comentarios
    return re.findall(r'"([^"]+)"', body)


def rust_fixed_env(source: str) -> dict[str, str]:
    start = source.index("pub fn engine_command(")
    end = source.index("\n}\n", start)
    return {m["name"]: m["value"] for m in FIXED_ENV_RE.finditer(source[start:end])}


# --- Lista permitida del entorno del motor -------------------------------------------------


def test_la_lista_permitida_del_humo_es_la_del_nucleo() -> None:
    source = LAUNCHER_RS.read_text(encoding="utf-8")
    names = rust_allowlist(source)
    assert len(names) == len(set(names)), "nombres repetidos en launcher.rs"
    assert set(names) == set(build.ENGINE_ENV_ALLOWLIST)


def test_las_variables_fijas_del_interprete_son_las_del_nucleo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = rust_fixed_env(LAUNCHER_RS.read_text(encoding="utf-8"))
    assert fixed == {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    for name in list(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("PYTHONHOME", "/malicioso")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    assert build.engine_env() == {"PATH": "/bin", **fixed}


def test_la_comparacion_detecta_una_lista_distinta() -> None:
    """Control: una variable de más en el núcleo (o de menos) rompe la igualdad."""
    source = LAUNCHER_RS.read_text(encoding="utf-8")
    changed = source.replace('    "TZ",\n', '    "TZ",\n    "PYTHONPATH", // de más\n', 1)
    assert changed != source
    assert set(rust_allowlist(changed)) != set(build.ENGINE_ENV_ALLOWLIST)
    assert "PYTHONPATH" in rust_allowlist(changed)
    assert "TZ" not in rust_allowlist(source.replace('    "TZ",\n', "", 1))
    with pytest.raises(AssertionError, match="ENGINE_ENV_ALLOWLIST"):
        rust_allowlist("pub const OTRA: &[&str] = &[];")


# --- Lectura del PYZ del motor real --------------------------------------------------------


GOOD = {
    "pyz": ["faro_engine.core.app", "faro_engine.llm.service", "litellm.proxy", "pytest_x"],
    "base_library": ["encodings.utf_8", "collections.abc"],
    "carchive": ["PYZ.pyz", "pyiboot01_bootstrap", "pyi_rth_inspect", "__main__"],
    "internal": ["base_library.zip", "certifi-2026.7.22.dist-info", "python3.12", "lib-dynload"],
    "internal_files": ["certifi/py.typed", "faro_engine/llm/tiktoken/x.py", "PYZ-00.pyz"],
}


def test_el_paquete_del_motor_sin_prohibidos_pasa() -> None:
    report = build.archive_report(GOOD)
    assert report["ok"] is True
    assert report["forbidden"] == []
    assert report["engine_marker"] is True
    assert report["pyz_modules"] == 4


@pytest.mark.parametrize(
    ("where", "name"),
    [
        ("pyz", "sitecustomize"),
        ("pyz", "manual_llm_check"),
        ("pyz", "scripts.manual_tls_check"),
        ("pyz", "respx"),
        ("pyz", "respx._transports.default"),
        ("pyz", "trustme"),
        ("pyz", "pytest"),
        ("pyz", "_pytest.config"),
        ("pyz", "tests.network_guard"),
        ("base_library", "sitecustomize"),
        ("internal", "respx-0.23.1.dist-info"),
        ("internal", "trustme-1.2.1.dist-info"),
        ("internal", "sitecustomize.py"),
        # Revisión de seguridad de T12, hallazgo 4: CArchive (script y runtime hooks) y
        # cualquier `*.py*` de `_internal`, con su ruta relativa.
        ("carchive", "sitecustomize"),
        ("carchive", "manual_rth_hook"),
        ("carchive", "pyi_rth_manual_algo"),
        ("internal_files", "tests/network_guard.py"),
        ("internal_files", "faro_engine/manual_tokens.pyc"),
        ("internal_files", "respx/__init__.py"),
        ("internal_files", "otro/sitecustomize.py"),
    ],
)
def test_cada_prohibido_hace_fallar_el_humo(where: str, name: str) -> None:
    names: dict[str, Any] = {key: list(value) for key, value in GOOD.items()}
    names[where].append(name)
    report = build.archive_report(names)
    assert report["ok"] is False
    assert report["forbidden"] != []


def test_una_lectura_vacia_no_pasa() -> None:
    """Control positivo: sin `faro_engine.core.app` en el PYZ, la lectura no vale."""
    report = build.archive_report({"pyz": [], "base_library": [], "internal": []})
    assert report == {
        "ok": False,
        "pyz_modules": 0,
        "base_library_modules": 0,
        "carchive_entries": 0,
        "internal_entries": 0,
        "internal_python_files": 0,
        "engine_marker": False,
        "forbidden": [],
    }


def test_un_ejecutable_ilegible_es_un_fallo(tmp_path: Path) -> None:
    exe = tmp_path / "faro-engine"
    exe.write_bytes(b"no es un ejecutable de PyInstaller")
    report = build.check_engine_archive(exe)
    assert report["ok"] is False
    assert "error" in report


def test_nombres_de_entradas_de_internal() -> None:
    assert build.internal_entry_module("respx-0.23.1.dist-info") == "respx"
    assert build.internal_entry_module("sitecustomize.py") == "sitecustomize"
    assert build.internal_entry_module("_pytest") == "_pytest"


# --- El humo usa el gancho de las pruebas ------------------------------------------------------


def _smoke_tree() -> ast.Module:
    return ast.parse((SCRIPTS / "bundle_smoke.py").read_text(encoding="utf-8"))


def test_el_humo_no_parchea_socket() -> None:
    patched = [
        ast.unparse(target)
        for node in ast.walk(_smoke_tree())
        if isinstance(node, ast.Assign)
        for target in node.targets
        if ast.unparse(target).startswith("socket.")
    ]
    assert patched == []
    imports = {
        node.module
        for node in ast.walk(_smoke_tree())
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "tests.network_guard" in imports


def _smoke_child(code: str) -> dict[str, Any]:
    """`bundle_smoke` en un proceso sin el bloqueo heredado de las pruebas: el que se prueba
    es el que instala el propio humo."""
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_VARS}
    prologue = (
        "import json, sys\n"
        "sys.argv = ['bundle_smoke', '--keylog-control']\n"  # sin el prólogo del motor
        "sys.path.insert(0, 'scripts')\n"
        "import bundle_smoke as smoke\n"
        "guard = smoke.install_network_guard()\n"
    )
    # Excepción a propósito (lista en `tests/test_child_network_guard.py`): el bloqueo que
    # se prueba es el que instala el humo, y el de los hijos lo taparía (gancho anterior).
    with unguarded_child("se prueba el bloqueo que instala el propio humo"):
        completed = subprocess.run(
            [sys.executable, "-c", prologue + code],
            cwd=ENGINE_DIR,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr[-3000:]
    result: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def test_el_control_del_humo_demuestra_que_el_gancho_bloquea() -> None:
    report = _smoke_child(
        "control = smoke.network_guard_control(guard)\n"
        "print(json.dumps({**control, 'module': type(guard).__module__}))\n"
    )
    assert report["ok"] is True
    assert report["kind"] == "audit_hook"
    assert report["blocked"] == ["getaddrinfo", "connect"]
    assert report["module"] == "tests.network_guard"


def test_el_control_del_humo_falla_si_el_bloqueo_no_anota() -> None:
    """Control negativo: con un registro que no anota nada, el humo no da el bloqueo por
    bueno (aunque el gancho real siga bloqueando en ese proceso)."""
    report = _smoke_child(
        "class Mute:\n"
        "    def take(self):\n"
        "        return []\n"
        "print(json.dumps(smoke.network_guard_control(Mute())))\n"
    )
    assert report["ok"] is False
    assert report["recorded"] == []


def test_los_scripts_manuales_que_busca_el_humo_estan_al_dia() -> None:
    manual = next(
        node.value
        for node in ast.walk(_smoke_tree())
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "MANUAL_SCRIPTS" for t in node.targets)
    )
    listed = sorted(ast.literal_eval(manual))
    assert listed == sorted(p.stem for p in SCRIPTS.glob("manual_*.py"))


def test_el_apreton_tls_del_humo_funciona_con_truststore_inyectado() -> None:
    """Regresión (T12): `8c78aa6` fijaba `minimum_version` en el servidor de la biblioteca
    estándar y, con `ssl.SSLContext` ya sustituido por el de truststore (como en el motor
    congelado), la comprobación `keylog` de `full` fallaba con `AttributeError: … '_ctx'`.
    En otro proceso: la inyección cambia `ssl` para todo el proceso."""
    code = (
        "import json, ssl, sys\n"
        "sys.argv = ['bundle_smoke', '--keylog-control']\n"
        "sys.path.insert(0, 'scripts')\n"
        "import bundle_smoke as smoke\n"
        "from faro_engine.net.tls import install_system_trust_for_libraries\n"
        "install_system_trust_for_libraries()\n"
        "assert type(ssl.create_default_context()).__name__ == '_LockedContext'\n"
        "print(json.dumps({'version': smoke.tls_handshake_in_loopback(None)}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ENGINE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    version = json.loads(completed.stdout.strip().splitlines()[-1])["version"]
    assert version in {"TLSv1.2", "TLSv1.3"}
