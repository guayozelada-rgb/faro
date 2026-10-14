"""El bloqueo de red llega a los procesos hijos de las pruebas (condición T11-C2, revisión de
seguridad de T11, hallazgo 3c).

Controles negativos: un hijo (o un nieto) que intenta salir de loopback hace **fallar** la
prueba aunque se trague el error. Las direcciones son de documentación (RFC 5737) y los
nombres, de proveedores reales que nunca se llegan a resolver: si el bloqueo fallara, la
conexión no llegaría a ningún equipo real.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import CHILD_NETWORK_LOG, ENGINE_DIR
from tests.network_guard import (
    CHILD_GUARD_DIR,
    CHILD_LOG_VAR,
    ChildAttemptLog,
    NetworkGuard,
    child_pythonpath,
    fail_on_network_attempts,
)

OUTSIDE_IP = "203.0.113.10"
TIMEOUT_S = 60


def run_child(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """`python -c code`; sin `env`, con el del proceso de pruebas (como los demás hijos)."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=TIMEOUT_S,
        check=False,
    )


def failure_of(guard: NetworkGuard, log: ChildAttemptLog) -> str:
    """El mensaje con el que el *fixture* automático haría fallar la prueba."""
    with pytest.raises(pytest.fail.Exception) as failed:
        fail_on_network_attempts(guard, "en esta prueba", log)
    return str(failed.value)


def test_el_entorno_de_las_pruebas_lleva_el_bloqueo_a_los_hijos() -> None:
    assert os.environ[CHILD_LOG_VAR] == str(CHILD_NETWORK_LOG.path)
    assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(CHILD_GUARD_DIR)
    assert (CHILD_GUARD_DIR / "sitecustomize.py").is_file()


def test_un_hijo_que_sale_a_internet_hace_fallar_la_prueba(
    network_guard: NetworkGuard, child_network_log: ChildAttemptLog
) -> None:
    code = f"import socket\nsocket.create_connection(('{OUTSIDE_IP}', 443), timeout=3)\n"
    completed = run_child(code)
    assert completed.returncode == 1
    assert "red bloqueada" in completed.stderr  # el hijo recibió el error
    message = failure_of(network_guard, child_network_log)
    assert f"socket.connect:{OUTSIDE_IP}" in message
    assert child_network_log.take() == ([], [])  # ya se recogieron


def test_un_hijo_que_se_traga_el_error_tambien_hace_fallar_la_prueba(
    network_guard: NetworkGuard, child_network_log: ChildAttemptLog
) -> None:
    """Lo que hacen LiteLLM (reintentos) y LangSmith (hilo propio): el hijo sale con 0."""
    code = (
        "import _socket, contextlib, socket\n"
        "with contextlib.suppress(OSError):\n"
        "    socket.getaddrinfo('api.anthropic.com', 443)\n"
        "s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)\n"
        "with contextlib.suppress(OSError):\n"
        f"    s.connect_ex(('{OUTSIDE_IP}', 80))\n"
        "print('sigo vivo')\n"
    )
    completed = run_child(code)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "sigo vivo"
    message = failure_of(network_guard, child_network_log)
    assert "socket.getaddrinfo:api.anthropic.com" in message
    assert f"socket.connect:{OUTSIDE_IP}" in message  # también desde C (`_socket`)


