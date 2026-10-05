---
name: capa-llm
description: Cómo llama Faro a los modelos de IA (Anthropic, OpenAI, Gemini por AI Studio) desde el motor - LlmService como única puerta, LiteLLM endurecido detrás de LlmClient, catálogo models.json con precios, niveles economy/premium, orden de comprobaciones de una llamada, reintentos, costo en micros, reservas y tope diario por clave, errores llm.*, manejo de la clave en memoria y pruebas con FakeLLM. Úsala al escribir o cambiar cualquier código de faro_engine/llm, al hacer que un agente llame a un modelo o al tocar precios, límites o el modo --fake-llm.
---

# Capa de IA (`faro_engine/llm`)

> **Diseño de referencia de F1b** (spec `docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md` §4.1, ADR 0015 §1 y 0014). El código todavía no existe cuando se escribe esta skill: las plantillas muestran la forma acordada, no el código final. Al implementar (T6), ajusta nombres y firmas al código real y actualiza esta skill en el cierre (T14). Lo marcado **(verificar en T6)** depende de la versión de LiteLLM que fije `uv.lock`. Los puntos de T2 ya están resueltos con **LiteLLM 1.104.0** (informe `docs/qa/2026-10-05-f1b-t2-dependencias.md`, pruebas en `tests/llm/test_litellm_isolation.py`): si cambias la versión, vuelve a pasar esas pruebas y revisa la sección 2.

Dueño: `ingeniero-ia`. Revisión obligatoria de `revisor-seguridad` en todo cambio de esta carpeta.

## 1. La regla

- **Todo pasa por `LlmService.call(run, step, request)`.** Ningún agente, herramienta ni ruta llama a un proveedor por su cuenta.
- **Nadie importa `litellm` salvo `llm/litellm_client.py`.** Una prueba recorre `faro_engine/` y falla si otro módulo lo importa (también `from litellm …` o `importlib`).
- Quien llama dice **qué tipo de tarea** es (`task_kind`), nunca qué proveedor ni qué modelo: eso lo decide el servicio (preferencia del usuario → `routing` → `catalog`).
- Si LiteLLM no cumple los criterios de ADR 0015 §6, el plan B (adaptador propio sobre `httpx`) implementa el mismo `LlmClient` y no se toca ningún agente (decisión del usuario, spec §11-8).

```
llm/  client.py          LlmClient (protocolo), LlmRequest, LlmResult, LlmUsage
      litellm_client.py  única importación de litellm; configuración endurecida
      fake.py            FakeLLM del modo --fake-llm (también base de tests/fakes/llm.py)
      catalog.py         carga y valida models.json
      models.json        2 niveles × 3 proveedores, precios en micros/Mtok, verified_at
      routing.py         TaskKind → nivel
      pricing.py         costo en micros con enteros (redondeo hacia arriba)
      limits.py          tope diario por clave: reservas en memoria + gasto en credential_usage
      usage.py           repositorio de credential_usage, credential_limits, settings
      errors.py          excepciones de LiteLLM → llm.*
      service.py         LlmService.call(): orden de comprobaciones y registro
```

Tipos de referencia (`client.py`):

```python
# Diseño de referencia de F1b: ajustar al código real en T6.
Provider = Literal["anthropic", "openai", "gemini"]
Tier = Literal["economy", "premium"]
TaskKind = Literal["classify", "extract", "write", "plan"]

class LlmRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_kind: TaskKind
    messages: tuple[LlmMessage, ...]          # los arma prompts.render (skill prompts-y-evals)
    output_schema: type[BaseModel] | None = None
    max_output_tokens: int
    prompt_id: str
    prompt_version: int
    tools: tuple[ToolSpec, ...] = ()          # solo herramientas read (skill herramientas-de-agente)

class LlmUsage(BaseModel):
    tokens_in: int
    tokens_out: int

class LlmResult(BaseModel):
    text: str | None                          # salida libre (raro: casi siempre hay output_schema)
    parsed: dict[str, Any] | None             # salida estructurada ya validada, como dict JSON
    tool_calls: tuple[ToolCall, ...]
    usage: LlmUsage
    provider: Provider
    model: str
    tier: Tier

class LlmClient(Protocol):
    async def complete(self, call: ResolvedCall, api_key: str) -> RawCompletion: ...
```

