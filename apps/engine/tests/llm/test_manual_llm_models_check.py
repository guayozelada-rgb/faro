"""`scripts/manual_llm_models_check.py` (solo lo ejecuta el usuario con su clave real).

Con un cliente falso: no pide nada si no hay clave, muestra el costo máximo antes de pedir
la clave, llama a los modelos del catálogo y nunca imprime la clave ni el texto de la
respuesta.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest
import structlog

from faro_engine.core.logging import configure_logging
from faro_engine.llm.client import RawCompletion, ResolvedCall
from faro_engine.llm.errors import LlmCallError
from tests.conftest import ENGINE_DIR
from tests.fakes.llm import FAKE_KEYS

SCRIPT = ENGINE_DIR / "scripts" / "manual_llm_models_check.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("manual_llm_models_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load()


def run(argv: list[str], **kwargs: object) -> int:
    """`main` sin tocar la configuración de logs del proceso de pruebas."""
    kwargs.setdefault("setup_logging", lambda: None)
    return int(script.main(argv, **kwargs))


class Client:
    def __init__(self, *, fail: str | None = None, text: str = '{"ok": true, "idioma": "es"}'):
        self.calls: list[ResolvedCall] = []
        self.fail = fail
        self.text = text

    @property
    def requires_key(self) -> bool:
        return True

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        assert api_key
        self.calls.append(call)
        if self.fail:
            raise LlmCallError(self.fail)  # type: ignore[arg-type]
        return RawCompletion(text=self.text, finish_reason="stop", tokens_in=40, tokens_out=12)


def test_sin_clave_no_llama(capsys: pytest.CaptureFixture[str]) -> None:
    client = Client()
    code = run(["anthropic"], read_key=lambda _p: " ", client_factory=lambda: client)
    assert code == 2
    assert client.calls == []
    assert "costo máximo" in capsys.readouterr().out


def test_llama_a_los_dos_modelos_con_salida_estructurada(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = Client()
    key = FAKE_KEYS["anthropic"]
    assert run(["anthropic"], read_key=lambda _p: key, client_factory=lambda: client) == 0
    assert [c.model for c in client.calls] == ["claude-haiku-5-5", "claude-opus-5-5"]
    assert all(c.response_format is not None for c in client.calls)
    assert all(c.max_output_tokens == script.MAX_OUTPUT_TOKENS for c in client.calls)
    out = capsys.readouterr().out
    assert "JSON válido: sí" in out
    assert key not in out
    assert '"idioma"' not in out  # nunca el texto de la respuesta


def test_un_solo_nivel_y_fallos(capsys: pytest.CaptureFixture[str]) -> None:
    client = Client(fail="insufficient_quota")
    key = FAKE_KEYS["openai"]
    args = ["openai", "--tier", "premium"]
    assert run(args, read_key=lambda _p: key, client_factory=lambda: client) == 0
    assert [c.model for c in client.calls] == ["gpt-6.1-sol"]
    assert "FALLO: insufficient_quota" in capsys.readouterr().out


def test_json_invalido_y_sin_tokens(capsys: pytest.CaptureFixture[str]) -> None:
    class NoUsage(Client):
        async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
            await super().complete(call, api_key)
            return RawCompletion(text=None)

    client = NoUsage()
    key = FAKE_KEYS["gemini"]
    assert run(["gemini"], read_key=lambda _p: key, client_factory=lambda: client) == 0
    out = capsys.readouterr().out
    assert "JSON válido: no" in out
    assert "costo ?" in out
    assert not script._json_ok("no json")
    assert script._json_ok('```json\n{"ok": true, "idioma": "es"}\n```')


def test_configura_los_logs_antes_de_cargar_el_adaptador() -> None:
    order: list[str] = []
    client = Client()

    def factory() -> Client:
        order.append("client")
        return client

    run(
        ["anthropic"],
        read_key=lambda _p: "",
        client_factory=factory,
        setup_logging=lambda: order.append("logs"),
    )
    assert order == ["logs", "client"]


def test_quiet_logging_no_imprime_nada(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        script.quiet_logging()
        structlog.get_logger("prueba").critical("llm.prueba", api_key=FAKE_KEYS["openai"])
        logging.getLogger("LiteLLM").critical("texto del proveedor")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
    finally:
        configure_logging()


def test_el_script_esta_fuera_del_paquete_y_de_la_ci() -> None:
    assert SCRIPT.parent.name == "scripts"
    assert Path(str(script.__file__)).resolve() == SCRIPT.resolve()
