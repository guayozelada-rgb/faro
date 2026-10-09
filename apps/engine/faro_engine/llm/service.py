"""`LlmService.call`: la única puerta para llamar a un modelo (spec F1b §4.1).

Orden de comprobaciones (skill `capa-llm` §5). Los rechazos 1–5 ocurren **sin pedir la
clave** (hay una prueba de cada uno):

1. **Pausa**: sin un `agents_control` válido o con los agentes pausados → `agents.paused`.
2. **Clave disponible**: el proveedor de la tarea (o, si no tiene, la preferencia del
   usuario o el primero con clave) está en `llm_providers` del último `agents_control`
   → si no, `llm.no_key`. Sin cambio automático a otro proveedor.
3. **Máximo de la llamada** = `ceil(caracteres / 3)` × precio de entrada +
   `max_output_tokens` × precio de salida (`pricing.max_call_cost`).
4. **Presupuesto de la tarea** (leído de la base): `cost_micros + máximo ≤ max_cost_micros`
   y `tokens + tokens máximos ≤ token_budget` → si no, `agent.budget_exhausted`.
5. **Tope diario de la clave**: `gastado hoy + reservado + máximo ≤ límite` → si no,
   `llm.daily_limit_reached`. Si cabe, se **reserva** el máximo (`limits.py`).
6. **Clave** por `secret_request` dentro de la concesión de la ejecución (ADR 0014): una
   sola petición por llamada lógica; la copia `str` que exige LiteLLM se suelta en el
   `finally` y `SecretValue` se sobrescribe al salir del `with`. Con `FakeLLM`
   (`--fake-llm`) no se pide ninguna clave.
7. **Intentos y reintentos de Faro** (`num_retries=0` en LiteLLM): hasta 2 reintentos
   ante 429, 5xx, tiempo agotado o red; espera de 2 s y 6 s (±20 %) o el `Retry-After` si es
   ≤ 20 s (si es mayor, no se reintenta). Antes de cada reintento se vuelven a comprobar la
   pausa, el presupuesto y el tope (la reserva sigue tomada). La clave no se vuelve a pedir.
8. **Registro** de cada intento en una transacción (`usage.record_attempt`): paso
   (`attempts`, tokens, costo, `provider`, `model`, `tier`, `secret_ref`, prompt),
   acumulados de la tarea y `credential_usage` del **día local en que se registra**. El
   costo real = tokens informados × precios del catálogo. Al final se suelta la reserva.
   - Un intento sin respuesta que el proveedor pudo procesar (tiempo agotado, red) cuenta
     su **máximo** como gastado (`cost_estimated = 1`), conservador.
   - Una respuesta sin `usage` también cuenta el máximo con `cost_estimated = 1`.
9. **Cancelada** (cierre del motor, `CancelledError`) durante la llamada: el paso queda
   `cost_estimated = 1` con el **máximo** como gastado antes de soltar la reserva.
10. **Salida estructurada inválida**: un reintento con el mismo prompt (cuenta en el
    presupuesto y el tope); si vuelve a fallar, `llm.bad_output`.

Quien llama dice solo `task_kind`; no elige modelo, no reintenta y no registra tokens.
Nunca se registran prompts, respuestas ni la clave: solo identificadores, códigos, tokens
y costos.
"""

from __future__ import annotations

import asyncio
import importlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from typing import Any, Final, Protocol

import anyio.to_thread
import structlog
from pydantic import ValidationError

from faro_engine.core.db.database import Database
from faro_engine.core.errors import (
    AGENT_BUDGET_EXHAUSTED,
    AGENTS_PAUSED,
    LLM_BAD_OUTPUT,
    LLM_CONTENT_BLOCKED,
    LLM_DAILY_LIMIT_REACHED,
    LLM_INVALID_KEY,
    LLM_NO_KEY,
    FaroError,
)
from faro_engine.core.ids import new_id
from faro_engine.core.jobs.control import AgentsControlState, ControlSnapshot
from faro_engine.core.secrets import SecretValue
from faro_engine.core.store.common import format_utc
from faro_engine.llm import usage
from faro_engine.llm.catalog import Catalog, CatalogModel
from faro_engine.llm.client import (
    LlmClient,
    LlmRequest,
    LlmResult,
    LlmUsage,
    Provider,
    RawCompletion,
    ResolvedCall,
    Tier,
    ToolCall,
    parse_tool_calls,
    response_format_for,
    tool_param,
)
from faro_engine.llm.errors import LlmCallError, classify_exception, failure_error, llm_error
from faro_engine.llm.limits import DailyLimiter, DailyLimitReachedError, Reservation
from faro_engine.llm.pricing import call_cost, estimate_input_tokens, max_call_cost
from faro_engine.llm.routing import choose_provider, secret_ref_for, tier_for
from faro_engine.net.client import Deadline