`LlmClient` solo habla con el proveedor; **no** calcula costos, no reintenta, no pide claves ni escribe en la base. Todo eso es de `LlmService`.

## 2. Configuración endurecida de LiteLLM (ADR 0015 §1)

Se aplica al importar `litellm_client.py`; si algo no se puede aplicar, el módulo lanza una excepción y **el motor no arranca** (mejor parado que filtrando datos).

| Ajuste (LiteLLM 1.104.0) | Por qué (comprobado en T2) |
| --- | --- |
| `LITELLM_LOCAL_MODEL_COST_MAP=True` en el entorno **antes** del primer `import litellm` | Sin ella, `import litellm` intenta 3 veces descargar el mapa de precios de `raw.githubusercontent.com` (~10 s). Los precios de Faro salen de `models.json`. |
| `CUSTOM_TIKTOKEN_CACHE_DIR` = carpeta del motor con `cl100k_base` (archivo `9b5ad71b2ce5302211f9c61530b329a4922fc6a4`, SHA-256 `223921b7…65b2a7`), antes de la primera llamada | LiteLLM 1.104 **no** trae ese vocabulario y lo carga en cada llamada de **Anthropic y Gemini** (OpenAI no): sin él lo descarga de `openaipublic.blob.core.windows.net` y lo escribe en la carpeta del paquete. tiktoken comprueba el SHA-256. T6 lo incluye en el motor (PyInstaller: `--add-data` y el hook `hook-tiktoken.py`). |
| Fuera del entorno **antes** de importar: `SSL_VERIFY`, `SSLKEYLOGFILE`, `LANGSMITH_*`, `LANGCHAIN_*` | `SSL_VERIFY=False` desactiva la verificación TLS de LiteLLM (gana a `litellm.ssl_verify`); `SSLKEYLOGFILE` escribe las claves de sesión TLS en un archivo (en el equipo del usuario, Norton la apunta a su tubería); LangSmith: skill `agentes-langgraph`. |
| `truststore.inject_into_ssl()` al arrancar el motor, **antes de importar LiteLLM** | Al importarse, LiteLLM crea y guarda un contexto TLS con `certifi`: inyectar después no le llega (prueba). Criterio 8: con un antivirus que intercepta HTTPS (Norton en el equipo del usuario) `certifi` falla con `CERTIFICATE_VERIFY_FAILED`. `litellm.ssl_verify = <SSLContext>` **no** sirve (LiteLLM vuelve a `certifi`); la inyección sí llega a su transporte aiohttp. |
| Sin telemetría ni callbacks: `success_callback`, `failure_callback`, `callbacks`, `input_callback`, `service_callback` vacíos | Ningún prompt, respuesta ni clave sale hacia servicios de observabilidad. Con las listas vacías no hay intentos de red (prueba con sockets bloqueados). |
| `litellm.turn_off_message_logging = True`, `suppress_debug_info = True`, `log_raw_request_response = False`, `redact_messages_in_exceptions = True`, `disable_hf_tokenizer_download = True` | Nombres verificados en 1.104.0. Los prompts llevan contenido del sitio; nunca van a logs ni a excepciones. Sin descargas de tokenizadores de Hugging Face. |
| Loggers `LiteLLM*`: quitar sus handlers, `propagate = True`, nivel `WARNING` | Su `LevelRoutingStreamHandler` escribe en **stdout** todo lo que está por debajo de WARNING (p. ej. `LiteLLM completion() model=…`), y stdout es el canal del protocolo. Así pasan por los handlers del motor (JSON a stderr con redacción, ADR 0013). |
| `num_retries=0` en cada llamada | Los reintentos los hace Faro (§6) para registrar cada intento y respetar la pausa. |
| Sin caché de respuestas (`litellm.cache = None`) | Una respuesta en caché no es un dato del proveedor y escribiría contenido en disco o memoria larga. |
| Nunca `api_base`, `base_url`, `extra_headers` ni `custom_llm_provider` libres | Solo los hosts oficiales que implica el prefijo del catálogo. Un `api_base` permitiría mandar la clave a otro host. |
| Solo modelos del catálogo; Gemini solo `gemini/…` (AI Studio), nunca `vertex_ai/…` | Lista cerrada revisable en PR. Con `gemini/` la clave va en la cabecera `x-goog-api-key`, nunca en la URL (comprobado). |
| TLS verificado, sin seguir redirecciones a otros hosts, plazo 60 s por intento | ADR 0015 §1. Almacén del sistema con `truststore` (fila anterior). |

