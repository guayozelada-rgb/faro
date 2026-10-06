"""LiteLLM sin red y sin la clave en sus cachés (ADR 0015 §6 criterio 4, spec F1b T2).

Comprueba el comportamiento de la versión fijada en `uv.lock`. Si una actualización de
LiteLLM cambia algo de esto, la prueba falla y hay que revisar el endurecimiento del
adaptador (skill `capa-llm`). Todo corre en un proceso aparte con la red bloqueada salvo
loopback; las claves son falsas y van a un servidor falso local. Cada proceso tarda unos
7 s (importar LiteLLM), así que los resultados se reutilizan entre pruebas.
"""

from __future__ import annotations

import functools

import pytest

from tests.deps.helpers import ProbeResult, run_probe

PROVIDERS = ("openai", "anthropic", "gemini")
KEY_HEADER = {"openai": "authorization", "anthropic": "x-api-key", "gemini": "x-goog-api-key"}


@functools.cache
def probe(*args: str, env: tuple[tuple[str, str], ...] = ()) -> ProbeResult:
    return run_probe(*args, env=dict(env))


def call(provider: str) -> ProbeResult:
    """OpenAI no carga vocabulario; Anthropic y Gemini, con el sustituto sin red."""
    if provider == "openai":
        return probe("litellm_call", provider)
    return probe("litellm_call", provider, "--stub-encoding")


def test_importar_con_mapa_de_precios_local_no_abre_conexiones() -> None:
    report = probe("import_litellm").report
    assert report["attempts"] == []
    assert report["model_cost_entries"] > 0


@pytest.mark.parametrize("provider", PROVIDERS)
def test_llamada_y_vaciado_de_cache_sin_red(provider: str) -> None:
    report = call(provider).report
    assert report["attempts"] == []
    assert report["text"] == "hola"
    assert report["tokens"] == [3, 1]
    # La clave viaja en una cabecera, nunca en la URL (Gemini: AI Studio, no `?key=`).
    assert report["key_sent_in"] == [KEY_HEADER[provider]]
    assert report["key_in_url"] is False
    # Tras cerrar los clientes y vaciar la caché no queda nada que la retenga.
    assert report["cache_entries_after_flush"] == 0
    assert report["heap_dicts_with_key_after_flush"] == 0


def test_openai_guarda_la_clave_en_su_cache_de_clientes() -> None:
    """Motivo del vaciado tras cada llamada: la clave de caché y el cliente la llevan en claro."""
    report = call("openai").report
    assert report["key_in_cache_keys"] is True
    assert report["key_in_cached_clients"] == ["AsyncOpenAI"]


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_anthropic_y_gemini_no_guardan_la_clave_en_cache(provider: str) -> None:
    report = call(provider).report
    assert report["key_in_cache_keys"] is False
    assert report["key_in_cached_clients"] == []


def test_sin_vocabulario_local_gemini_lo_descarga() -> None:
    """LiteLLM 1.104 no trae `cl100k_base` (Anthropic y Gemini lo cargan): T6 lo incluye."""
    report = probe("litellm_call", "gemini").report
    assert report["attempts"] == ["openaipublic.blob.core.windows.net"]
    assert report["error"] == "APIConnectionError"


def test_litellm_escribe_en_stdout_si_no_se_redirigen_sus_loggers() -> None:
    """Motivo de quitar los handlers de LiteLLM: stdout es el canal del protocolo."""
    assert "LiteLLM completion()" in call("openai").stdout


def test_truststore_inyectado_antes_de_importar_llega_a_litellm() -> None:
    report = probe("import_litellm").report
    assert report["type"].startswith("truststore.")


def test_truststore_inyectado_despues_de_importar_no_llega_a_litellm() -> None:
    """LiteLLM crea y guarda un contexto TLS con certifi al importarse: el orden importa."""
    report = probe("ssl_late_injection").report
    assert report["type"] == "ssl.SSLContext"


def test_ssl_verify_del_entorno_desactiva_tls_en_litellm() -> None:
    """Motivo de quitar `SSL_VERIFY` del entorno del motor antes de importar LiteLLM."""
    report = probe("ssl_late_injection", env=(("SSL_VERIFY", "False"),)).report
    assert report["value"] == "False"
    assert report["attempts"] == []
