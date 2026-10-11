"""`LlmService.call` (spec F1b §4.1 y §9.2, skill `capa-llm` §5-§7).

Cada rechazo de los pasos 1-5 ocurre sin pedir la clave; reintentos y no reintentos;
`Retry-After`; salida inválida → un reintento → `llm.bad_output`; cancelada → máximo con
`cost_estimated`; reservas concurrentes que no pasan el tope; día local; la clave falsa no
aparece en la base, en los logs ni en el resultado.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog
from pydantic import BaseModel, ConfigDict

from faro_engine.core.errors import FaroError
from faro_engine.core.secrets import SecretError
from faro_engine.core.store.credentials import add_usage, set_daily_limit
from faro_engine.core.store.settings import PREFERRED_PROVIDER, set_setting
from faro_engine.llm.catalog import default_catalog
from faro_engine.llm.client import RawCompletion, ResolvedCall, ToolSpec
from faro_engine.llm.errors import LlmCallError
from faro_engine.llm.fake import FAKE_MODEL, FakeLLM, fake_catalog
from faro_engine.llm.pricing import max_call_cost
from faro_engine.llm.service import LazyLiteLlmClient, LlmService, _jitter, _strip_fence
from faro_engine.net.client import Deadline
from tests.fakes.llm import FAKE_KEYS, FakeFailure, FakeReply, make_request
from tests.llm.conftest import NOW, Run, Step, World, add_run, query, set_control

ANTHROPIC_REF = "/".join(("llm", "anthropic", "default"))


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str
    language: str


SUMMARY_JSON = '{"headline": "Tienda de ropa", "language": "es"}'


async def call(world: World, request: Any = None, *, run: Any = None, **over: Any) -> Any:
    service = over.pop("service", None) or world.service(**over)
    return await service.call(run or Run(), Step(), request or make_request())


async def expect_error(world: World, code: str, request: Any = None, **kwargs: Any) -> FaroError:
    with pytest.raises(FaroError) as info:
        await call(world, request, **kwargs)
    assert info.value.code == code
    return info.value


def dump_tables(world: World) -> str:
    parts: list[str] = []
    for table in ("agent_steps", "agent_runs", "credential_usage", "audit_log", "settings"):
        parts.append(repr(query(world.database, f"SELECT * FROM {table}")))  # noqa: S608
    return "\n".join(parts)


# --- Llamada correcta y registro ------------------------------------------------------


async def test_llamada_correcta_registra_paso_tarea_y_uso_del_dia(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text="ok", tokens_in=1000, tokens_out=200)}
    result = await call(world)

    assert result.text == "ok"
    assert result.provider == "anthropic"
    assert result.model == "claude-haiku-5-5"
    assert result.tier == "economy"
    assert result.attempts == 1
    assert result.usage.tokens_in == 1000
    assert result.usage.tokens_out == 200
    # Haiku 5.5: 0,10 USD/Mtok de entrada y 0,50 de salida → 100 + 100 micros.
    assert result.cost_micros == 200
    step = world.step_row()
    assert step == {
        "attempts": 1,
        "tokens_in": 1000,
        "tokens_out": 200,
        "cost_micros": 200,
        "cost_estimated": 0,
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "tier": "economy",
        "secret_ref": ANTHROPIC_REF,
        "prompt_id": "test.prompt",
        "prompt_version": 1,
    }
    assert world.run_row() == (1000, 200, 200)
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 1, 1000, 200, 200)]
    # Una sola petición de la clave y su copia sobrescrita al salir.
    assert [ref for ref, _ in world.secrets.requests] == [ANTHROPIC_REF]
    with pytest.raises(ValueError, match="borró"):
        _ = world.secrets.values[0].buffer
    assert world.llm.keys_seen == [True]


async def test_resuelve_el_modelo_del_catalogo_sin_host_ni_cabeceras(world: World) -> None:
    await call(world, make_request("write"))
    [resolved] = world.llm.calls
    assert resolved.model == "claude-opus-5-5"
    assert resolved.litellm_model == "anthropic/claude-opus-5-5"
    assert resolved.tier == "premium"
    assert resolved.messages == ({"role": "user", "content": "Hola, clasifica esto."},)
    assert not hasattr(resolved, "api_base")


@pytest.mark.parametrize(
    ("provider", "task", "model"),
    [
        ("openai", "extract", "gpt-6-luna"),
        ("openai", "plan", "gpt-6.1-sol"),
        ("gemini", "classify", "gemini-3.5-flash-lite"),
        ("gemini", "write", "gemini-3.8-flash"),
    ],
)
async def test_proveedor_de_la_tarea_y_nivel_por_tipo_de_tarea(
    world: World, provider: str, task: Any, model: str
) -> None:
    result = await call(world, make_request(task), run=Run(provider=provider))
    assert (result.provider, result.model) == (provider, model)
    assert world.secrets.requests[0][0] == f"llm/{provider}/default"


async def test_sin_proveedor_en_la_tarea_usa_la_preferencia(world: World) -> None:
    world.database.run_sync(
        lambda c: set_setting(c, PREFERRED_PROVIDER, "gemini", now="2026-10-09T15:00:00Z")
    )
    result = await call(world, run=Run(provider=None))
    assert result.provider == "gemini"


async def test_sin_proveedor_ni_preferencia_el_primero_con_clave(world: World) -> None:
    set_control(world.control, paused=False, providers=["openai", "gemini"])
    result = await call(world, run=Run(provider=None))
    assert result.provider == "openai"


async def test_con_deadline_la_clave_se_pide_con_lo_que_queda(world: World) -> None:
    service = world.service()
    await service.call(Run(), Step(), make_request(), deadline=Deadline(30, lambda: 0.0))
    assert world.secrets.requests == [(ANTHROPIC_REF, 30.0)]


# --- Rechazos 1-5 sin pedir la clave ---------------------------------------------------


async def test_pausa_rechaza_sin_pedir_la_clave(world: World) -> None:
    set_control(world.control, paused=True, providers=["anthropic"])
    await expect_error(world, "agents.paused")
    assert world.secrets.requests == []
    assert world.llm.calls == []


async def test_sin_agents_control_cuenta_como_pausa(world: World) -> None:
    from faro_engine.core.jobs.control import AgentsControlState

    await expect_error(world, "agents.paused", control=AgentsControlState())
    assert world.secrets.requests == []


async def test_proveedor_sin_clave_da_no_key_sin_pedirla(world: World) -> None:
    set_control(world.control, paused=False, providers=["openai"])
    error = await expect_error(world, "llm.no_key")
    assert error.details == {"provider": "anthropic"}
    assert world.secrets.requests == []


async def test_preferencia_sin_clave_no_cambia_de_proveedor(world: World) -> None:
    set_control(world.control, paused=False, providers=["openai"])
    world.database.run_sync(
        lambda c: set_setting(c, PREFERRED_PROVIDER, "gemini", now="2026-10-09T15:00:00Z")
    )
    error = await expect_error(world, "llm.no_key", run=Run(provider=None))
    assert error.details == {"provider": "gemini"}


async def test_sin_ninguna_clave_da_no_key_sin_proveedor(world: World) -> None:
    set_control(world.control, paused=False, providers=[])
    error = await expect_error(world, "llm.no_key", run=Run(provider=None))
    assert error.details == {}
    assert world.secrets.requests == []


async def test_presupuesto_de_costo_agotado_sin_pedir_la_clave(world: World) -> None:
    add_run(world.database, "run-pobre", "step-pobre", max_cost_micros=50)
    request = make_request()  # máximo: 1 + 50 = 51 micros
    with pytest.raises(FaroError) as info:
        await world.service().call(Run(run_id="run-pobre"), Step(step_id="step-pobre"), request)
    assert info.value.code == "agent.budget_exhausted"
    assert world.secrets.requests == []


async def test_presupuesto_de_tokens_agotado_sin_pedir_la_clave(world: World) -> None:
    add_run(world.database, "run-tokens", "step-tokens", token_budget=106)
    with pytest.raises(FaroError) as info:
        await world.service().call(
            Run(run_id="run-tokens"), Step(step_id="step-tokens"), make_request()
        )
    assert info.value.code == "agent.budget_exhausted"
    assert world.secrets.requests == []
    # Justo en el límite (7 + 100 = 107 tokens) sí cabe.
    add_run(world.database, "run-justo", "step-justo", token_budget=107)
    await world.service().call(Run(run_id="run-justo"), Step(step_id="step-justo"), make_request())


async def test_tope_diario_alcanzado_sin_pedir_la_clave(world: World) -> None:
    def spend(conn: Any) -> None:
        add_usage(
            conn,
            usage_id="u-1",
            secret_ref=ANTHROPIC_REF,
            provider="anthropic",
            usage_date="2026-10-09",
            tokens_in=0,
            tokens_out=0,
            cost_micros=4_999_960,
            now="2026-10-09T10:00:00Z",
        )

    world.database.run_sync(spend)
    error = await expect_error(world, "llm.daily_limit_reached")  # 4 999 960 + 51 > 5 000 000
    assert error.details == {"provider": "anthropic"}
    assert world.secrets.requests == []


async def test_otro_dia_no_cuenta_para_el_tope(world: World) -> None:
    def spend(conn: Any) -> None:
        add_usage(
            conn,
            usage_id="u-ayer",
            secret_ref=ANTHROPIC_REF,
            provider="anthropic",
            usage_date="2026-10-08",
            tokens_in=0,
            tokens_out=0,
            cost_micros=5_000_000,
            now="2026-10-08T10:00:00Z",
        )

    world.database.run_sync(spend)
    await call(world)


async def test_max_output_tokens_mayor_que_el_del_modelo_es_un_error_de_codigo(
    world: World,
) -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        await call(world, make_request(max_output_tokens=65_537), run=Run(provider="gemini"))


async def test_error_del_canal_de_secretos_se_propaga_y_suelta_la_reserva(world: World) -> None:
    world.secrets.error = "vault.secret_not_allowed"
    service = world.service()
    with pytest.raises(SecretError) as info:
        await service.call(Run(), Step(), make_request())
    assert info.value.code == "vault.secret_not_allowed"
    assert service.limiter.reserved(ANTHROPIC_REF) == 0
    assert world.llm.calls == []


async def test_clave_no_ascii_da_invalid_key(world: World) -> None:
    world.secrets.value = "clave-ñ".encode()
    await expect_error(world, "llm.invalid_key")
    assert world.llm.calls == []


# --- Reservas concurrentes --------------------------------------------------------------


class GatedLLM(FakeLLM):
    """No responde hasta que la prueba abre la puerta (dos llamadas a la vez)."""

    def __init__(self) -> None:
        super().__init__(needs_key=True)
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.entered = 0

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        self.entered += 1
        self.started.set()
        await self.gate.wait()
        return await FakeLLM.complete(self, call, api_key)


async def test_reservas_concurrentes_no_pasan_el_tope(world: World) -> None:
    world.database.run_sync(
        lambda c: set_daily_limit(
            c, limit_id="l-1", secret_ref=ANTHROPIC_REF, micros=500_000, now="2026-10-09T10:00:00Z"
        )
    )
    add_run(world.database, "run-2", "step-2")
    gated = GatedLLM()
    service = world.service(client=gated)
    request = make_request("write", max_output_tokens=15_000)  # Opus: 300 000 micros de máximo
    first = asyncio.create_task(service.call(Run(), Step(), request))
    second = asyncio.create_task(service.call(Run(run_id="run-2"), Step(step_id="step-2"), request))
    done, _ = await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    [rejected] = done
    with pytest.raises(FaroError) as info:
        rejected.result()
    assert info.value.code == "llm.daily_limit_reached"
    assert gated.entered == 1
    assert service.limiter.reserved(ANTHROPIC_REF) == max_call_cost(
        default_catalog().model_for("anthropic", "premium"),
        request.prompt_chars,
        15_000,
        NOW.date(),
    )
    gated.gate.set()
    accepted = first if rejected is second else second
    await accepted
    assert service.limiter.reserved(ANTHROPIC_REF) == 0
    # Solo una petición de clave: el rechazado nunca la pidió.
    assert len(world.secrets.requests) == 1


# --- Reintentos -------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["rate_limited", "server_error", "timeout", "unreachable"])
async def test_reintenta_fallos_transitorios(world: World, kind: Any) -> None:
    world.llm.responses = {"test.prompt": [FakeFailure(kind), FakeReply(text="ok")]}
    result = await call(world)
    assert result.attempts == 2
    assert world.sleeps.delays == [2.0]
    assert world.step_row()["attempts"] == 2
    # La clave se pidió una sola vez para toda la llamada lógica.
    assert len(world.secrets.requests) == 1


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("rate_limited", "llm.rate_limited"),
        ("server_error", "llm.provider_error"),
        ("timeout", "llm.timeout"),
        ("unreachable", "llm.unreachable"),
    ],
)
async def test_tras_dos_reintentos_sale_el_codigo(world: World, kind: Any, code: str) -> None:
    world.jitters[0] = 0.2
    world.llm.responses = {"test.prompt": FakeFailure(kind)}
    error = await expect_error(world, code)
    assert error.details == {"provider": "anthropic"}
    assert world.sleeps.delays == [pytest.approx(2.4), pytest.approx(7.2)]
    assert world.step_row()["attempts"] == 3
    assert len(world.llm.calls) == 3


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("invalid_key", "llm.invalid_key"),
        ("permission", "llm.invalid_key"),
        ("insufficient_quota", "llm.insufficient_quota"),
        ("content_blocked", "llm.content_blocked"),
        ("bad_request", "llm.provider_error"),
        ("redirect", "llm.provider_error"),
        ("unknown", "llm.provider_error"),
    ],
)
async def test_no_reintenta_fallos_definitivos(world: World, kind: Any, code: str) -> None:
    world.llm.responses = {"test.prompt": [FakeFailure(kind), FakeReply(text="ok")]}
    await expect_error(world, code)
    assert world.sleeps.delays == []
    step = world.step_row()
    assert (step["attempts"], step["cost_micros"], step["cost_estimated"]) == (1, 0, 0)
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 1, 0, 0, 0)]


async def test_retry_after_corto_se_respeta(world: World) -> None:
    world.llm.responses = {
        "test.prompt": [FakeFailure("rate_limited", retry_after=3.5), FakeReply(text="ok")]
    }
    await call(world)
    assert world.sleeps.delays == [3.5]


async def test_retry_after_largo_no_se_reintenta(world: World) -> None:
    world.llm.responses = {
        "test.prompt": [FakeFailure("rate_limited", retry_after=21), FakeReply(text="ok")]
    }
    await expect_error(world, "llm.rate_limited")
    assert world.sleeps.delays == []
    assert len(world.llm.calls) == 1


async def test_intento_sin_respuesta_cuenta_el_maximo_como_gastado(world: World) -> None:
    world.llm.responses = {
        "test.prompt": [FakeFailure("timeout"), FakeReply(text="ok", tokens_in=7, tokens_out=10)]
    }
    result = await call(world)
    # Máximo del intento perdido (1 + 50) + real (1 + 5).
    assert result.cost_micros == 51 + 6
    step = world.step_row()
    assert step["cost_estimated"] == 1
    assert (step["tokens_in"], step["tokens_out"]) == (7 + 7, 100 + 10)
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 2, 14, 110, 57)]


async def test_pausa_durante_la_espera_no_reintenta(world: World) -> None:
    world.llm.responses = {"test.prompt": [FakeFailure("server_error"), FakeReply(text="ok")]}

    async def pause_while_sleeping(_seconds: float) -> None:
        set_control(world.control, paused=True, providers=["anthropic"])

    await expect_error(world, "agents.paused", sleep=pause_while_sleeping)
    assert len(world.llm.calls) == 1


async def test_reintento_sin_presupuesto_tras_un_intento_perdido(world: World) -> None:
    add_run(world.database, "run-justo", "step-justo", max_cost_micros=60)
    world.llm.responses = {"test.prompt": [FakeFailure("timeout"), FakeReply(text="ok")]}
    with pytest.raises(FaroError) as info:
        await world.service().call(
            Run(run_id="run-justo"), Step(step_id="step-justo"), make_request()
        )
    assert info.value.code == "agent.budget_exhausted"  # 51 gastado + 51 > 60
    assert len(world.llm.calls) == 1


async def test_reintento_sin_tope_tras_un_intento_perdido(world: World) -> None:
    def spend(conn: Any) -> None:
        add_usage(
            conn,
            usage_id="u-1",
            secret_ref=ANTHROPIC_REF,
            provider="anthropic",
            usage_date="2026-10-09",
            tokens_in=0,
            tokens_out=0,
            cost_micros=4_999_900,
            now="2026-10-09T10:00:00Z",
        )

    world.database.run_sync(spend)
    world.llm.responses = {"test.prompt": [FakeFailure("timeout"), FakeReply(text="ok")]}
    service = world.service()
    with pytest.raises(FaroError) as info:
        await service.call(Run(), Step(), make_request())
    assert info.value.code == "llm.daily_limit_reached"  # 4 999 951 + 51 > 5 000 000
    assert service.limiter.reserved(ANTHROPIC_REF) == 0


async def test_excepcion_inesperada_del_cliente_es_provider_error(world: World) -> None:
    class Broken(FakeLLM):
        async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:  # noqa: ARG002
            raise RuntimeError("no debería pasar")

    with structlog.testing.capture_logs() as logs:
        await expect_error(world, "llm.provider_error", client=Broken(needs_key=True))
    assert any(e.get("error_type") == "RuntimeError" for e in logs)
    assert not any("no debería pasar" in repr(e) for e in logs)


# --- Salida -----------------------------------------------------------------------------


async def test_salida_estructurada_valida(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text=f"```json\n{SUMMARY_JSON}\n```")}
    result = await call(world, make_request(output_schema=Summary))
    assert result.parsed == {"headline": "Tienda de ropa", "language": "es"}
    [resolved] = world.llm.calls
    assert resolved.response_format is not None
    assert resolved.response_format["json_schema"]["name"] == "Summary"


async def test_salida_invalida_un_reintento_y_luego_vale(world: World) -> None:
    world.llm.responses = {
        "test.prompt": [FakeReply(text='{"headline": 1}'), FakeReply(text=SUMMARY_JSON)]
    }
    result = await call(world, make_request(output_schema=Summary))
    assert result.attempts == 2
    assert result.parsed is not None
    assert world.sleeps.delays == []


async def test_salida_invalida_dos_veces_da_bad_output(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text="no es JSON")}
    await expect_error(world, "llm.bad_output", make_request(output_schema=Summary))
    assert len(world.llm.calls) == 2
    assert world.step_row()["attempts"] == 2


async def test_texto_vacio_sin_esquema_es_salida_invalida(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text=None)}
    await expect_error(world, "llm.bad_output")


async def test_esquema_sin_texto_ni_herramientas_es_salida_invalida(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text=None)}
    await expect_error(world, "llm.bad_output", make_request(output_schema=Summary))


async def test_contenido_bloqueado_por_finish_reason(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text="", finish_reason="content_filter")}
    await expect_error(world, "llm.content_blocked")
    assert world.step_row()["attempts"] == 1


TOOL = ToolSpec(name="read_page", description="Lee una página.", parameters={"type": "object"})


async def test_llamada_a_una_herramienta_ofrecida(world: World) -> None:
    world.llm.responses = {
        "test.prompt": FakeReply(text=None, tool_calls=(("read_page", '{"page": 2}'),))
    }
    result = await call(world, make_request(tools=(TOOL,), output_schema=Summary))
    assert result.parsed is None
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("read_page", {"page": 2})]
    [resolved] = world.llm.calls
    assert resolved.tools[0]["function"]["name"] == "read_page"


@pytest.mark.parametrize(
    "calls", [(("otra_herramienta", "{}"),), (("read_page", "no json"),), (("read_page", "[]"),)]
)
async def test_herramienta_no_ofrecida_o_argumentos_malos_son_salida_invalida(
    world: World, calls: tuple[tuple[str, str], ...]
) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text=None, tool_calls=calls)}
    await expect_error(world, "llm.bad_output", make_request(tools=(TOOL,)))


async def test_respuesta_sin_tokens_cuenta_el_maximo(world: World) -> None:
    world.llm.responses = {"test.prompt": FakeReply(text="ok", report_usage=False)}
    result = await call(world)
    assert result.cost_micros == 51
    assert world.step_row()["cost_estimated"] == 1


# --- Cancelación ------------------------------------------------------------------------


async def test_llamada_cancelada_cuenta_el_maximo_como_estimado(world: World) -> None:
    gated = GatedLLM()
    service = world.service(client=gated)
    task = asyncio.create_task(service.call(Run(), Step(), make_request()))
    await gated.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    step = world.step_row()
    assert (step["attempts"], step["cost_micros"], step["cost_estimated"]) == (1, 51, 1)
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 1, 7, 100, 51)]
    assert service.limiter.reserved(ANTHROPIC_REF) == 0


# --- Día local --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "day"), [("UTC", "2026-10-10"), ("America/Guatemala", "2026-10-09")]
)
async def test_dia_local_de_la_computadora(world: World, zone: str, day: str) -> None:
    world.clock.now = datetime(2026, 10, 10, 5, 30, tzinfo=UTC)  # 23:30 en Guatemala
    await call(world, local_tz=ZoneInfo(zone))
    assert [row[1] for row in world.usage_rows()] == [day]


async def test_cuenta_en_el_dia_en_que_se_registra(world: World) -> None:
    guatemala = ZoneInfo("America/Guatemala")
    world.clock.now = datetime(2026, 10, 9, 23, 59, tzinfo=guatemala)

    class Slow(FakeLLM):
        async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
            world.clock.now = datetime(2026, 10, 10, 0, 1, tzinfo=guatemala)
            return await FakeLLM.complete(self, call, api_key)

    await call(world, client=Slow(needs_key=True), local_tz=guatemala)
    assert [row[1] for row in world.usage_rows()] == ["2026-10-10"]


async def test_today_sin_zona_usa_la_del_sistema(world: World) -> None:
    service = world.service(local_tz=None)
    assert service.today() == NOW.astimezone().date()


# --- Modo --fake-llm ---------------------------------------------------------------------


async def test_modo_simulado_no_pide_clave_y_marca_el_modelo_fake(world: World) -> None:
    result = await call(world, client=FakeLLM(), catalog=fake_catalog())
    assert world.secrets.requests == []
    assert result.model == FAKE_MODEL
    assert world.step_row()["model"] == FAKE_MODEL
    # Precio de prueba alto: 6 tokens x 100 USD/Mtok + 12 tokens x 500 USD/Mtok.
    assert result.cost_micros == 600 + 6000


# --- La clave no sale de la llamada ----------------------------------------------------


async def test_la_clave_no_aparece_en_la_base_ni_en_los_logs_ni_en_el_resultado(
    world: World,
) -> None:
    world.llm.responses = {"test.prompt": [FakeFailure("server_error"), FakeReply(text="ok")]}
    with structlog.testing.capture_logs() as logs:
        result = await call(world)
        await expect_error(world, "llm.invalid_key", client=_failing("invalid_key"))
    key = FAKE_KEYS["anthropic"]
    assert key not in dump_tables(world)
    assert key not in repr(logs)
    assert key not in result.model_dump_json()
    assert key not in repr(world.llm.calls)


def _failing(kind: Any) -> FakeLLM:
    return FakeLLM(responses={"test.prompt": FakeFailure(kind)}, needs_key=True)


# --- Lecturas para rutas y trabajador -----------------------------------------------------


async def test_daily_state_y_usage_overview(world: World) -> None:
    service = world.service()
    await service.call(Run(), Step(), make_request())
    spent, reserved, limit = await service.daily_state("anthropic")
    assert (reserved, limit) == (0, 5_000_000)
    assert spent > 0
    overview = await service.usage_overview()
    assert overview.usage_date == "2026-10-09"
    assert [p.provider for p in overview.providers] == ["anthropic", "openai", "gemini"]
    assert overview.providers[0].requests_today == 1
    assert overview.total_today_micros == spent


async def test_record_falla_si_el_paso_ya_no_existe(world: World) -> None:
    from faro_engine.llm.usage import MissingRecordError

    with pytest.raises(MissingRecordError):
        await world.service().call(Run(), Step(step_id="no-existe"), make_request())


async def test_record_falla_si_la_tarea_ya_no_existe(world: World) -> None:
    from faro_engine.llm.usage import MissingRecordError

    with pytest.raises(MissingRecordError):
        await world.service().call(Run(run_id="no-existe"), Step(), make_request())


# --- Adaptador perezoso -----------------------------------------------------------------


async def test_adaptador_perezoso_que_no_carga_falla_sin_enviar(world: World) -> None:
    lazy = LazyLiteLlmClient("tests.llm.no_existe_este_modulo")
    assert lazy.requires_key is True
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(LlmCallError) as info:
            await lazy.complete(world_call(), "x")
        service = world.service(client=lazy)
        await expect_error(world, "llm.provider_error", service=service)
    assert info.value.kind == "adapter_unavailable"
    assert any(e["event"] == "llm.adapter_unavailable" for e in logs)
    # Revisión de seguridad de T6, hallazgo 5: sin adaptador no se pide la clave.
    assert world.secrets.requests == []
    assert service.limiter.reserved(ANTHROPIC_REF) == 0


async def test_adaptador_perezoso_carga_una_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[int] = []

    class Client:
        requires_key = True

        def __init__(self) -> None:
            loaded.append(1)

        async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:  # noqa: ARG002
            return RawCompletion(text=api_key)

    module = types.ModuleType("tests.llm.adaptador_falso")
    module.LiteLlmClient = Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tests.llm.adaptador_falso", module)
    lazy = LazyLiteLlmClient("tests.llm.adaptador_falso")
    first = await lazy.complete(world_call(), "a")
    second = await lazy.complete(world_call(), "b")
    assert (first.text, second.text) == ("a", "b")
    assert loaded == [1]


def world_call() -> ResolvedCall:
    return LlmService._resolve(
        "anthropic", default_catalog().model_for("anthropic", "economy"), make_request()
    )


def test_strip_fence() -> None:
    assert _strip_fence("  {}  ") == "{}"
    assert _strip_fence("```json\n{}\n```") == "{}\n"
    assert _strip_fence("```{}```") == "{}"
    assert _strip_fence("```a``` y ```b```") == "```a``` y ```b```"


def test_jitter_dentro_del_veinte_por_ciento() -> None:
    values = [_jitter() for _ in range(200)]
    assert all(-0.2 <= value <= 0.2 for value in values)
    assert len(set(values)) > 1


def test_registro_sin_la_tarea_escribe_igual_el_uso_del_dia(world: World) -> None:
    """T6-C2: lo ya cobrado cuenta para el tope aunque la tarea ya no exista."""
    from faro_engine.llm.usage import AttemptRecord, MissingRecordError, record_attempt

    record = AttemptRecord(
        run_id="no-existe",
        step_id="tampoco",
        provider="anthropic",
        model="claude-haiku-5-5",
        tier="economy",
        prompt_id="test.prompt",
        prompt_version=1,
        usage_date="2026-10-09",
        tokens_in=1,
        tokens_out=1,
        cost_micros=7,
        estimated=False,
        now="2026-10-09T15:00:00Z",
        usage_id="u-1",
    )
    with pytest.raises(MissingRecordError):
        world.database.run_sync(lambda c: record_attempt(c, record))
    assert world.step_row()["attempts"] == 0
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 1, 1, 1, 7)]


async def test_borrar_la_tarea_durante_la_llamada_no_borra_el_gasto_del_dia(
    world: World,
) -> None:
    """T6-C2: la tarea (y su paso, en cascada) se borran mientras el proveedor responde."""
    from faro_engine.llm.usage import MissingRecordError

    class Deleting(FakeLLM):
        async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
            world.database.run_sync(lambda c: c.execute("DELETE FROM agent_runs"))
            return await FakeLLM.complete(self, call, api_key)

    llm = Deleting(
        needs_key=True,
        responses={"test.prompt": FakeReply(text="ok", tokens_in=1000, tokens_out=200)},
    )
    service = world.service(client=llm)
    with pytest.raises(MissingRecordError):
        await service.call(Run(), Step(), make_request())
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 1, 1000, 200, 200)]
    spent, reserved, _ = await service.daily_state("anthropic")
    assert (spent, reserved) == (200, 0)


# --- Doble cancelación (T6-C1) ------------------------------------------------------------


async def test_doble_cancelacion_no_suelta_la_reserva_antes_del_registro(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = GatedLLM()
    service = world.service(client=gated)
    record_started = asyncio.Event()
    record_gate = asyncio.Event()
    seen: list[int] = []
    original = LlmService._record

    async def slow_record(self: LlmService, call: Any, **kwargs: Any) -> None:
        record_started.set()
        await record_gate.wait()
        seen.append(self.limiter.reserved(ANTHROPIC_REF))
        await original(self, call, **kwargs)

    monkeypatch.setattr(LlmService, "_record", slow_record)
    task = asyncio.create_task(service.call(Run(), Step(), make_request()))
    await gated.started.wait()
    task.cancel()
    await record_started.wait()
    task.cancel()  # segunda cancelación mientras se guarda el registro
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()
    assert service.limiter.reserved(ANTHROPIC_REF) > 0  # la reserva sigue tomada
    record_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen
    assert seen[0] > 0
    assert world.usage_rows() == [(ANTHROPIC_REF, "2026-10-09", 1, 7, 100, 51)]
    assert world.step_row()["cost_estimated"] == 1
    assert service.limiter.reserved(ANTHROPIC_REF) == 0


async def test_finish_despite_cancel_propaga_errores_y_registros_cancelados() -> None:
    from faro_engine.llm.service import finish_despite_cancel

    async def boom() -> None:
        raise RuntimeError("registro")

    with pytest.raises(RuntimeError):
        await finish_despite_cancel(boom())

    blocker = asyncio.Event()

    async def blocked() -> None:
        await blocker.wait()

    inner = asyncio.ensure_future(blocked())
    waiter = asyncio.create_task(finish_despite_cancel(inner))
    await asyncio.sleep(0)
    inner.cancel()  # el bucle se cierra: el propio registro se cancela
    await waiter  # sale sin error
    assert inner.cancelled()


async def test_clave_con_caracteres_no_validos_en_una_cabecera_no_se_envia(
    world: World,
) -> None:
    for value in (b"sk-test con espacio", b"sk-test-abc\n", b"sk-test-\x7f", b"", b"\tsk-test"):
        world.secrets.value = value
        await expect_error(world, "llm.invalid_key")
    assert world.llm.calls == []
