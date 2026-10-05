"""`scripts/manual_llm_check.py` no desvía la clave a otro host (revisión de seguridad de T2).

El usuario ejecuta ese script con su clave real. Estas pruebas usan claves falsas y la red
bloqueada salvo loopback: un servidor en loopback hace de host "desviado" (lo que pondría un
`OPENAI_BASE_URL` del entorno o de un `.env`) y no debe recibir ninguna conexión. Las
pruebas de proceso aparte importan LiteLLM (~7 s cada una).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import ENGINE_DIR
from tests.deps.helpers import PROBE_TIMEOUT_S
from tests.deps.manual_check_probe import DOTENV_MARKER, load_script

# Claves falsas partidas para que no parezcan secretos (gitleaks).
FAKE_KEYS = {
    "openai": "sk-" + "faro-test-" + "0" * 24,
    "anthropic": "sk-ant-" + "faro-test-" + "0" * 24,
    "gemini": "AIza" + "FaroTest" + "0" * 27,
}
MODELS = {"openai": "openai/gpt-x", "anthropic": "anthropic/claude-x", "gemini": "gemini/gemini-x"}
OFFICIAL_HOSTS = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "gemini": "generativelanguage.googleapis.com",
}
# LiteLLM 1.104 descarga `cl100k_base` antes de llamar a Anthropic y Gemini (informe de T2).
TIKTOKEN_HOST = "openaipublic.blob.core.windows.net"
REDIRECT_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_BASE",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_BASE",
)
script = load_script()


class Trap:
    """Servidor TCP en loopback que solo cuenta las conexiones que recibe."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = int(self.sock.getsockname()[1])
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.sock.close()


@pytest.fixture
def trap() -> Iterator[Trap]:
    server = Trap()
    try:
        yield server
    finally:
        server.close()


def _redirect_env(port: int, keylog: Path) -> dict[str, str]:
    """Todo lo que intentaría desviar la clave o debilitar TLS, apuntando a la trampa."""
    env = dict.fromkeys(REDIRECT_VARS, f"http://127.0.0.1:{port}/v1")
    env |= {
        "LITELLM_MODE": "DEV",
        "LITELLM_LOG": "DEBUG",
        "SSLKEYLOGFILE": str(keylog),
        "SSL_VERIFY": "False",
        "OPENAI_API_KEY": "sk-" + "otra-" + "1" * 20,
    }
    return env


def run_harness(
    cwd: Path, env: dict[str, str], *args: str, stdin: str = ""
) -> tuple[dict[str, Any], str]:
    """Lanza `tests.deps.manual_check_probe` con `python -c` desde `cwd` (ver su docstring)."""
    base = {k: v for k, v in os.environ.items() if not script._is_scrubbed(k)}
    base.pop("LITELLM_MODE", None)
    base["PYTHONPATH"] = str(ENGINE_DIR)
    base["PYTHONIOENCODING"] = "utf-8"
    base |= env
    code = "import sys; from tests.deps.manual_check_probe import main; sys.exit(main())"
    completed = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd,
        env=base,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT_S,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, completed.stderr[-3000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith("FARO_PROBE ")]
    assert len(lines) == 1, completed.stdout[-3000:]
    return json.loads(lines[0].removeprefix("FARO_PROBE ")), output


@pytest.fixture
def workdir(tmp_path: Path, trap: Trap) -> Path:
    """Directorio de trabajo cuyo padre tiene un `.env` que desvía la clave a la trampa."""
    lines = [f"{name}=http://127.0.0.1:{trap.port}/v1" for name in REDIRECT_VARS]
    lines.append(f"{DOTENV_MARKER}=cargado")
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    child = tmp_path / "trabajo"
    child.mkdir()
    return child


# --- En proceso aparte, con LiteLLM real ----------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_la_clave_solo_va_al_host_oficial(provider: str, trap: Trap, workdir: Path) -> None:
    keylog = workdir / "keylog.txt"
    report, output = run_harness(
        workdir,
        _redirect_env(trap.port, keylog),
        "run",
        provider,
        "--completion",
        MODELS[provider],
        stdin=FAKE_KEYS[provider] + "\n",
    )
    assert report["code"] == 0
    # Ni el entorno ni el `.env` del directorio padre desvían nada a la trampa.
    assert trap.connections == 0
    assert report["dotenv_marker"] is None
    # Solo se intentó el host oficial (y la descarga del vocabulario, sin clave).
    assert OFFICIAL_HOSTS[provider] in report["attempts"]
    assert set(report["attempts"]) <= {OFFICIAL_HOSTS[provider], TIKTOKEN_HOST}
    assert report["unsafe_after"] == []
    # Nada escribe las claves de sesión TLS y la salida nunca contiene la clave.
    assert not keylog.exists()
    assert FAKE_KEYS[provider] not in output
    assert "[litellm   ] FALLO" in output


def test_motivo_un_env_en_un_directorio_padre_si_se_carga_en_modo_dev(workdir: Path) -> None:
    """Sin `LITELLM_MODE=PRODUCTION`, `import litellm` carga el `.env` del directorio padre."""
    report, _ = run_harness(workdir, {}, "control_dotenv")
    assert report["dotenv_marker"] == "cargado"