Más hechos de 1.104.0 que afectan al diseño (T2):
- **Importar tarda ~6,5 s** (1040 módulos, incluidos 21 de `litellm.proxy`): importa el adaptador de forma perezosa (primera llamada o en segundo plano después de `ready`), nunca antes de escribir `ready`.
- El transporte asíncrono por defecto es **aiohttp** (`LiteLLMAiohttpTransport`) dentro de un `httpx.AsyncClient`. La ruta `chat_completions` es `PYTHON_ONLY` en el puente Rust de 1.104 (`litellm/rust_bridge/catalog.py`): el binario nativo (~45 MB) no interviene en `acompletion` y el paquete puede excluirlo (`--exclude-module litellm.rust_bridge._native`; LiteLLM sigue por Python si falta).
- La clave viaja en `authorization` (OpenAI), `x-api-key` (Anthropic) y `x-goog-api-key` (Gemini).

Los hosts de los proveedores son fijos, así que estas llamadas **no** pasan por `faro_engine/net` (ADR 0012 cubre URLs no fijas). Una herramienta de agente que llame a una URL variable sí va por `net` (skill `herramientas-de-agente`).

Plantilla de referencia del adaptador:

```python
# llm/litellm_client.py — diseño de referencia de F1b; ajustes verificados en T2 con LiteLLM 1.104.0.
import logging
import os

# El arranque del motor ya quitó SSL_VERIFY, SSLKEYLOGFILE, LANGSMITH_* y LANGCHAIN_* del
# entorno y llamó a truststore.inject_into_ssl() (tabla anterior).
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"            # antes de importar
os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(TIKTOKEN_DIR)    # cl100k_base incluido en el motor

import litellm  # noqa: E402  (única importación de litellm en todo el motor)
from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients  # noqa: E402

def _harden() -> None:
    for name in ("success_callback", "failure_callback", "callbacks", "input_callback",
                 "service_callback"):
        setattr(litellm, name, [])
    litellm.cache = None
    litellm.turn_off_message_logging = True
    litellm.suppress_debug_info = True
    litellm.log_raw_request_response = False
    litellm.redact_messages_in_exceptions = True
    litellm.disable_hf_tokenizer_download = True
    for name in list(logging.root.manager.loggerDict):     # su handler escribe en stdout
        if name.startswith("LiteLLM"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
            logger.setLevel(logging.WARNING)
    # + comprobar después de asignarlos que quedaron así; si no, raise RuntimeError

_harden()

async def _drop_client_cache() -> None:
    # OpenAI guarda 1 h un AsyncOpenAI con la clave (y la clave en claro dentro de la clave
    # de la caché); Anthropic y Gemini, un AsyncHTTPHandler sin clave. Se vacía todo.
    await close_litellm_async_clients()
    litellm.in_memory_llm_clients_cache.flush_cache()

class LiteLlmClient:
    async def complete(self, call: ResolvedCall, api_key: str) -> RawCompletion:
        try:
            response = await litellm.acompletion(
                model=call.litellm_model,          # siempre del catálogo
                messages=call.messages,
                api_key=api_key,
                max_tokens=call.max_output_tokens,
                timeout=60,
                num_retries=0,
                **call.extra,                      # solo response_format y tools, construidos por Faro
            )
        finally:
            await _drop_client_cache()             # criterio 4 de ADR 0015 §6 (resuelto en T2)
        return _to_raw(response)                   # tokens de usage + texto/tool_calls; nada más
```

`_to_raw` copia solo lo necesario (texto, llamadas a herramientas, `prompt_tokens`/`completion_tokens`). El objeto de respuesta de LiteLLM no sale de este módulo.