def test_un_hijo_que_sale_por_el_proxy_del_entorno_hace_fallar_la_prueba(
    network_guard: NetworkGuard, child_network_log: ChildAttemptLog
) -> None:
    """Un proxy en loopback (como el del contenedor de los agentes) reenvía a internet."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = int(server.getsockname()[1])
        env = {**os.environ, "HTTPS_PROXY": f"http://127.0.0.1:{port}"}
        code = f"import socket\nsocket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
        completed = run_child(code, env=env)
    assert completed.returncode == 1
    assert "proxy del entorno" in completed.stderr
    message = failure_of(network_guard, child_network_log)
    assert f"127.0.0.1:{port} (proxy del entorno)" in message


def test_un_nieto_tambien_lleva_el_bloqueo(
    network_guard: NetworkGuard, child_network_log: ChildAttemptLog
) -> None:
    grandchild = "import socket; socket.getaddrinfo('api.openai.com', 443)"
    code = (
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, '-c', {grandchild!r}], check=False, timeout=60)\n"
    )
    assert run_child(code).returncode == 0
    message = failure_of(network_guard, child_network_log)
    assert "socket.getaddrinfo:api.openai.com" in message


def test_loopback_en_un_hijo_sigue_funcionando_y_el_bloqueo_llego(
    child_network_log: ChildAttemptLog,
) -> None:
    code = (
        "import socket, sys, threading\n"
        "server = socket.create_server(('127.0.0.1', 0))\n"
        "port = server.getsockname()[1]\n"
        "serve = lambda: server.accept()[0].sendall(b'hola')\n"
        "threading.Thread(target=serve, daemon=True).start()\n"
        "with socket.create_connection(('127.0.0.1', port), timeout=5) as conn:\n"
        "    print(conn.recv(4).decode())\n"
        "socket.getaddrinfo('localhost', 80)\n"
        f"socket.getaddrinfo('{OUTSIDE_IP}', 80)\n"  # IP literal: sin DNS
        "print(sys.modules['faro_test_network_guard'].GUARD.installed)\n"
    )
    completed = run_child(code)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["hola", "True"]
    attempts, started = child_network_log.take()
    assert attempts == []
    assert len(started) == 1


def test_el_motor_lanzado_en_las_pruebas_lleva_el_bloqueo(
    child_network_log: ChildAttemptLog,
) -> None:
    """`python -m faro_engine` (como en las pruebas de protocolo) arranca con el gancho."""
    completed = subprocess.run(
        [sys.executable, "-m", "faro_engine", "--host", "0.0.0.0"],  # sale con código 2
        cwd=ENGINE_DIR,
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 2
    attempts, started = child_network_log.take()
    assert attempts == []
    assert len(started) == 1


def test_sin_poder_instalar_el_gancho_la_prueba_falla(
    network_guard: NetworkGuard, child_network_log: ChildAttemptLog, tmp_path: Path
) -> None:
    """Falla cerrado: un hijo donde el `sitecustomize` no puede cargar el gancho lo anota."""
    broken = tmp_path / "roto"
    broken.mkdir()
    (broken / "sitecustomize.py").write_text(
        (CHILD_GUARD_DIR / "sitecustomize.py")
        .read_text(encoding="utf-8")
        .replace('"network_guard.py"', '"no_existe.py"'),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(broken)}
    assert run_child("print('hola')", env=env).returncode == 0
    message = failure_of(network_guard, child_network_log)
    assert "sin bloqueo" in message


def test_la_sonda_con_su_propio_bloqueo_sigue_viendo_el_intento(
    child_network_log: ChildAttemptLog,
) -> None:
    """Convivencia: `offline_probe.block_network` (lo usan las sondas que esperan un intento,
    p. ej. LangSmith o la descarga de `cl100k_base`) lo intercepta en Python antes que el
    gancho: la sonda lo anota y la prueba no falla por él."""
    code = (
        "import contextlib, json, socket\n"
        "from tests.deps.offline_probe import ATTEMPTS, block_network\n"
        "block_network()\n"
        "with contextlib.suppress(OSError):\n"
        "    socket.getaddrinfo('api.smith.langchain.com', 443)\n"
        "with contextlib.suppress(OSError):\n"
        f"    socket.create_connection(('{OUTSIDE_IP}', 443), timeout=3)\n"
        "print(json.dumps(ATTEMPTS))\n"
    )
    completed = run_child(code)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f'["api.smith.langchain.com", "{OUTSIDE_IP}"]'
    attempts, started = child_network_log.take()
    assert attempts == []
    assert len(started) == 1


def test_el_sitecustomize_tapado_se_sigue_ejecutando(tmp_path: Path) -> None:
    """Si la distribución trae su propio `sitecustomize`, se ejecuta después del bloqueo."""
    other = tmp_path / "otro"
    other.mkdir()
    (other / "sitecustomize.py").write_text("import os\nos.environ['FARO_OTRO_SC'] = 'si'\n")
    env = {**os.environ, "PYTHONPATH": child_pythonpath(other)}
    code = (
        "import os, sys\n"
        "print(os.environ.get('FARO_OTRO_SC'), 'faro_test_network_guard' in sys.modules)\n"
    )
    completed = run_child(code, env=env)
    assert completed.stdout.split() == ["si", "True"], completed.stderr


def test_lineas_del_archivo_de_intentos(tmp_path: Path) -> None:
    log = ChildAttemptLog(tmp_path / "intentos.log")
    assert log.take() == ([], [])  # el archivo aún no existe
    log.path.write_text(
        "start 10\nattempt 10 socket.connect:203.0.113.10\nerror 11 ImportError\nattempt 12 x",
        encoding="utf-8",
    )
    assert log.take() == (
        ["socket.connect:203.0.113.10 (pid 10)", "sin bloqueo (pid 11): ImportError"],
        ["10"],
    )
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(":443\n")  # la línea a medias se completa
    assert log.take() == (["x:443 (pid 12)"], [])


def test_child_pythonpath_pone_el_bloqueo_primero() -> None:
    assert child_pythonpath() == str(CHILD_GUARD_DIR)
    assert child_pythonpath(ENGINE_DIR, "otra").split(os.pathsep) == [
        str(CHILD_GUARD_DIR),
        str(ENGINE_DIR),
        "otra",
    ]


def _pythonpath_assignments(tree: ast.AST) -> list[ast.expr]:
    """Lugares donde se da valor a `PYTHONPATH`: `env["PYTHONPATH"] = …`, una clave
    `"PYTHONPATH"` en un diccionario literal o `setenv("PYTHONPATH", …)`."""

    def is_name(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Constant) and node.value == "PYTHONPATH"

    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found += [t for t in targets if isinstance(t, ast.Subscript) and is_name(t.slice)]
        elif isinstance(node, ast.Dict):
            found += [key for key in node.keys if key is not None and is_name(key)]
        elif isinstance(node, ast.Call) and node.args and is_name(node.args[0]):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if name in {"setenv", "putenv", "setdefault", "update"}:
                found.append(node)
    return found


def test_quien_cambia_pythonpath_en_las_pruebas_conserva_el_bloqueo() -> None:
    """Guarda estática: toda asignación a `PYTHONPATH` en `tests/` usa `child_pythonpath`."""
    offenders = []
    allowed = {"network_guard.py", Path(__file__).name}
    for path in sorted(ENGINE_DIR.joinpath("tests").rglob("*.py")):
        if path.name in allowed:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in _pythonpath_assignments(ast.parse("\n".join(lines))):
            line = lines[node.lineno - 1]
            if "child_pythonpath(" not in line:
                offenders.append(f"{path.relative_to(ENGINE_DIR)}:{node.lineno}")
    assert offenders == []


def test_la_guarda_estatica_detecta_cada_forma() -> None:
    code = (
        'env["PYTHONPATH"] = "x"\n'
        'env = {"PYTHONPATH": "x"}\n'
        'monkeypatch.setenv("PYTHONPATH", "x")\n'
        'env["PYTHONPATH"] = child_pythonpath("x")\n'
        'texto = "PYTHONPATH"\n'
    )
    found = _pythonpath_assignments(ast.parse(code))
    assert sorted(node.lineno for node in found) == [1, 2, 3, 4]
