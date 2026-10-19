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
    CHILD_GUARD_EXIT,
    CHILD_LOG_VAR,
    ChildAttemptLog,
    NetworkGuard,
    UnguardedChildError,
    child_pythonpath,
    fail_on_network_attempts,
    guard_off_options,
    is_python,
    unguarded_child,
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
    with unguarded_child("el sitecustomize roto sustituye al del bloqueo"):
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


# --- Hijos de Python sin el bloqueo (revisión de seguridad de T12, hallazgo 2) -------------

# Excepciones a propósito: cada `with unguarded_child(...)` de `tests/` y por qué. Una nueva
# exige añadirla aquí (y que la revise `revisor-seguridad`).
UNGUARDED_ALLOWED = {
    ("tests/test_bundle_smoke_scripts.py", "_smoke_child"): "prueba el bloqueo del propio humo",
    (
        "tests/test_child_network_guard.py",
        "test_sin_poder_instalar_el_gancho_la_prueba_falla",
    ): "sitecustomize roto a propósito",
    (
        "tests/test_child_network_guard.py",
        "test_sin_poder_anotar_el_fallo_el_hijo_sale_con_97",
    ): "archivo de intentos que no se puede escribir",
    (
        "tests/test_child_network_guard.py",
        "test_sin_poder_anotar_un_intento_el_hijo_sale_con_97",
    ): "archivo de intentos que no se puede escribir",
    (
        "tests/test_child_network_guard.py",
        "test_una_excepcion_explicita_deja_lanzar_sin_el_bloqueo",
    ): "control de la propia excepción",
}


def guarded_env() -> dict[str, str]:
    return dict(os.environ)


def spawn(
    guard: NetworkGuard, argv: list[str], env: dict[str, str] | None, event: str = "Popen"
) -> None:
    """El evento de auditoría de un lanzamiento, como lo recibe el gancho."""
    if event == "Popen":
        guard.check_spawn("subprocess.Popen", (None, argv, None, env))
    elif event == "posix_spawn":
        guard.check_spawn("os.posix_spawn", (argv[0], argv, env))
    elif event == "exec":
        guard.check_spawn("os.exec", (argv[0], argv, env))
    else:
        guard.check_spawn("os.spawn", (os.P_WAIT, argv[0], argv, env))


def fresh_guard() -> NetworkGuard:
    guard = NetworkGuard()
    guard.child_log = os.environ[CHILD_LOG_VAR]
    return guard


def without(*names: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


@pytest.mark.parametrize(
    ("argv", "env", "problem"),
    [
        ([sys.executable, "-I", "-c", "pass"], None, "con -I"),
        ([sys.executable, "-E", "-m", "faro_engine"], None, "con -E"),
        (["python3", "-S", "x.py"], None, "con -S"),
        (["python", "-IS", "-c", "pass"], None, "con -I -S"),
        (["python3.12", "-X", "dev", "-S", "-c", "pass"], None, "con -S"),
        (["pythonw.exe", "-Wignore", "-E", "x.py"], None, "con -E"),
        ([sys.executable, "-c", "pass"], "sin_log", f"sin {CHILD_LOG_VAR}"),
        ([sys.executable, "-c", "pass"], "sin_pythonpath", "PYTHONPATH"),
        ([sys.executable, "-c", "pass"], "otro_pythonpath", "PYTHONPATH"),
        ([sys.executable, "-c", "pass"], "otro_log", f"sin {CHILD_LOG_VAR}"),
    ],
)
@pytest.mark.parametrize("event", ["Popen", "posix_spawn", "exec", "spawn"])
def test_un_python_sin_el_bloqueo_no_se_lanza(
    argv: list[str], env: str | None, problem: str, event: str
) -> None:
    environ = {
        None: None,
        "sin_log": without(CHILD_LOG_VAR),
        "sin_pythonpath": without("PYTHONPATH"),
        "otro_pythonpath": {**os.environ, "PYTHONPATH": str(ENGINE_DIR)},
        "otro_log": {**os.environ, CHILD_LOG_VAR: "/otro/archivo.log"},
    }[env]
    guard = fresh_guard()
    with pytest.raises(UnguardedChildError, match="sin el bloqueo"):
        spawn(guard, argv, environ, event)
    [attempt] = guard.take()
    assert problem in attempt


@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "-c", "pass"],
        [sys.executable, "-c", "-I"],  # `-I` es el código, no una opción
        ["python", "-m", "pytest", "-S"],  # opción del módulo, no de Python
        ["python", "x.py", "-E"],
        ["python", "-X", "-S", "-c", "pass"],  # `-S` es el valor de `-X`
        ["npx", "--no-install", "wp-env", "run", "cli", "wp", "option", "get", "siteurl"],
        ["/usr/bin/git", "status"],
        ["pythonista", "-I"],
    ],
)
def test_lo_que_lleva_el_bloqueo_o_no_es_python_se_lanza(argv: list[str]) -> None:
    guard = fresh_guard()
    spawn(guard, argv, guarded_env())
    spawn(guard, argv, None)
    if not is_python(argv[0]):
        spawn(guard, argv, {})  # un programa que no es Python, con cualquier entorno
    assert guard.take() == []