## 3. Catálogo `models.json`

Forma (ejemplo; **los identificadores y precios reales los fija T6 con la documentación vigente de cada proveedor**):

```json
{
  "version": 1,
  "currency": "USD",
  "models": [
    {"provider": "anthropic", "tier": "economy", "model": "<id vigente>",
     "litellm_model": "anthropic/<id vigente>", "context_tokens": 200000, "max_output_tokens": 8192,
     "input_micros_per_mtok": 1000000, "output_micros_per_mtok": 5000000, "verified_at": "2026-10-xx"}
  ]
}
```

Reglas (las comprueba `catalog.py` al arrancar; si fallan, el motor **no arranca** la capa de IA):
- Exactamente **un** modelo por (`provider`, `tier`): 3 proveedores × 2 niveles = 6 entradas.
- Precios enteros > 0 (micros de USD por millón de tokens). `currency = "USD"`.
- `litellm_model` empieza por `anthropic/`, `openai/` o `gemini/` según `provider`. `vertex_ai/` o cualquier otro prefijo: rechazado.
- `verified_at` con fecha ISO; `max_output_tokens` ≤ lo que admite el modelo.

**Cambiar un modelo o un precio**
1. Toma el dato de la página oficial de precios o modelos del proveedor (no de la tabla de LiteLLM ni de blogs).
2. Cambia la entrada y pon `verified_at` = fecha de hoy.
3. En el PR: enlace a la fuente, precio anterior y nuevo, y el efecto en el estimado de cada agente (ejecuta sus evaluaciones, skill `prompts-y-evals`).
4. Pasa la prueba del catálogo (forma, un modelo por nivel, ningún precio 0, sin Vertex).

Cambiar el catálogo cambia el estimado y el máximo de todos los agentes: no lo mezcles con cambios de prompts.

## 4. Niveles y `task_kind`

| `task_kind` | Nivel | Uso |
| --- | --- | --- |
| `classify` | `economy` | ordenar, etiquetar, detectar idioma |
| `extract` | `economy` | sacar datos estructurados de un texto |
| `write` | `premium` | redactar lo que leerá el usuario |
| `plan` | `premium` | planificar pasos (F2 en adelante) |

- El usuario no elige modelos en F1b; elige **la clave que usan los agentes** (`settings.llm.preferred_provider`). Si no eligió, la primera con clave en el orden Anthropic, OpenAI, Gemini.
- **Sin cambio automático de proveedor** si uno falla (decisión del usuario, spec §11-4): la llamada falla con su `llm.*`.
- Un `task_kind` nuevo se añade aquí, en `routing.py` y en su prueba.

## 5. Orden de comprobaciones de una llamada

`LlmService.call(run, step, request)` hace siempre, en este orden (spec §4.1). Cada rechazo de los pasos 1–5 ocurre **sin pedir la clave** (hay prueba de cada uno):

1. **Pausa**: agentes pausados → `agents.paused`.
2. **Clave disponible**: el proveedor elegido está en `llm_providers` del último `agents_control` → si no, `llm.no_key`.
3. **Máximo de la llamada** = `ceil(caracteres_del_prompt / 3)` × precio de entrada + `max_output_tokens` × precio de salida (en micros, §7).
4. **Presupuesto de la tarea**: `run.cost_micros + máximo ≤ run.max_cost_micros` y `run.tokens + tokens_máximos ≤ run.token_budget` → si no, `agent.budget_exhausted`.
5. **Tope diario de la clave**: `gastado_hoy + reservado + máximo ≤ límite` → si no, `llm.daily_limit_reached`. Si cabe, **reserva** el máximo.
6. **Clave** por `secret_request` dentro de la concesión de la ejecución (ADR 0014): `with await secrets.get("llm/<proveedor>/default", max_wait=…) as secret:` (§9).
7. **Llamada y reintentos** (§6). Cada intento suma `attempts` en el paso.
8. **Registro**: costo real = tokens informados × precios del catálogo. En **una** transacción: `agent_steps` (tokens, costo, `provider`, `model`, `tier`, `secret_ref`, `prompt_id`, `prompt_version`), acumulados de `agent_runs` y `credential_usage` del día local. Se libera la reserva.
9. **Cancelada o sin respuesta** (cierre del motor, `CancelledError`): el paso queda `cost_estimated = 1` y se cuenta el **máximo reservado** como gastado (conservador).
10. **Salida estructurada inválida**: un reintento con el mismo prompt (cuenta en presupuesto y tope); si vuelve a fallar, `llm.bad_output`.

