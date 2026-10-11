"""LangSmith siempre apagado antes de construir un grafo (condición 7 del informe de T2)."""

from __future__ import annotations

import os

import langsmith
import pytest

from faro_engine.agents.framework import tracing


def test_quita_las_variables_y_apaga_langsmith(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("langsmith_endpoint", "https://trampa.test")
    calls: list[bool] = []
    monkeypatch.setattr(langsmith, "configure", lambda *, enabled: calls.append(enabled))
    assert len(tracing.tracing_variables()) == 4
    tracing.disable_langsmith()
    assert tracing.tracing_variables() == []
    assert not any(name.upper().startswith(("LANGSMITH_", "LANGCHAIN_")) for name in os.environ)
    assert calls == [False]