def test_orden_de_windows_en_una_cadena() -> None:
    """En Windows, `subprocess` pasa la orden ya unida en una cadena."""
    guard = fresh_guard()
    command = f'"{sys.executable}" -I -c "print(1)"'
    with pytest.raises(UnguardedChildError):
        guard.check_spawn("subprocess.Popen", (None, command, None, None))
    assert "con -I" in guard.take()[0]
    guard.check_spawn("subprocess.Popen", (None, f'"{sys.executable}" -c pass', None, None))
    assert guard.take() == []


def test_sin_bloqueo_de_hijos_no_se_vigilan_los_lanzamientos() -> None:
    """Fuera de las pruebas (p. ej. en el humo del empaquetado) no hay archivo de intentos."""
    guard = NetworkGuard()
    spawn(guard, [sys.executable, "-I", "-c", "pass"], {})
    assert guard.take() == []


def test_un_python_lanzado_con_menos_i_hace_fallar_la_prueba(
    network_guard: NetworkGuard,
) -> None:
    """Control negativo con un lanzamiento real: no llega a arrancar y la prueba fallaría."""
    with pytest.raises(UnguardedChildError):
        subprocess.run([sys.executable, "-I", "-c", "pass"], check=False, timeout=TIMEOUT_S)
    with pytest.raises(UnguardedChildError):
        subprocess.run(
            [sys.executable, "-c", "pass"], env=without(CHILD_LOG_VAR), check=False, timeout=60
        )
    with pytest.raises(pytest.fail.Exception, match=r"con -I"):
        fail_on_network_attempts(network_guard, "en esta prueba")


def test_un_hijo_que_lanza_un_nieto_sin_el_bloqueo_hace_fallar_la_prueba(
    network_guard: NetworkGuard, child_network_log: ChildAttemptLog
) -> None:
    code = (
        "import contextlib, subprocess, sys\n"
        "with contextlib.suppress(OSError):\n"
        "    subprocess.run([sys.executable, '-S', '-c', 'pass'], timeout=60)\n"
    )
    assert run_child(code).returncode == 0
    assert "con -S" in failure_of(network_guard, child_network_log)


def test_una_excepcion_explicita_deja_lanzar_sin_el_bloqueo(
    network_guard: NetworkGuard,
) -> None:
    with unguarded_child("control de la excepción"):
        completed = subprocess.run(
            [sys.executable, "-I", "-c", "print('sin bloqueo')"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    assert completed.stdout.strip() == "sin bloqueo"
    assert network_guard.take() == []
    with pytest.raises(ValueError, match="por qué"), unguarded_child("  "):
        pass
    with pytest.raises(UnguardedChildError):  # fuera del `with`, otra vez vigilado
        subprocess.run([sys.executable, "-I", "-c", "pass"], check=False, timeout=TIMEOUT_S)
    network_guard.take()


def test_sin_poder_anotar_el_fallo_el_hijo_sale_con_97(tmp_path: Path) -> None:
    """Falla cerrado: el gancho no se instala y tampoco se puede anotar (el archivo de
    intentos es una carpeta)."""
    broken = tmp_path / "roto"
    broken.mkdir()
    (broken / "sitecustomize.py").write_text(
        (CHILD_GUARD_DIR / "sitecustomize.py")
        .read_text(encoding="utf-8")
        .replace('"network_guard.py"', '"no_existe.py"'),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(broken), CHILD_LOG_VAR: str(tmp_path)}
    with unguarded_child("archivo de intentos que no se puede escribir"):
        completed = run_child("print('no debo llegar aquí')", env=env)
    assert completed.returncode == CHILD_GUARD_EXIT
    assert completed.stdout == ""


def test_sin_poder_anotar_un_intento_el_hijo_sale_con_97(tmp_path: Path) -> None:
    env = {**os.environ, CHILD_LOG_VAR: str(tmp_path)}
    code = (
        "import contextlib, socket\n"
        "with contextlib.suppress(OSError):\n"
        "    socket.getaddrinfo('api.openai.com', 443)\n"
        "print('no debo llegar aquí')\n"
    )
    with unguarded_child("archivo de intentos que no se puede escribir"):
        completed = run_child(code, env=env)
    assert completed.returncode == CHILD_GUARD_EXIT
    assert completed.stdout == ""


def test_opciones_que_quitan_el_bloqueo() -> None:
    assert guard_off_options(["python", "-I", "-E", "-S", "-c", "x"]) == ["-I", "-E", "-S"]
    assert guard_off_options(["python", "-bIS"]) == ["-I", "-S"]
    assert guard_off_options(["python", "--check-hash-based-pycs", "always", "x.py"]) == []
    assert guard_off_options(["python", "--", "-I"]) == []
    assert guard_off_options(["python"]) == []


@pytest.mark.parametrize(
    ("program", "python"),
    [
        (sys.executable, True),
        ("python", True),
        ("/usr/bin/python3", True),
        ("python3.12", True),
        ("C:\\Python312\\pythonw.exe", True),
        ("PYTHON.EXE", True),
        ("npx", False),
        ("uv", False),
        ("pythonista", False),
        ("", False),
    ],
)
def test_que_cuenta_como_python(program: str, python: bool) -> None:
    assert is_python(program) is python


def _unguarded_uses() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(ENGINE_DIR.joinpath("tests").rglob("*.py")):
        if path.name == "network_guard.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "unguarded_child"
                ):
                    found.add((path.relative_to(ENGINE_DIR).as_posix(), function.name))
    return found


def test_las_excepciones_al_bloqueo_de_hijos_son_las_de_la_lista() -> None:
    assert _unguarded_uses() == set(UNGUARDED_ALLOWED)