Ejemplo de uso desde un nodo de agente (skill `agentes-langgraph`):

```python
# Diseño de referencia de F1b. El nodo lo envuelve StepRecorder (skill agentes-langgraph),
# que crea el paso y se lo pasa; `ctx` es el RunContext de la ejecución (el "run" de la firma).
async def classify_content(state: SiteSummaryState, *, ctx: RunContext, step: StepHandle,
                           deps: AgentDeps) -> dict[str, Any]:
    messages = deps.prompts.render(
        "site_summary.classify_content", 1,
        instructions_vars={"max_items": 150},
        data=[DataBlock(source="site", items=site_items(state), max_chars=20_000)],
    )
    result = await deps.llm.call(ctx, step, LlmRequest(
        task_kind="classify",
        messages=messages,
        output_schema=ContentClassificationV1,
        max_output_tokens=800,
        prompt_id="site_summary.classify_content",
        prompt_version=1,
    ))
    return {"classification": result.parsed}     # dict JSON, nunca el objeto Pydantic
```

`LlmService` lee los acumulados de la tarea (`cost_micros`, tokens) de la base, no del estado del grafo.

Lo que **no** hace quien llama: elegir modelo, reintentar, capturar errores `llm.*` para seguir como si nada, ni registrar tokens a mano.

## 6. Reintentos

Son de Faro, no de LiteLLM (`num_retries=0`).

| Situación | ¿Reintenta? |
| --- | --- |
| 429 (límite de frecuencia) | Sí |
| 5xx del proveedor | Sí |
| Tiempo agotado | Sí |
| Error de red o DNS | Sí |
| 400 (petición mal formada, contexto excedido) | **No** |
| 401 / clave inválida | **No** |
| 403 / permiso | **No** |
| Falta de saldo o cuota agotada | **No** (aunque llegue como 429) |
| Contenido bloqueado por políticas del proveedor | **No** |
| Agentes pausados mientras se esperaba | **No**: sale con `agents.paused` |

- Hasta **2** reintentos (3 intentos en total). Espera 2 s y luego 6 s, con ±20 % aleatorio, o el `Retry-After` del proveedor si es ≤ 20 s (si es mayor, no se reintenta y sale el código de esa situación).
- Antes de cada reintento se vuelve a comprobar la pausa (paso 1 de §5). Si un intento fallido pudo consumir tokens (p. ej. tiempo agotado), el presupuesto de la tarea y la reserva del tope se vuelven a comprobar para el nuevo intento; cómo se cuenta lo consumido por un intento sin respuesta lo fija T6 (conservador: el máximo del intento). La clave **no** se vuelve a pedir entre intentos de la misma llamada lógica (una petición por llamada lógica, spec §2.2).
- Reloj y espera inyectables (`sleep` y `jitter` como dependencias): las pruebas no esperan.

## 7. Dinero: micros, reservas y día local

- Todo en **micros de USD** (`int`). Nunca `float`, nunca `Decimal` en la base.
- Costo de una cantidad de tokens, redondeando hacia arriba:

```python
def cost_micros(tokens: int, micros_per_mtok: int) -> int:
    return (tokens * micros_per_mtok + 999_999) // 1_000_000

def call_cost(usage: LlmUsage, model: CatalogModel) -> int:
    return cost_micros(usage.tokens_in, model.input_micros_per_mtok) + cost_micros(
        usage.tokens_out, model.output_micros_per_mtok
    )
```

