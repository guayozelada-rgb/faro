"""Adaptador real sobre LiteLLM 1.104.0, en proceso aparte (spec F1b §9.2; informe de T2
§12, condiciones 1-6, 14, 15 y 18-21; ADR 0012, actualización 2026-10-06).

Cada escenario de `tests/llm/adapter_probe.py` corre una vez (unos 7 s por importar
LiteLLM) con la red bloqueada salvo loopback y claves falsas; las pruebas leen su informe.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from faro_engine.llm import hardening
from tests.conftest import ENGINE_DIR
from tests.deps.helpers import PROBE_TIMEOUT_S
from tests.llm.adapter_probe import KEYS, QUOTA_CASES
from tests.llm.test_manual_llm_check import Trap

PROVIDERS = ("openai", "anthropic", "gemini")
KEY_HEADER = {"openai": "authorization", "anthropic": "x-api-key", "gemini": "x-goog-api-key"}
OFFICIAL = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash-lite:generateContent"
    ),
}
REDIRECT_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_BASE",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_BASE",
)


def run_probe(
    cwd: Path, scenario: str, env: dict[str, str] | None = None
) -> tuple[dict[str, Any], str, str]:
    base = {k: v for k, v in os.environ.items() if not hardening.is_scrubbed(k)}
    base["PYTHONPATH"] = str(ENGINE_DIR)
    base["PYTHONIOENCODING"] = "utf-8"
    base.update(env or {})
    code = "import sys; from tests.llm.adapter_probe import main; sys.exit(main())"
    completed = subprocess.run(
        [sys.executable, "-c", code, scenario],
        cwd=cwd,
        env=base,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    lines = [line for line in completed.stdout.splitlines() if line.startswith("FARO_PROBE ")]
    assert len(lines) == 1, completed.stdout[-3000:]
    return json.loads(lines[0].removeprefix("FARO_PROBE ")), completed.stdout, completed.stderr


@functools.cache
def matrix() -> dict[str, Any]:
    work = Path(os.environ.get("TMP", ENGINE_DIR)) / "faro-adapter-matrix"
    work.mkdir(parents=True, exist_ok=True)
    report, out, err = run_probe(work, "matrix")
    report["_output"] = out + err
    return report


@functools.cache
def tls_spy() -> dict[str, Any]:
    work = Path(os.environ.get("TMP", ENGINE_DIR)) / "faro-adapter-tls"
    work.mkdir(parents=True, exist_ok=True)
    report, _, _ = run_probe(work, "tls_spy")
    return report


# --- Errores y reintentos con respuestas grabadas (respx) --------------------------------

EXPECTED_KINDS = {
    "unauthorized": "invalid_key",
    "forbidden": "permission",
    "rate_limited": "rate_limited",
    "server_error": "server_error",
    "unavailable": "server_error",
    "bad_request": "bad_request",
    "timeout": "timeout",
    "connect": "unreachable",
    "redirect": "redirect",
}
EXPECTED_QUOTA = {
    ("openai", "quota"): "insufficient_quota",
    ("openai", "bad_key"): "invalid_key",
    ("anthropic", "quota"): "insufficient_quota",
    ("anthropic", "billing"): "insufficient_quota",
    ("gemini", "quota"): "insufficient_quota",
    ("gemini", "bad_key"): "invalid_key",
}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_respuesta_correcta_tokens_y_cabecera_de_la_clave(provider: str) -> None:
    report = matrix()
    ok = report["results"][provider]["ok"]
    assert ok == {"text": "hola", "tokens": [11, 2], "finish_reason": "stop"}
    request = report["requests"][provider][0]
    assert request["url"] == OFFICIAL[provider]
    assert request["key_headers"] == [KEY_HEADER[provider]]
    assert request["key_in_url"] is False


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(("case", "kind"), sorted(EXPECTED_KINDS.items()))
def test_mapeo_de_errores_de_cada_proveedor(provider: str, case: str, kind: str) -> None:
    result = matrix()["results"][provider][case]
    assert result["kind"] == kind
    expected_retry = 3.0 if case == "rate_limited" else None
    assert result["retry_after"] == expected_retry


@pytest.mark.parametrize(("pair", "kind"), sorted(EXPECTED_QUOTA.items()))
def test_falta_de_saldo_y_clave_invalida_propias_de_cada_proveedor(
    pair: tuple[str, str], kind: str
) -> None:
    provider, case = pair
    assert case in QUOTA_CASES[provider]
    assert matrix()["results"][provider][case]["kind"] == kind


@pytest.mark.parametrize("provider", PROVIDERS)
def test_contenido_bloqueado_llega_como_content_filter(provider: str) -> None:
    assert matrix()["results"][provider]["content_filter"]["finish_reason"] == "content_filter"


def test_salida_estructurada_con_los_seis_modelos_solo_va_a_los_hosts_oficiales() -> None:
    """Hallazgo de T6: sin `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS`, la salida estructurada de
    Opus 5.5 descargaba la tabla de cabeceras beta de `raw.githubusercontent.com`."""
    structured = matrix()["structured"]
    assert structured["other_hosts"] == []
    assert set(structured["models"]) == {
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "gpt-6-luna",
        "gpt-6.1-sol",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
    }
    for result in structured["models"].values():
        assert result["tokens"] == [11, 2], result
    assert structured["requires_key"] is True


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("failing", ["close", "cache", "settings"])
def test_un_fallo_de_limpieza_no_sustituye_el_resultado(provider: str, failing: str) -> None:
    """Revisión de seguridad de T6, hallazgo 1: cada paso de la limpieza por separado."""
    cleanup = matrix()["cleanup"]
    ok = cleanup[f"{provider}:{failing}:ok"]
    assert ok["result"]["text"] == "hola"
    assert ok["steps"] == ["close", "cache", "settings"]
    failed = cleanup[f"{provider}:{failing}:server_error"]
    assert failed["result"] == {"kind": "server_error", "retry_after": None}
    assert failed["steps"] == ["close", "cache", "settings"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_cancelar_durante_close_termina_la_limpieza_y_sale_cancelada(provider: str) -> None:
    """La cancelación sale (el servicio cuenta el intento como consumido, ver
    `test_service.py`), pero antes se vacía la caché y se reaplican los ajustes."""
    cancel = matrix()["cleanup"][f"{provider}:cancel"]
    assert cancel == {"outcome": "cancelled", "steps": ["close", "cache", "settings"]}


def test_los_fallos_de_limpieza_no_dejan_la_clave_ni_clientes_en_cache() -> None:
    report = matrix()
    assert report["cleanup"]["cache_entries"] == 0
    assert "llm.cleanup_failed" in report["_output"]
    for key in KEYS.values():
        assert key not in report["_output"]


def test_herramientas_rechazadas_sin_enviar_nada() -> None:
    """Hallazgo de T6: con `tools`, LiteLLM crea su propio cliente aiohttp para `gpt-6*`."""
    structured = matrix()["structured"]
    assert structured["tools"] == {"kind": "bad_request", "retry_after": None}
    assert structured["tools_requests"] == 0


def test_sin_clave_no_se_llama() -> None:
    assert matrix()["no_key"] == {"kind": "invalid_key"}


def test_una_redireccion_no_se_sigue() -> None:
    """Condición 15: el 307 del host oficial hacia loopback no llega a la trampa."""
    assert matrix()["redirect_hits"] == 0


def test_sin_red_ni_clave_en_caches_tras_las_llamadas() -> None:
    """Criterio 4 de ADR 0015 §6 y condición 6: caché vacía y ningún diccionario con la clave."""
    report = matrix()
    assert report["attempts"] == []
    assert report["cache_entries"] == 0
    assert report["holders"] == dict.fromkeys(PROVIDERS, 0)


def test_configuracion_endurecida_aplicada() -> None:
    """Condiciones 2, 4, 5 y 20: entorno limpio, callbacks vacíos, loggers a stderr y
    contextos TLS inyectados con la subclase con cerrojo del motor."""
    state = matrix()["state"]
    assert state["unsafe_env"] == []
    assert state["cwd_in_sys_path"] is False
    assert state["callbacks"] == [[] for _ in hardening.EMPTY_LIST_SETTINGS]
    assert state["cache"] is True
    assert state["turn_off_message_logging"] is True
    assert state["ssl_context_type"] == "_LockedContext"
    assert state["loggers"]
    for _name, handlers, propagate, level in state["loggers"]:
        assert (handlers, propagate, level) == (0, True, 30)


# --- Entorno hostil, `.env` del directorio padre y `tiktoken_ext` -------------------------


def test_entorno_hostil_no_desvia_la_clave_ni_ejecuta_codigo_del_directorio(
    tmp_path: Path,
) -> None:
    """Condiciones 1, 2, 3, 4, 14, 15 y 16: con `*_BASE_URL`, proxies, `SSL_VERIFY=False`,
    `LITELLM_MODE=DEV`, `LITELLM_LOG=DEBUG` y un `.env` en el directorio padre apuntando a
    una trampa, la clave solo puede ir al host oficial; un `tiktoken_ext/*.py` del
    directorio de trabajo no se ejecuta; stdout solo lleva la línea de la sonda."""
    trap = Trap()
    try:
        trap_url = f"http://127.0.0.1:{trap.port}"
        lines = [f"{name}={trap_url}/v1" for name in REDIRECT_VARS]
        (tmp_path / ".env").write_text("\n".join([*lines, "FARO_DOTENV_PROBE=cargado"]) + "\n")
        work = tmp_path / "trabajo"
        (work / "tiktoken_ext").mkdir(parents=True)
        (work / "tiktoken_ext" / "faro_trap.py").write_text(
            "open('faro_trap_marker.txt', 'w').write('x')\n", encoding="utf-8"
        )
        keylog = work / "keylog.txt"
        env = dict.fromkeys(REDIRECT_VARS, f"{trap_url}/v1") | {
            "LITELLM_MODE": "DEV",
            "LITELLM_LOG": "DEBUG",
            "SSL_VERIFY": "False",
            "SSLKEYLOGFILE": str(keylog),
            "HTTPS_PROXY": trap_url,
            "https_proxy": trap_url,
            "ALL_PROXY": trap_url,
            "DISABLE_AIOHTTP_TRANSPORT": "True",
            "AIOHTTP_TRUST_ENV": "True",
            "CUSTOM_TIKTOKEN_CACHE_DIR": str(work),
            "OPENAI_API_KEY": "sk-" + "otra-" + "1" * 20,
        }
        report, stdout, stderr = run_probe(work, "trap", env)
        assert trap.connections == 0
    finally:
        trap.close()
    assert set(report["attempts"]) == {
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
    }
    assert report["results"] == {p: {"kind": "unreachable", "retry_after": None} for p in PROVIDERS}
    assert report["dotenv_marker"] is None
    assert report["marker_exists"] is False
    assert not (work / "faro_trap_marker.txt").exists()
    assert report["encoding_tokens"] > 0  # `cl100k_base` incluido, sin descargas
    assert report["state"]["unsafe_env"] == []
    assert report["state"]["cwd_in_sys_path"] is False
    assert not keylog.exists()
    # stdout es el canal del protocolo: ni LiteLLM ni los logs escriben ahí.
    assert stdout.splitlines() == [
        line for line in stdout.splitlines() if line.startswith("FARO_PROBE ")
    ]
    for key in KEYS.values():
        assert key not in stdout
        assert key not in stderr


# --- Condición 21: contextos TLS durante `acompletion` -------------------------------------


@pytest.mark.parametrize("provider", PROVIDERS)
def test_handshake_real_en_memoria_con_los_tres_proveedores(provider: str) -> None:
    report = tls_spy()
    assert report["results"][provider]["text"] == "hola"
    assert report["attempts"] == []


def test_solo_el_contexto_del_motor_o_la_subclase_con_cerrojo() -> None:
    report = tls_spy()
    assert report["hosts"] == [
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
    ]
    assert report["wrap_calls"] >= 3
    assert report["handshakes"] >= 3
    assert report["inner_wrap_calls"] == 3
    assert report["engine_context_used"] is True
    assert report["all_engine_or_locked"] is True
    assert report["plain_truststore"] == []


def test_ningun_contexto_de_la_cache_de_litellm_ni_wrap_socket_en_el_bucle() -> None:
    report = tls_spy()
    assert report["litellm_cache_used"] is False
    assert report["wrap_socket_in_loop"] is False


def test_los_contextos_siguen_verificando_tras_cada_llamada() -> None:
    assert tls_spy()["after_each_call"] == [[True, True, True, True]] * 3
