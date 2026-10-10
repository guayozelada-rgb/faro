"""Catálogo `models.json`, costo en micros con enteros y elección de proveedor y nivel
(spec F1b §4.1 y §9.2: catálogo inválido → no arranca; costo con redondeo hacia arriba)."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from faro_engine.core.config import Settings
from faro_engine.llm import catalog as catalog_module
from faro_engine.llm.catalog import (
    CATALOG_PATH,
    CatalogError,
    default_catalog,
    load_catalog,
    parse_catalog,
)
from faro_engine.llm.client import LlmUsage
from faro_engine.llm.pricing import (
    call_cost,
    cost_micros,
    estimate_input_tokens,
    max_call_cost,
    prices_for,
)
from faro_engine.llm.routing import choose_provider, is_provider, secret_ref_for, tier_for

RAW: dict[str, Any] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def mutated(change: Any) -> str:
    data = copy.deepcopy(RAW)
    change(data)
    return json.dumps(data)


def model(data: dict[str, Any], provider: str, tier: str) -> dict[str, Any]:
    return next(m for m in data["models"] if (m["provider"], m["tier"]) == (provider, tier))


# --- Catálogo ----------------------------------------------------------------------------


def test_el_catalogo_incluido_es_valido_y_tiene_seis_modelos() -> None:
    catalog = default_catalog()
    assert len(catalog.models) == 6
    assert catalog.currency == "USD"
    for entry in catalog.models:
        assert entry.litellm_model.startswith(f"{entry.provider}/")
        assert entry.input_micros_per_mtok > 0
        assert entry.output_micros_per_mtok > 0
        assert entry.verified_at >= date(2026, 10, 9)
    assert {m.litellm_model.split("/")[0] for m in catalog.models} == {
        "anthropic",
        "openai",
        "gemini",
    }


def test_valores_verificados_el_2026_10_09() -> None:
    catalog = default_catalog()
    haiku = catalog.model_for("anthropic", "economy")
    assert (haiku.model, haiku.input_micros_per_mtok, haiku.output_micros_per_mtok) == (
        "claude-haiku-5-5",
        100_000,
        500_000,
    )
    assert catalog.model_for("anthropic", "premium").model == "claude-opus-5-5"
    assert catalog.model_for("openai", "economy").model == "gpt-6-luna"
    assert catalog.model_for("openai", "premium").model == "gpt-6.1-sol"
    assert catalog.model_for("gemini", "economy").model == "gemini-3.5-flash-lite"
    assert catalog.model_for("gemini", "premium").model == "gemini-3.8-flash"


def _repeat_model(data: dict[str, Any]) -> None:
    data["models"][1] = copy.deepcopy(data["models"][0])


def _drop_model(data: dict[str, Any]) -> None:
    data["models"].pop()


def _vertex(data: dict[str, Any]) -> None:
    entry = model(data, "gemini", "economy")
    entry["litellm_model"] = "vertex_ai/" + entry["model"]


def _mismatch(data: dict[str, Any]) -> None:
    model(data, "openai", "economy")["litellm_model"] = "openai/gpt-otro"


def _set(provider: str, tier: str, key: str, value: Any) -> Any:
    def change(data: dict[str, Any]) -> None:
        model(data, provider, tier)[key] = value

    return change


def _top(key: str, value: Any) -> Any:
    def change(data: dict[str, Any]) -> None:
        data[key] = value

    return change


@pytest.mark.parametrize(
    "change",
    [
        _repeat_model,
        _drop_model,
        _vertex,
        _mismatch,
        _set("anthropic", "economy", "input_micros_per_mtok", 0),
        _set("anthropic", "economy", "output_micros_per_mtok", -1),
        _set("anthropic", "economy", "input_micros_per_mtok", 1e5),
        _set("anthropic", "economy", "input_micros_per_mtok", "100000"),
        _set("anthropic", "economy", "input_micros_per_mtok", True),
        _set("anthropic", "economy", "max_output_tokens", 2_000_000),
        _set("anthropic", "economy", "verified_at", "2026-10"),
        _set("anthropic", "economy", "source", "https://blog.ejemplo.test/precios"),
        _set("anthropic", "economy", "source", "http://platform.claude.com/precios"),
        _set("anthropic", "economy", "provider", "vertex"),
        _set("anthropic", "economy", "extra", 1),
        _set(
            "anthropic",
            "economy",
            "long_context",
            {"above_input_tokens": 1, "input_micros_per_mtok": 1, "output_micros_per_mtok": 1},
        ),
        _set(
            "gemini",
            "premium",
            "price_changes",
            [
                {
                    "effective_from": "2027-01-01",
                    "input_micros_per_mtok": 1,
                    "output_micros_per_mtok": 1,
                },
                {
                    "effective_from": "2027-01-01",
                    "input_micros_per_mtok": 2,
                    "output_micros_per_mtok": 2,
                },
            ],
        ),
        _top("version", 2),
        _top("currency", "EUR"),
        _top("models", []),
    ],
)
def test_catalogo_invalido_no_arranca(change: Any) -> None:
    with pytest.raises(CatalogError):
        parse_catalog(mutated(change))


def test_claves_repetidas_se_rechazan() -> None:
    text = json.dumps(RAW).replace('"version": 1', '"version": 1, "version": 1', 1)
    with pytest.raises(CatalogError, match="repetidas"):
        parse_catalog(text)


def test_json_roto_se_rechaza() -> None:
    with pytest.raises(CatalogError):
        parse_catalog("{")


def test_archivo_ausente_se_rechaza(tmp_path: Path) -> None:
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "no-existe.json")


def test_create_app_no_arranca_con_un_catalogo_invalido(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from faro_engine.core.app import create_app

    bad = tmp_path / "models.json"
    bad.write_text(mutated(_vertex), encoding="utf-8")
    monkeypatch.setattr(catalog_module, "CATALOG_PATH", bad)
    default_catalog.cache_clear()
    monkeypatch.setattr(catalog_module.load_catalog, "__defaults__", (bad,))
    try:
        with pytest.raises(CatalogError):
            create_app(settings)
    finally:
        default_catalog.cache_clear()


def test_precio_que_cambia_en_una_fecha() -> None:
    flash = default_catalog().model_for("gemini", "premium")
    assert flash.prices_on(date(2026, 12, 31)).input_micros_per_mtok == 750_000
    assert flash.prices_on(date(2027, 1, 1)).input_micros_per_mtok == 1_500_000
    assert flash.prices_on(date(2027, 6, 1)).output_micros_per_mtok == 7_500_000


# --- Costo en micros ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "price", "expected"),
    [
        (0, 1_000_000, 0),
        (1, 1_000_000, 1),
        (1, 100_000, 1),  # 0,1 micros → 1 (hacia arriba)
        (10, 100_000, 1),
        (11, 100_000, 2),
        (1_000_000, 3_000_000, 3_000_000),
        (999_999, 1, 1),
    ],
)
def test_cost_micros_con_enteros_y_redondeo_hacia_arriba(
    tokens: int, price: int, expected: int
) -> None:
    result = cost_micros(tokens, price)
    assert result == expected
    assert isinstance(result, int)


def test_cost_micros_rechaza_negativos() -> None:
    with pytest.raises(ValueError, match="negativos"):
        cost_micros(-1, 1)


@pytest.mark.parametrize(("chars", "tokens"), [(0, 0), (1, 1), (3, 1), (4, 2), (3000, 1000)])
def test_estimado_de_entrada_caracteres_entre_tres(chars: int, tokens: int) -> None:
    assert estimate_input_tokens(chars) == tokens


def test_estimado_rechaza_negativos() -> None:
    with pytest.raises(ValueError, match="negativos"):
        estimate_input_tokens(-1)


def test_call_cost_con_los_precios_del_catalogo() -> None:
    opus = default_catalog().model_for("anthropic", "premium")
    usage = LlmUsage(tokens_in=1200, tokens_out=600)
    # 1200 x 4 USD/Mtok + 600 x 20 USD/Mtok = 4800 + 12 000 micros.
    assert call_cost(opus, usage, date(2026, 10, 9)) == 16_800


def test_contexto_largo_cobra_toda_la_peticion_con_su_precio() -> None:
    haiku = default_catalog().model_for("anthropic", "economy")
    day = date(2026, 10, 9)
    assert prices_for(haiku, day, 100_000).input_micros_per_mtok == 100_000
    assert prices_for(haiku, day, 100_001).input_micros_per_mtok == 500_000
    usage = LlmUsage(tokens_in=200_000, tokens_out=1000)
    assert call_cost(haiku, usage, day) == 100_000 + 2_500


def test_maximo_usa_el_peor_caso_para_el_umbral() -> None:
    haiku = default_catalog().model_for("anthropic", "economy")
    day = date(2026, 10, 9)
    # 150 000 caracteres → 50 000 tokens estimados, pero con el peor caso (un token por
    # carácter) se pasaría del umbral: se usan los precios de contexto largo.
    assert max_call_cost(haiku, 150_000, 800, day) == 25_000 + 2_000
    assert max_call_cost(haiku, 90_000, 800, day) == 3_000 + 400


def test_maximo_rechaza_salida_negativa() -> None:
    with pytest.raises(ValueError, match="negativo"):
        max_call_cost(default_catalog().models[0], 10, -1, date(2026, 10, 9))


# --- Proveedor y nivel -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "tier"),
    [("classify", "economy"), ("extract", "economy"), ("write", "premium"), ("plan", "premium")],
)
def test_tipo_de_tarea_a_nivel(task: Any, tier: str) -> None:
    assert tier_for(task) == tier


def test_eleccion_de_proveedor() -> None:
    assert choose_provider(None, ["gemini", "openai"]) == "openai"
    assert choose_provider(None, ["anthropic", "gemini"]) == "anthropic"
    assert choose_provider(None, []) is None
    assert choose_provider("gemini", ["anthropic"]) == "gemini"  # sin cambio automático
    assert choose_provider("vertex", ["anthropic"]) is None


def test_utilidades_de_proveedor() -> None:
    assert is_provider("openai")
    assert not is_provider("vertex")
    assert not is_provider(None)
    assert secret_ref_for("gemini") == "llm/" + "gemini/default"