- **Tope diario por clave** (`credential_limits.daily_limit_micros`, por defecto 5 000 000 = US$5, decisión del usuario; rango 500 000–500 000 000). Sin fila = US$5.
- **Reservas en memoria** (`limits.py`): antes de llamar se reserva el máximo de la llamada bajo un candado `asyncio.Lock` por `secret_ref`; al registrar el costo real se libera. Así dos llamadas concurrentes no pueden pasar el tope entre las dos (prueba obligatoria). Las reservas no se guardan: tras un cierre brusco, el paso 9 de §5 ya contó el máximo como gastado.
- **Día local**: `usage_date` = fecha de la computadora del usuario (`AAAA-MM-DD`, empieza a medianoche), con reloj inyectado. Una llamada que empieza a las 23:59 y termina a las 00:01 cuenta en el día en que **se registra**. Pruebas con `America/Guatemala` además de UTC.
- Cambiar un límite o la preferencia → `audit_log` (`llm.limit_changed`, `llm.preference_changed`, actor `user`).

## 8. Errores `llm.*`

`errors.py` traduce las excepciones del cliente a `FaroError` con `details.provider` siempre y **nunca** el cuerpo ni el mensaje de la respuesta del proveedor (pueden repetir parte del prompt o de la clave).

| Situación del proveedor | `code` |
| --- | --- |
| 401, clave inválida o revocada | `llm.invalid_key` |
| Sin saldo, cuota agotada | `llm.insufficient_quota` |
| 429 tras los reintentos | `llm.rate_limited` |
| 5xx tras los reintentos | `llm.provider_error` |
| Red o DNS | `llm.unreachable` |
| Tiempo agotado | `llm.timeout` |
| Bloqueado por políticas del proveedor | `llm.content_blocked` |
| Salida estructurada inválida dos veces | `llm.bad_output` |
| Sin clave del proveedor elegido | `llm.no_key` |
| Tope diario | `llm.daily_limit_reached` |
| Proveedor fuera de la lista (rutas) | `llm.invalid_provider` |
| Límite fuera de rango (rutas) | `llm.invalid_limit` |

- **(verificar en T6)** Las clases de excepción exactas de LiteLLM y cómo distingue cada proveedor "sin saldo" (OpenAI lo manda como 429 con un código propio; Anthropic como 400 con un mensaje): el mapeo puede necesitar mirar el código de error del proveedor, nunca registrarlo. Cada fila tiene su prueba con `respx`.
- Cualquier excepción no reconocida → `llm.provider_error` y una línea de log con solo el nombre de la clase.
- Los mensajes para el usuario están en `locales/es/errors.json` (spec §5.5) con `{{provider}}`.

## 9. La clave en memoria

- La clave solo llega por la **concesión de la ejecución** (ADR 0014): una petición `get` de `llm/<proveedor>/default` por llamada lógica. Ninguna operación de `engine-operations.json` declara `llm/*` en F1b.
- `SecretValue` (de `core/secrets.py`) se usa con `with` y se sobrescribe al salir. LiteLLM exige `str`: la copia se crea **dentro** del `with`, se pasa al adaptador y se suelta (`del`) en el `finally`. Python no garantiza borrar esa copia (riesgo aceptado, spec §7); no la guardes en ningún objeto que viva más que la llamada.
- Tras cada llamada se vacía la caché de clientes HTTP de LiteLLM (resuelto en T2 con 1.104.0): es `litellm.in_memory_llm_clients_cache` (`LLMClientCache`, TTL 3600 s). Con **OpenAI** guarda un `AsyncOpenAI` que lleva la clave, y la clave de la caché contiene la clave **en claro** (además de su SHA-256); con Anthropic y Gemini guarda un `AsyncHTTPHandler` sin clave. Se vacía con `await close_litellm_async_clients()` + `litellm.in_memory_llm_clients_cache.flush_cache()`. Tras vaciar y terminar las tareas de registro de LiteLLM en segundo plano no queda en memoria ningún diccionario con la clave (prueba en `tests/llm/test_litellm_isolation.py`). El vaciado es global: con un solo trabajador (spec §4.3) no hay llamadas concurrentes; si algún día las hay, hazlo bajo un candado.
- La clave nunca va a: SQLite (tampoco checkpoints ni `agent_steps`: solo `secret_ref`), prompts, eventos `agent_activity`, logs, excepciones, `details` de errores ni la interfaz.