log = structlog.get_logger(__name__)

MAX_ATTEMPTS: Final = 3  # 1 intento + 2 reintentos
RETRY_DELAYS_SECONDS: Final = (2.0, 6.0)
RETRY_JITTER: Final = 0.2
MAX_RETRY_AFTER_SECONDS: Final = 20.0
BAD_OUTPUT_RETRIES: Final = 1
CONTENT_FILTER_REASONS: Final = frozenset({"content_filter", "safety", "refusal"})
ADAPTER_MODULE: Final = "faro_engine.llm.litellm_client"

Sleep = Callable[[float], Awaitable[None]]


class RunLike(Protocol):
    """La tarea en curso (el `RunContext` de los agentes, skill `agentes-langgraph`)."""

    @property
    def run_id(self) -> str: ...

    @property
    def provider(self) -> str | None: ...


class StepLike(Protocol):
    """El paso de `agent_steps` que creó `StepRecorder` para este nodo."""

    @property
    def step_id(self) -> str: ...


class SecretSource(Protocol):
    """El canal de secretos (`core/secrets.SecretBroker`)."""

    async def get(self, ref: str, *, max_wait: float | None = None) -> SecretValue: ...


def _jitter() -> float:
    return random.uniform(-RETRY_JITTER, RETRY_JITTER)  # noqa: S311 - espera, no criptografía


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LazyLiteLlmClient:
    """Carga `litellm_client` en la primera llamada, en un hilo (importar tarda ~6,5 s).

    Nunca antes de `ready`. Si la configuración endurecida no se puede aplicar, el módulo no
    se carga y la llamada falla con `llm.provider_error` sin haber enviado nada; la
    siguiente llamada lo vuelve a intentar.
    """

    def __init__(self, module: str = ADAPTER_MODULE) -> None:
        self._module = module
        self._client: LlmClient | None = None
        self._lock = asyncio.Lock()

    @property
    def requires_key(self) -> bool:
        return True

    async def _load(self) -> LlmClient:
        async with self._lock:
            if self._client is None:
                try:
                    module: Any = await anyio.to_thread.run_sync(
                        importlib.import_module, self._module
                    )
                except Exception as exc:  # noqa: BLE001 - el adaptador no se carga
                    log.error("llm.adapter_unavailable", error_type=type(exc).__name__)
                    raise LlmCallError("adapter_unavailable") from None
                self._client = module.LiteLlmClient()
            return self._client

    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion:
        client = await self._load()
        return await client.complete(call, api_key)


@dataclass(slots=True)
class _Call:
    """Estado de una llamada lógica mientras dura."""

    run_id: str
    step_id: str
    provider: Provider
    model: CatalogModel
    request: LlmRequest
    resolved: ResolvedCall
    reservation: Reservation
    max_cost: int
    max_input_tokens: int
    attempts: int = 0
    cost: int = 0
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def max_tokens(self) -> int:
        return self.max_input_tokens + self.request.max_output_tokens

    @property
    def tier(self) -> Tier:
        return self.model.tier