# --- En este proceso, sin importar LiteLLM ---------------------------------------------


@pytest.fixture
def environ() -> Iterator[None]:
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.mark.usefixtures("environ")
def test_limpieza_quita_las_variables_peligrosas_y_fija_las_obligatorias() -> None:
    dangerous = [
        *REDIRECT_VARS,
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "LITELLM_LOG",
        "LITELLM_MODE",
        "SSL_VERIFY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "SSLKEYLOGFILE",
        "LANGSMITH_TRACING",
        "LANGCHAIN_API_KEY",
    ]
    os.environ.update(dict.fromkeys(dangerous, "x"))
    os.environ["FARO_OTRA"] = "se queda"
    assert set(dangerous) <= set(script.unsafe_variables())

    script.clean_environment()

    assert script.unsafe_variables() == []
    assert os.environ["LITELLM_MODE"] == "PRODUCTION"
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["FARO_OTRA"] == "se queda"
    script.check_environment()


def test_variables_obligatorias_con_otro_valor_son_inseguras() -> None:
    env = {"LITELLM_MODE": "DEV", "LITELLM_LOCAL_MODEL_COST_MAP": "True", "PATH": "x"}
    assert script.unsafe_variables(env) == ["LITELLM_MODE"]


@pytest.mark.usefixtures("environ")
def test_comprobacion_falla_si_queda_una_variable() -> None:
    script.clean_environment()
    os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:1/v1"
    with pytest.raises(script.UnsafeEnvironmentError, match="OPENAI_BASE_URL"):
        script.check_environment()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://api.openai.com.evil.test/v1",
        "https://127.0.0.1/v1",
        "https://api.openai.com:8443/v1",
    ],
)
def test_solo_https_al_host_oficial(url: str) -> None:
    with pytest.raises(script.UnsafeEnvironmentError):
        script._official_url("openai", url)


@pytest.mark.parametrize("provider", sorted(OFFICIAL_HOSTS))
def test_destinos_fijos_son_oficiales(provider: str) -> None:
    config = script.PROVIDERS[provider]
    assert config.host == OFFICIAL_HOSTS[provider]
    assert script._official_url(provider, config.models_url) == config.models_url
    assert script._official_url(provider, config.api_base) == config.api_base


@contextlib.contextmanager
def fake_litellm(tmp_path: Path, source: str) -> Iterator[None]:
    """Paquete `litellm` falso cuya importación ejecuta `source` (p. ej. un `load_dotenv`)."""
    package = tmp_path / "fake" / "litellm"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(source, encoding="utf-8")
    saved = sys.modules.pop("litellm", None)
    sys.path.insert(0, str(package.parent))
    try:
        yield
    finally:
        sys.path.remove(str(package.parent))
        sys.modules.pop("litellm", None)
        if saved is not None:
            sys.modules["litellm"] = saved


@pytest.mark.usefixtures("environ")
def test_variables_que_reaparecen_al_importar_se_vuelven_a_quitar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("truststore.inject_into_ssl", lambda: None)
    source = "import os\nos.environ['OPENAI_BASE_URL'] = 'http://127.0.0.1:1/v1'\n"
    with fake_litellm(tmp_path, source):
        module = script.import_litellm()
    assert isinstance(module, types.ModuleType)
    assert "OPENAI_BASE_URL" not in os.environ
    assert module.api_base is None
    assert module.callbacks == []
    assert module.turn_off_message_logging is True


@pytest.mark.usefixtures("environ")
def test_import_abortado_si_la_comprobacion_falla(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("truststore.inject_into_ssl", lambda: None)
    real_clean = script.clean_environment
    calls: list[int] = []

    def clean_only_before_import() -> None:
        calls.append(1)
        if len(calls) == 1:
            real_clean()

    monkeypatch.setattr(script, "clean_environment", clean_only_before_import)
    source = "import os\nos.environ['ANTHROPIC_BASE_URL'] = 'http://127.0.0.1:1'\n"
    with fake_litellm(tmp_path, source), pytest.raises(script.UnsafeEnvironmentError):
        script.import_litellm()


@pytest.mark.usefixtures("environ")
def test_main_aborta_sin_pedir_la_clave_si_el_entorno_no_queda_limpio(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(script, "clean_environment", lambda: None)
    os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:1/v1"

    def read_key(_prompt: str) -> str:
        raise AssertionError("no debe pedir la clave")

    assert script.main(["openai"], read_key=read_key) == script.EXIT_UNSAFE_ENV
    assert "ABORTADO" in capsys.readouterr().out


@pytest.mark.usefixtures("environ")
def test_main_devuelve_error_si_la_llamada_se_aborta(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = FAKE_KEYS["openai"]
    monkeypatch.setattr(script, "list_models", lambda *_args: None)

    def unsafe() -> None:
        raise script.UnsafeEnvironmentError("OPENAI_BASE_URL")

    monkeypatch.setattr(script, "import_litellm", unsafe)
    code = script.main(["openai", "--completion", "openai/x"], read_key=lambda _p: key)
    assert code == script.EXIT_UNSAFE_ENV
    output = capsys.readouterr().out
    assert "ABORTADO" in output
    assert key not in output