```python
# Diseño de referencia de F1b.
async def _call_with_key(self, call: ResolvedCall, deadline: Deadline) -> RawCompletion:
    with await self._secrets.get(call.secret_ref, max_wait=deadline.remaining()) as secret:
        api_key = secret.buffer.decode("ascii")   # copia str inevitable para LiteLLM
        try:
            return await self._retrying(call, api_key)
        finally:
            del api_key
```

`vault.secret_not_allowed` a mitad de una tarea significa que la concesión caducó o se revocó (pausa): lo gestiona el trabajador (`core/jobs/worker.py`), no la capa de IA.

## 10. Pruebas

- **`FakeLLM`** (`llm/fake.py`, base de `tests/fakes/llm.py`): implementa `LlmClient`; responde por `prompt_id` con fixtures, cuenta tokens de forma determinista y puede simular cada error de §8, salidas inválidas, llamadas a herramientas y respuestas "que obedecen" a una inyección (skill `prompts-y-evals`).
- **Adaptador** contra un servidor falso para los tres proveedores (forma de la petición, `api_key` en la cabecera correcta —`authorization`, `x-api-key`, `x-goog-api-key`—, ningún `api_base` en producción, tokens leídos de la respuesta, cada error mapeado). `respx` solo intercepta el transporte de httpx, no el aiohttp que LiteLLM usa por defecto: usa un servidor en loopback como `tests/deps/offline_probe.py`, o `respx` si T6 decide `litellm.disable_aiohttp_transport = True`.
- **Sockets bloqueados**: el fixture automático de `tests/conftest.py` impide toda conexión que no sea a loopback. `tests/llm/test_litellm_isolation.py` (creado en T2 contra LiteLLM directamente; T6 lo pasa al adaptador) llama a un servidor falso desde un proceso con la red bloqueada: no sale nada a internet y la caché queda vacía (criterio 4).
- **Clave falsa** con forma evidente (`sk-ant-test-…`, `AIzaTEST…`): no aparece en logs capturados, `agent_steps`, checkpoints, respuestas ni excepciones.
- **Obligatorias** (spec §9.2): costo con enteros y redondeo; selección de proveedor y nivel; cada rechazo de §5 con su código y sin pedir la clave; reservas concurrentes no pasan el tope; cambio de día local; reintentos y no-reintentos de §6; `Retry-After`; salida inválida → un reintento → `llm.bad_output`; llamada cancelada → `cost_estimated` con el máximo; catálogo inválido (modelo repetido, precio 0, Vertex) → no arranca; ningún otro módulo importa `litellm`.
- Cobertura 95 % en `llm/pricing.py`, `llm/limits.py`, `llm/service.py` (`strict_modules`).

### Modo `--fake-llm` (solo desarrollo)

- El motor lo acepta solo sin `sys.frozen` (si no, sale con código 2). El núcleo lo pasa solo en build de depuración **y** con `FARO_FAKE_LLM=1` exacto (entorno o `.env.local`), igual que `--allow-local-sites`.
- Usa `FakeLLM` con respuestas fijas por `prompt_id` y un **precio de prueba alto** para poder llegar al tope diario con pocas tareas.
- No pide ninguna clave por `secret_request`, pero **sí** pide la concesión de la ejecución (el flujo de concesiones se ejercita igual).
- Marca cada paso con `model = "fake"`.

## Lista de verificación

- [ ] Ningún módulo fuera de `llm/litellm_client.py` importa `litellm`.
- [ ] La configuración endurecida se aplica al importar y el motor no arranca si falla.
- [ ] Ningún `api_base`/`base_url`/cabecera extra; modelos solo del catálogo; Gemini solo `gemini/`.
- [ ] Orden de §5 respetado; los rechazos 1–5 no piden la clave.
- [ ] Costos en micros enteros con redondeo hacia arriba; nada de `float`.
- [ ] Reservas bajo candado; prueba de concurrencia en verde.
- [ ] Errores `llm.*` con `details.provider` y sin cuerpo del proveedor.
- [ ] La clave no aparece en logs, base, checkpoints, eventos ni respuestas (prueba de captura).
- [ ] Cambios de `models.json` con fuente oficial, `verified_at` y efecto en los estimados.
- [ ] `--fake-llm` rechazado con `sys.frozen`.