@dataclass(slots=True)
class LlmService:
    """Capa de IA del motor. Una instancia por motor (`app.state.llm`)."""

    database: Database
    control: AgentsControlState
    secrets: SecretSource
    client: LlmClient
    catalog: Catalog
    limiter: DailyLimiter = field(default_factory=DailyLimiter)
    clock: Callable[[], datetime] = _utc_now
    local_tz: tzinfo | None = None  # `None` = zona del sistema (día local del usuario)
    sleep: Sleep = asyncio.sleep
    jitter: Callable[[], float] = _jitter
    ids: Callable[[], str] = new_id

    # --- Fechas -----------------------------------------------------------------------

    def today(self) -> date:
        """Día local de la computadora (empieza a medianoche), con el reloj inyectado."""
        now = self.clock()
        local = now.astimezone(self.local_tz) if self.local_tz else now.astimezone()
        return local.date()

    # --- Lecturas para rutas y trabajador ------------------------------------------------

    async def daily_state(self, provider: Provider) -> tuple[int, int, int]:
        """(gastado hoy, reservado ahora, tope) de la clave del proveedor."""
        day = self.today().isoformat()
        spent, limit = await self.database.run(lambda c: usage.daily_state(c, provider, day))
        return spent, self.limiter.reserved(secret_ref_for(provider)), limit

    async def usage_overview(self) -> usage.UsageOverview:
        day = self.today().isoformat()
        with_key = self.control.snapshot().llm_providers
        return await self.database.run(lambda c: usage.usage_overview(c, day, with_key))

    # --- La llamada ---------------------------------------------------------------------

    async def call(
        self,
        run: RunLike,
        step: StepLike,
        request: LlmRequest,
        *,
        deadline: Deadline | None = None,
    ) -> LlmResult:
        # 1. Pausa.
        snapshot = self.control.snapshot()
        if not snapshot.can_run:
            raise FaroError.of(AGENTS_PAUSED, 409)
        # 2. Proveedor con clave.
        provider = await self._provider(run, snapshot)
        # 3. Máximo de la llamada.
        tier = tier_for(request.task_kind)
        model = self.catalog.model_for(provider, tier)
        if request.max_output_tokens > model.max_output_tokens:
            raise ValueError("max_output_tokens supera el del modelo del catálogo")
        max_input = estimate_input_tokens(request.prompt_chars)
        max_cost = max_call_cost(
            model, request.prompt_chars, request.max_output_tokens, self.today()
        )
        # 4. Presupuesto de la tarea.
        await self._check_budget(run.run_id, provider, max_cost, max_input + request.max_output_tokens)
        # 5. Tope diario y reserva.
        reservation = await self._reserve(provider, max_cost)
        try:
            call = _Call(
                run_id=run.run_id,
                step_id=step.step_id,
                provider=provider,
                model=model,
                request=request,
                resolved=self._resolve(provider, model, request),
                reservation=reservation,
                max_cost=max_cost,
                max_input_tokens=max_input,
            )
            # 6. Clave (una petición por llamada lógica) y 7–10.
            if not self.client.requires_key:
                return await self._run(call, None)
            max_wait = deadline.remaining() if deadline is not None else None
            with await self.secrets.get(secret_ref_for(provider), max_wait=max_wait) as secret:
                api_key = self._decode(secret, provider)
                try:
                    return await self._run(call, api_key)
                finally:
                    del api_key
        finally:
            reservation.release()

    # --- Comprobaciones -----------------------------------------------------------------

    async def _provider(self, run: RunLike, snapshot: ControlSnapshot) -> Provider:
        preferred = run.provider
        if preferred is None:
            preferred = await self.database.run(usage.preferred_provider)
        provider = choose_provider(preferred, snapshot.llm_providers)
        if provider is None or provider not in snapshot.llm_providers:
            raise llm_error(LLM_NO_KEY, provider)
        return provider

    async def _check_budget(self, run_id: str, provider: Provider, cost: int, tokens: int) -> None:
        budget = await self.database.run(lambda c: usage.run_budget(c, run_id))
        if (
            budget.cost_micros + cost > budget.max_cost_micros
            or budget.tokens + tokens > budget.token_budget
        ):
            raise llm_error(AGENT_BUDGET_EXHAUSTED, provider)

    def _reader(self, provider: Provider) -> Callable[[], Awaitable[tuple[int, int]]]:
        async def read() -> tuple[int, int]:
            day = self.today().isoformat()
            return await self.database.run(lambda c: usage.daily_state(c, provider, day))

        return read

    async def _reserve(self, provider: Provider, amount: int) -> Reservation:
        try:
            return await self.limiter.reserve(
                secret_ref_for(provider), amount, self._reader(provider)
            )
        except DailyLimitReachedError:
            raise llm_error(LLM_DAILY_LIMIT_REACHED, provider) from None

    async def _before_retry(self, call: _Call) -> None:
        """Antes de repetir: pausa, presupuesto y tope (la reserva sigue tomada)."""
        if not self.control.snapshot().can_run:
            raise FaroError.of(AGENTS_PAUSED, 409)
        await self._check_budget(call.run_id, call.provider, call.max_cost, call.max_tokens)
        try:
            await call.reservation.recheck(self._reader(call.provider))
        except DailyLimitReachedError:
            raise llm_error(LLM_DAILY_LIMIT_REACHED, call.provider) from None

    @staticmethod
    def _decode(secret: SecretValue, provider: Provider) -> str:
        try:
            return secret.buffer.decode("ascii")
        except UnicodeDecodeError:
            raise llm_error(LLM_INVALID_KEY, provider) from None

    @staticmethod
    def _resolve(provider: Provider, model: CatalogModel, request: LlmRequest) -> ResolvedCall:
        return ResolvedCall(
            provider=provider,
            tier=model.tier,
            model=model.model,
            litellm_model=model.litellm_model,
            prompt_id=request.prompt_id,
            messages=tuple(
                {"role": message.role, "content": message.content} for message in request.messages
            ),
            max_output_tokens=request.max_output_tokens,
            response_format=(
                response_format_for(request.output_schema) if request.output_schema else None
            ),
            tools=tuple(tool_param(tool) for tool in request.tools),
        )

    # --- Intentos -----------------------------------------------------------------------

    async def _run(self, call: _Call, api_key: str | None) -> LlmResult:
        bad_output_left = BAD_OUTPUT_RETRIES
        while True:
            raw = await self._attempts(call, api_key)
            if raw.finish_reason in CONTENT_FILTER_REASONS:
                self._log_failure(call, LLM_CONTENT_BLOCKED)
                raise llm_error(LLM_CONTENT_BLOCKED, call.provider)
            parsed = self._parse(raw, call.request)
            if parsed is not None:
                result = self._result(call, raw, *parsed)
                log.info(
                    "llm.call_finished",
                    run_id=call.run_id,
                    provider=call.provider,
                    model=call.model.model,
                    tier=call.tier,
                    task_kind=call.request.task_kind,
                    prompt_id=call.request.prompt_id,
                    prompt_version=call.request.prompt_version,
                    attempts=call.attempts,
                    tokens_in=call.tokens_in,
                    tokens_out=call.tokens_out,
                    cost_micros=call.cost,
                )
                return result
            if bad_output_left == 0:
                self._log_failure(call, LLM_BAD_OUTPUT)
                raise llm_error(LLM_BAD_OUTPUT, call.provider)
            # El reintento pasa por `_before_retry` en `_attempts` (ya hubo intentos).
            bad_output_left -= 1

    async def _attempts(self, call: _Call, api_key: str | None) -> RawCompletion:
        attempt = 0
        while True:
            attempt += 1
            if call.attempts > 0:
                await self._before_retry(call)
            try:
                raw = await self.client.complete(call.resolved, api_key)
            except asyncio.CancelledError:
                # 9. El proveedor pudo procesarla: se cuenta el máximo antes de soltar la
                # reserva, aunque vuelvan a cancelar mientras se escribe.
                await asyncio.shield(self._record(call, consumed=True))
                raise
            except Exception as exc:  # noqa: BLE001 - todo fallo pasa a `LlmCallError`
                err = classify_exception(exc)
                if not isinstance(exc, LlmCallError):
                    log.warning("llm.unexpected_error", error_type=type(exc).__name__)
                await self._record(call, consumed=err.may_have_consumed)
                delay = self._retry_delay(err, attempt)
                if delay is None:
                    error = failure_error(err, call.provider)
                    self._log_failure(call, error.code)
                    raise error from None
                await self.sleep(delay)
                continue
            await self._record(call, raw=raw)
            return raw

    def _retry_delay(self, err: LlmCallError, attempt: int) -> float | None:
        if not err.retryable or attempt >= MAX_ATTEMPTS:
            return None
        if err.retry_after is not None:
            return err.retry_after if err.retry_after <= MAX_RETRY_AFTER_SECONDS else None
        base = RETRY_DELAYS_SECONDS[attempt - 1]
        return max(0.0, base * (1 + self.jitter()))

    async def _record(
        self, call: _Call, *, raw: RawCompletion | None = None, consumed: bool = False
    ) -> None:
        """Registra un intento. Con respuesta: tokens informados (o el máximo si faltan);
        sin respuesta: el máximo si el proveedor pudo procesarla, si no cero."""
        now = self.clock()
        day = self.today()
        if raw is not None and raw.tokens_in is not None and raw.tokens_out is not None:
            spent = LlmUsage(tokens_in=raw.tokens_in, tokens_out=raw.tokens_out)
            cost = call_cost(call.model, spent, day)
            estimated = False
            call.tokens_in += spent.tokens_in
            call.tokens_out += spent.tokens_out
        elif raw is not None or consumed:
            spent = LlmUsage(
                tokens_in=call.max_input_tokens, tokens_out=call.request.max_output_tokens
            )
            cost = call.max_cost
            estimated = True
        else:
            spent = LlmUsage(tokens_in=0, tokens_out=0)
            cost = 0
            estimated = False
        call.attempts += 1
        call.cost += cost
        record = usage.AttemptRecord(
            run_id=call.run_id,
            step_id=call.step_id,
            provider=call.provider,
            model=call.model.model,
            tier=call.tier,
            prompt_id=call.request.prompt_id,
            prompt_version=call.request.prompt_version,
            usage_date=day.isoformat(),
            tokens_in=spent.tokens_in,
            tokens_out=spent.tokens_out,
            cost_micros=cost,
            estimated=estimated,
            now=format_utc(now),
            usage_id=self.ids(),
        )
        await self.database.run(lambda c: usage.record_attempt(c, record))

    # --- Salida -------------------------------------------------------------------------

    @staticmethod
    def _parse(
        raw: RawCompletion, request: LlmRequest
    ) -> tuple[dict[str, Any] | None, tuple[ToolCall, ...]] | None:
        """(salida estructurada como dict JSON, llamadas a herramientas) o `None` si la
        respuesta no sirve (JSON inválido, esquema incumplido, herramienta no ofrecida)."""
        tool_calls = parse_tool_calls(raw)
        if tool_calls is None:
            return None
        offered = {tool.name for tool in request.tools}
        if any(call.name not in offered for call in tool_calls):
            return None
        schema = request.output_schema
        if schema is None:
            if raw.text is None and not tool_calls:
                return None
            return None, tool_calls
        if raw.text is None:
            return (None, tool_calls) if tool_calls else None
        try:
            model = schema.model_validate_json(_strip_fence(raw.text))
        except ValidationError:
            return None
        return model.model_dump(mode="json"), tool_calls

    @staticmethod
    def _result(
        call: _Call,
        raw: RawCompletion,
        parsed: dict[str, Any] | None,
        tool_calls: tuple[ToolCall, ...],
    ) -> LlmResult:
        return LlmResult(
            text=raw.text,
            parsed=parsed,
            tool_calls=tool_calls,
            usage=LlmUsage(tokens_in=call.tokens_in, tokens_out=call.tokens_out),
            provider=call.provider,
            model=call.model.model,
            tier=call.tier,
            cost_micros=call.cost,
            attempts=call.attempts,
        )

    @staticmethod
    def _log_failure(call: _Call, code: str) -> None:
        log.warning(
            "llm.call_failed",
            run_id=call.run_id,
            provider=call.provider,
            model=call.model.model,
            prompt_id=call.request.prompt_id,
            attempts=call.attempts,
            cost_micros=call.cost,
            error_code=code,
        )


def _strip_fence(text: str) -> str:
    """Quita una cerca de Markdown alrededor del JSON (```json … ```), si la hay."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and stripped.count("```") == 2:
        body = stripped[3:-3]
        newline = body.find("\n")
        return body[newline + 1 :] if newline != -1 else body
    return stripped
