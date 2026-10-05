"""LangGraph, APScheduler, `truststore` y `sqlite-vec` sin red (spec F1b T2, ADR 0015 §6)."""

from __future__ import annotations

from tests.deps.helpers import run_probe

# Clave falsa partida para que no parezca un secreto (no sale del proceso: red bloqueada).
LANGSMITH_ON = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "lsv2_" + "0" * 20}


def test_importar_dependencias_f1b_no_abre_conexiones() -> None:
    result = run_probe("import_f1b")
    assert result.report["attempts"] == []
    assert len(result.report["modules"]) == 6


def test_grafo_con_interrupcion_y_reanudacion_sin_red() -> None:
    result = run_probe("graph_default")
    assert result.report == {"interrupted": True, "resumed": True, "attempts": []}


def test_langsmith_desactivado_aunque_el_entorno_lo_active() -> None:
    """`langsmith.configure(enabled=False)` gana a `LANGSMITH_TRACING=true` del entorno."""
    result = run_probe("graph_tracing_off", env=LANGSMITH_ON)
    assert result.report == {"interrupted": True, "resumed": True, "attempts": []}


def test_sin_desactivar_langsmith_el_entorno_envia_trazas() -> None:
    """Motivo de la regla anterior: langchain-core enviaría el estado del grafo a LangSmith."""
    result = run_probe("graph_default", env=LANGSMITH_ON)
    assert result.report["resumed"] is True
    assert result.report["attempts"] == ["api.smith.langchain.com"]
