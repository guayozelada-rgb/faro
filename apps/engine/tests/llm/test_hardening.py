"""Endurecimiento de LiteLLM sin importarlo (informe de T2 §12, condiciones 1-5, 14 y 3).

Las mismas funciones las aplica `litellm_client.py` alrededor de su `import litellm`; con
LiteLLM real se comprueban en `test_litellm_adapter.py` (proceso aparte).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from faro_engine.llm import hardening

DANGEROUS = [
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_BASE",
    "ANTHROPIC_BASE_URL",
    "GEMINI_API_BASE",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "LITELLM_LOG",
    "LITELLM_MODE",
    "SSL_VERIFY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SSL_CERTIFICATE",
    "SSL_SECURITY_LEVEL",
    "SSL_ECDH_CURVE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSLKEYLOGFILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "DISABLE_AIOHTTP_TRANSPORT",
    "AIOHTTP_TRUST_ENV",
    "aiohttp_trust_env",
    "CUSTOM_TIKTOKEN_CACHE_DIR",
    "LANGSMITH_TRACING",
    "LANGCHAIN_API_KEY",
]


def test_limpieza_quita_todo_y_fija_lo_obligatorio() -> None:
    env = dict.fromkeys(DANGEROUS, "x") | {"PATH": "/bin", "FARO_OTRA": "se queda"}
    assert set(DANGEROUS) <= set(hardening.unsafe_variables(env))
    hardening.clean_environment(env)
    assert hardening.unsafe_variables(env) == []
    assert env["LITELLM_MODE"] == "PRODUCTION"
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert env["CUSTOM_TIKTOKEN_CACHE_DIR"] == str(hardening.TIKTOKEN_DIR)
    assert env["FARO_OTRA"] == "se queda"
    hardening.check_environment(env)


def test_comprobacion_falla_con_nombres_y_sin_valores() -> None:
    env = hardening.required_env() | {"OPENAI_BASE_URL": "http://127.0.0.1:1/valor-secreto"}
    with pytest.raises(hardening.UnsafeEnvironmentError) as info:
        hardening.check_environment(env)
    assert "OPENAI_BASE_URL" in str(info.value)
    assert "valor-secreto" not in str(info.value)


def test_obligatoria_con_otro_valor_es_insegura() -> None:
    env = hardening.required_env() | {"LITELLM_MODE": "DEV"}
    assert hardening.unsafe_variables(env) == ["LITELLM_MODE"]


def test_limpieza_del_entorno_del_proceso(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in hardening.unsafe_variables():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1")
    for name, value in hardening.required_env().items():
        monkeypatch.setenv(name, value)
    with pytest.raises(hardening.UnsafeEnvironmentError):
        hardening.check_environment()
    assert "OPENAI_BASE_URL" in hardening.unsafe_variables()
    monkeypatch.delenv("OPENAI_BASE_URL")
    hardening.check_environment()


def test_el_directorio_de_trabajo_sale_de_sys_path(tmp_path: Path) -> None:
    work = tmp_path / "trabajo"
    work.mkdir()
    path = ["", str(work), str(tmp_path), str(work / ".")]
    hardening.drop_cwd_from_sys_path(path, work)
    assert path == [str(tmp_path)]


def test_drop_cwd_por_defecto_usa_sys_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", ["", str(tmp_path), "/otra"])
    hardening.drop_cwd_from_sys_path()
    assert sys.path == ["/otra"]


def test_vocabulario_incluido_y_comprobado(tmp_path: Path) -> None:
    path = hardening.verify_tiktoken_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hardening.CL100K_SHA256
    with pytest.raises(hardening.HardeningError, match="falta"):
        hardening.verify_tiktoken_file(tmp_path)
    (tmp_path / hardening.CL100K_CACHE_NAME).write_bytes(b"otro")
    with pytest.raises(hardening.HardeningError, match="esperado"):
        hardening.verify_tiktoken_file(tmp_path)


def test_ajustes_de_litellm_se_aplican_y_comprueban() -> None:
    module = SimpleNamespace(success_callback=["langsmith"], cache=object(), ssl_verify=False)
    hardening.apply_litellm_settings(module)
    hardening.check_litellm_settings(module)
    assert module.success_callback == []
    assert module.cache is None
    assert module.turn_off_message_logging is True
    module.callbacks = ["otro"]
    with pytest.raises(hardening.HardeningError, match="callbacks"):
        hardening.check_litellm_settings(module)
    hardening.apply_litellm_settings(module)
    module.redact_messages_in_exceptions = False
    with pytest.raises(hardening.HardeningError, match="redact"):
        hardening.check_litellm_settings(module)


def test_loggers_de_litellm_sin_handlers_propios() -> None:
    logger = logging.getLogger("LiteLLM Prueba")
    logger.addHandler(logging.StreamHandler())
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(hardening.HardeningError):
            hardening.check_litellm_loggers()
        hardening.quiet_litellm_loggers()
        hardening.check_litellm_loggers()
        assert logger.handlers == []
        assert logger.propagate is True
        assert logger.level == logging.WARNING
    finally:
        logger.handlers.clear()


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_api_base_fijo_al_host_oficial(provider: str) -> None:
    url = hardening.official_api_base(provider)  # type: ignore[arg-type]
    assert url.startswith(f"https://{hardening.OFFICIAL_HOSTS[provider]}")  # type: ignore[index]


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",
        "https://api.openai.com.evil.test/v1",
        "https://api.openai.com:8443/v1",
        "https://127.0.0.1/v1",
    ],
)
def test_api_base_no_oficial_se_rechaza(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setitem(hardening.API_BASE, "openai", url)
    with pytest.raises(hardening.HardeningError):
        hardening.official_api_base("openai")
