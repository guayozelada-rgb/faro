---
name: capa-llm
description: Cómo llama Faro a los modelos de IA (Anthropic, OpenAI, Gemini por AI Studio) desde el motor - LlmService como única puerta, LiteLLM endurecido detrás de LlmClient, catálogo models.json con precios, niveles economy/premium, orden de comprobaciones de una llamada, reintentos, costo en micros, reservas y tope diario por clave, errores llm.*, manejo de la clave en memoria y pruebas con FakeLLM. Úsala al escribir o cambiar cualquier código de faro_engine/llm, al hacer que un agente llame a un modelo o al tocar precios, límites o el modo --fake-llm.
---

# Capa de IA (`faro_engine/llm`)

> Refleja el código de `faro_engine/llm` al cierre de F1b (T14, 2026-10-20): spec `docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md` §4.1 y §13.2, ADR 0015 §1 (y su actualización del cierre) y ADR 0014. Versión fijada: **LiteLLM 1.104.0** (informe `docs/qa/2026-10-05-f1b-t2-dependencias.md`; pruebas en `tests/llm/test_litellm_isolation.py`): si cambias la versión, vuelve a pasar esas pruebas y revisa la sección 2. Decisiones de T6 en `docs/qa/2026-10-10-f1b-t6-capa-llm.md`.

Dueño: `ingeniero-ia`. Revisión obligatoria de `revisor-seguridad` en todo cambio de esta carpeta.

## 1. La regla

- **Todo pasa por `LlmService.call(run, step, request)`.** Ningún agente, herramienta ni ruta llama a un proveedor por su cuenta. Desde un nodo de agente se llama con `NodeContext.call_llm(prompt, input_tokens_bound=…)`, cota obligatoria (skill `agentes-langgraph`), que arma el `LlmRequest` desde el prompt renderizado.
- **Nadie importa `litellm` salvo `llm/litellm_client.py`.** Una prueba recorre `faro_engine/` y falla si otro módulo lo importa (también `from litellm …` o `importlib`).
- Quien llama dice **qué tipo de tarea** es (`task_kind`), nunca qué proveedor ni qué modelo: eso lo decide el servicio (preferencia del usuario → `routing` → `catalog`).
- Si LiteLLM no cumple los criterios de ADR 0015 §6, el plan B (adaptador propio sobre `httpx`) implementa el mismo `LlmClient` y no se toca ningún agente (decisión del usuario, spec §11-8).

```
llm/  client.py          LlmClient (protocolo), LlmRequest, LlmResult, LlmUsage, ResolvedCall, RawCompletion
      service.py         LlmService.call(): orden de comprobaciones, intentos y registro; LazyLiteLlmClient
      litellm_client.py  única importación de litellm; cliente HTTP propio por llamada
      hardening.py       entorno, API_BASE fijo, ajustes de LiteLLM, loggers, cl100k_base (sin importar litellm)
      fake.py            FakeLLM del modo --fake-llm (también base de tests/fakes/llm.py), fake_catalog()
      catalog.py         carga y valida models.json
      models.json        2 niveles × 3 proveedores, precios en micros/Mtok, long_context, price_changes, source
      tiktoken/          cl100k_base incluido (SHA-256 comprobado)
      routing.py         TaskKind → nivel; choose_provider; secret_ref_for
      pricing.py         costo en micros con enteros (redondeo hacia arriba); máximo de una llamada
      limits.py          DailyLimiter: reservas en memoria bajo candado + gasto en credential_usage
      usage.py           SQL de credential_usage, credential_limits, settings y acumulados de la tarea
      settings.py        cambiar el tope diario y la preferencia (con auditoría)
      errors.py          excepciones del adaptador → LlmCallError(kind) → llm.*
```

Tipos (`client.py`, resumidos):

```python
Provider = Literal["anthropic", "openai", "gemini"]
Tier = Literal["economy", "premium"]
TaskKind = Literal["classify", "extract", "write", "plan"]

class LlmRequest(BaseModel):                  # frozen, extra="forbid"
    task_kind: TaskKind
    messages: tuple[LlmMessage, ...]          # 1–32; solo los arma prompts.render (skill prompts-y-evals)
    output_schema: type[BaseModel] | None = None
    max_output_tokens: int                    # 1–128 000 y ≤ el del modelo del catálogo
    prompt_id: str
    prompt_version: int
    tools: tuple[ToolSpec, ...] = ()          # solo herramientas read (skill herramientas-de-agente)
    input_tokens_bound: int | None = None     # cota de entrada del agente (T9-C1); ≥ input_bytes (prompt + esquema + tools)

class LlmResult(BaseModel):
    text: str | None
    parsed: dict[str, Any] | None             # salida estructurada ya validada, como dict JSON
    tool_calls: tuple[ToolCall, ...]
    usage: LlmUsage                           # tokens_in, tokens_out (suma de intentos con respuesta)
    provider: Provider
    model: str
    tier: Tier
    cost_micros: int                          # costo registrado de la llamada lógica
    attempts: int

class LlmClient(Protocol):
    @property
    def requires_key(self) -> bool: ...       # False solo en FakeLLM: no se pide clave
    async def prepare(self) -> None: ...      # deja el cliente listo ANTES de pedir la clave
    async def complete(self, call: ResolvedCall, api_key: str | None) -> RawCompletion: ...
```

`ResolvedCall` (modelo del catálogo, mensajes, `max_output_tokens`, `response_format`, `tools`) no lleva nada que pueda cambiar el host. `RawCompletion` es lo único que sale del adaptador: texto, llamadas a herramientas, `finish_reason` y tokens (o `None` si el proveedor no los informó).

`LlmClient` solo habla con el proveedor; **no** calcula costos, no reintenta, no pide claves ni escribe en la base. Todo eso es de `LlmService`.

## 2. Configuración endurecida de LiteLLM (ADR 0015 §1)

Se aplica al importar `litellm_client.py`; si algo no se puede aplicar, el módulo lanza una excepción y **el motor no arranca** (mejor parado que filtrando datos).

| Ajuste (LiteLLM 1.104.0) | Por qué (comprobado en T2) |
| --- | --- |
| `LITELLM_MODE=PRODUCTION` y `LITELLM_LOCAL_MODEL_COST_MAP=True` en el entorno **antes** del primer `import litellm` | Con `LITELLM_MODE` en `DEV` (valor por defecto), `import litellm` llama a `load_dotenv()` y carga un `.env` buscado hacia arriba (desde el directorio de trabajo o desde la carpeta del paquete): reintroduce variables ya limpiadas, p. ej. `OPENAI_BASE_URL` (revisión de seguridad de T2). Sin el mapa local, `import litellm` intenta 3 veces descargar el mapa de precios de `raw.githubusercontent.com` (~10 s); los precios de Faro salen de `models.json`. |
| `CUSTOM_TIKTOKEN_CACHE_DIR` = carpeta del motor con `cl100k_base` (archivo `9b5ad71b2ce5302211f9c61530b329a4922fc6a4`, SHA-256 `223921b7…65b2a7`), antes de la primera llamada | LiteLLM 1.104 **no** trae ese vocabulario y lo carga en cada llamada de **Anthropic y Gemini** (OpenAI no): sin él lo descarga de `openaipublic.blob.core.windows.net` y lo escribe en la carpeta del paquete. tiktoken comprueba el SHA-256. Va en `llm/tiktoken/` dentro del paquete del motor (PyInstaller: `--collect-data faro_engine` y el hook `hook-tiktoken.py`). |
| Fuera del entorno **antes y después** de importar, y comprobado después (si queda alguna, `raise` y el adaptador no se carga), comparando en mayúsculas: todo `SSL_*` (`SSL_VERIFY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `SSL_CERTIFICATE`, `SSL_SECURITY_LEVEL`, `SSL_ECDH_CURVE`…), `SSLKEYLOGFILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` y `NO_PROXY` (también en minúsculas), `DISABLE_AIOHTTP_TRANSPORT`, todo `AIOHTTP_*` (`AIOHTTP_TRUST_ENV`), `GOOGLE_API_KEY`, `LANGSMITH_*`, `LANGCHAIN_*`, el resto de `LITELLM_*` (incluido `LITELLM_LOG`) y todas las `OPENAI_*`, `ANTHROPIC_*` y `GEMINI_*` (incluidas `*_API_KEY`, `*_API_BASE` y `*_BASE_URL`). `CUSTOM_TIKTOKEN_CACHE_DIR` se fija (fila anterior), nunca se hereda | LiteLLM lee `OPENAI_BASE_URL`, `OPENAI_API_BASE`, `ANTHROPIC_API_BASE`, `ANTHROPIC_BASE_URL` y `GEMINI_API_BASE` cuando no recibe `api_base`: con una apuntando a otro host, la clave se envía allí (el revisor lo reprodujo en loopback). `SSL_VERIFY=False` desactiva la verificación TLS de LiteLLM (gana a `litellm.ssl_verify`); `SSLKEYLOGFILE` escribe las claves de sesión TLS en un archivo (en el equipo del usuario, Norton la fija; `__main__` ya la quita al arrancar); LangSmith: skill `agentes-langgraph`. Con `DISABLE_AIOHTTP_TRANSPORT=True` o `AIOHTTP_TRUST_ENV=True`, LiteLLM envía la clave al proxy del entorno (prueba de control de la segunda revisión). La limpieza se repite tras el import por si algo la deshace (`load_dotenv`). El proxy del registro de Windows no es una variable: solo lo evita el cliente propio (fila "Cliente HTTP propio"). |
| **`sys.path` sin el directorio de trabajo**: justo después de `import litellm`, quitar `os.getcwd()` (y `""`) de `sys.path`; el núcleo lanza el motor con un directorio de trabajo fijo y no escribible | `import litellm` ejecuta `sys.path.append(os.getcwd())` (`litellm/proxy/proxy_cli.py`) y tiktoken importa todo `tiktoken_ext/*.py` que encuentre en `sys.path`: un archivo en el directorio de trabajo se ejecutaría en el proceso que tiene las claves (segunda revisión de T2, condición 14). Prueba obligatoria con un `tiktoken_ext/x.py` en el directorio de trabajo. |
| `faro_engine.net.tls.install_system_trust_for_libraries()` **antes de importar LiteLLM** (segunda barrera: lo que protege es el contexto explícito de la fila "Cliente HTTP propio"). Nunca `truststore.inject_into_ssl()` directamente: la comprobación estática prohíbe `inject_into_ssl` e importar `truststore` fuera de `net/tls.py` (ADR 0012, condición 13). La función construye antes el contexto del motor y solo inyecta con el almacén del sistema. **No inyecta `truststore.SSLContext`, sino la subclase con cerrojo** del motor (revisiones de T2b): `ssl.SSLContext`, `ssl.create_default_context()` y `urllib3.util.ssl_.SSLContext` dan `_LockedContext`, así que los contextos compartidos que LiteLLM y aiohttp crean después (`_ssl_context_cache`, `aiohttp.connector._SSL_CONTEXT_VERIFIED`) serializan `wrap_bio`/`wrap_socket`/`do_handshake` con su cerrojo y verifican en todos los hilos en vez de quedar en `CERT_NONE`, y llevan `OP_NO_RENEGOTIATION`. `scripts/bundle_smoke.py` exige que el contexto de LiteLLM sea `_LockedContext`. **Excepción conocida:** `httpx2`/`httpcore2` (de `langsmith`) crean `truststore.SSLContext` directamente, sin cerrojo; se acepta solo con LangSmith desactivado (ADR 0012, condición 13) | Al importarse, LiteLLM crea y guarda un contexto TLS con `certifi`: inyectar después no le llega (prueba). Criterio 8: con un antivirus que intercepta HTTPS (Norton en el equipo del usuario) `certifi` falla con `CERTIFICATE_VERIFY_FAILED`. `litellm.ssl_verify = <SSLContext>` **no** sirve (LiteLLM vuelve a `certifi`); la inyección sí llega a su transporte aiohttp. |
| Sin telemetría ni callbacks: `success_callback`, `failure_callback`, `callbacks`, `input_callback`, `service_callback` vacíos | Ningún prompt, respuesta ni clave sale hacia servicios de observabilidad. Con las listas vacías no hay intentos de red (prueba con sockets bloqueados). |
| `litellm.turn_off_message_logging = True`, `suppress_debug_info = True`, `log_raw_request_response = False`, `redact_messages_in_exceptions = True`, `disable_hf_tokenizer_download = True` | Nombres verificados en 1.104.0. Los prompts llevan contenido del sitio; nunca van a logs ni a excepciones. Sin descargas de tokenizadores de Hugging Face. |
| Loggers `LiteLLM*`: quitar sus handlers, `propagate = True`, nivel `WARNING` | Su `LevelRoutingStreamHandler` escribe en **stdout** todo lo que está por debajo de WARNING (p. ej. `LiteLLM completion() model=…`), y stdout es el canal del protocolo. Así pasan por los handlers del motor (JSON a stderr con redacción, ADR 0013). Prueba obligatoria: con `LITELLM_LOG=DEBUG` en el entorno, stdout solo contiene líneas del protocolo (el núcleo trata como `secret_request` una línea JSON de stdout, que podría venir de una página rastreada). |
| `num_retries=0` en cada llamada | Los reintentos los hace Faro (§6) para registrar cada intento y respetar la pausa. |
| Sin caché de respuestas (`litellm.cache = None`) | Una respuesta en caché no es un dato del proveedor y escribiría contenido en disco o memoria larga. |
| **`api_base` fijo al host oficial del catálogo** y `api_key` explícita en cada llamada; nunca `base_url`, `extra_headers` ni `custom_llm_provider` libres | Sin `api_base`, LiteLLM lo toma del entorno (fila anterior). Valores comprobados con 1.104.0: OpenAI `https://api.openai.com/v1`, Anthropic `https://api.anthropic.com`, Gemini `https://generativelanguage.googleapis.com/v1beta` (LiteLLM añade `/chat/completions`, `/v1/messages` y `/models/<id>:generateContent`). Salen de una tabla fija en código revisable en PR (solo `https`, host exacto, sin puerto), nunca del usuario ni del entorno. |
| Solo modelos del catálogo; Gemini solo `gemini/…` (AI Studio), nunca `vertex_ai/…` | Lista cerrada revisable en PR. Con `gemini/` la clave va en la cabecera `x-goog-api-key`, nunca en la URL (comprobado). |
| **Cliente HTTP propio** en cada llamada (parámetro `client` de `acompletion`): `follow_redirects=False`, `trust_env=False` y el contexto TLS compartido del motor, `faro_engine.net.tls.tls_context()` (almacén del sistema con `truststore`; condiciones en ADR 0012, actualización 2026-10-06; **no** construyas otro `SSLContext` aquí). Anthropic y Gemini: `AsyncHTTPHandler(transport=httpx.AsyncHTTPTransport(verify=tls_context(), trust_env=False), follow_redirects=False)`. OpenAI: `AsyncOpenAI(api_key=…, base_url=API_BASE, http_client=httpx.AsyncClient(transport=<igual>, follow_redirects=False, trust_env=False), max_retries=0)`. Se cierra en el `finally`. Plazo 60 s por intento | Sin él, LiteLLM 1.104 sigue redirecciones (`AsyncHTTPHandler(follow_redirects=True)`, `http_handler.py:618`, y el cliente de OpenAI): un 307 del host oficial a otro origen recibe `x-api-key` o `x-goog-api-key` (httpx solo quita `authorization`); con las variables de la fila de limpieza usa además proxies del entorno, y `getproxies()` lee en Windows el proxy del registro. Con el cliente propio no pasa ninguna de las dos cosas y no se usa aiohttp. Comprobado en loopback (`tests/llm/test_manual_llm_check.py`, con prueba de control); ADR 0015 §1; condición 15 del informe. `litellm.aclient_session` no sirve: solo lo usa OpenAI y es global. |

**Condiciones de la revisión de seguridad de T2b** (cumplidas en T6) (ADR 0012, actualización 2026-10-06, condiciones 12 y 13 y Consecuencias). `truststore` cambia el contexto durante cada `wrap_bio`/`wrap_socket`, así que el contexto del motor lleva **un cerrojo por contexto** que cubre esa ventana y cada `do_handshake`: cualquier hilo puede usarlo (anyio, que usa httpx, hace `wrap_bio` en un hilo de trabajo y el handshake en el bucle) y todas las conexiones verifican. Ya no existe `WrongThreadError` (el "dueño de hilo" anterior rompía todo el HTTPS real con anyio):

- solo `acompletion`, con el cliente asíncrono propio de cada llamada (fila "Cliente HTTP propio");
- prohibidos `litellm.completion` síncrono, `litellm.ssl_verify`, `litellm.aclient_session` y cualquier cliente que LiteLLM construya por su cuenta;
- la inyección global, solo con `install_system_trust_for_libraries()` antes de importar LiteLLM (inyecta la subclase con cerrojo, que también lo toma en el handshake: ADR 0012, condiciones 12 y 13);
- nada de `requests`, `urllib3` ni `aiohttp` directamente en `faro_engine`, y todo transporte de `httpx` con `verify=tls_context()` y `trust_env=False` (la comprobación estática de `tests/net/test_tls.py` lo exige);
- nunca `wrap_socket` (bloqueante) con un contexto compartido en el hilo del bucle: retendría el cerrojo durante todo el handshake de red;
- prueba obligatoria: espiar `wrap_bio`, `wrap_socket` y `do_handshake` y demostrar que, durante `acompletion` con los tres proveedores, (a) todos los contextos TLS que se usan son `tls_context()` o un `_LockedContext` (nunca un `truststore.SSLContext` sin cerrojo) y, tras cada llamada, siguen en `CERT_REQUIRED` con `check_hostname`; (b) no se usa ningún contexto de `_ssl_context_cache` de LiteLLM; y (c) nada llama a `wrap_socket` en el hilo del bucle. Que `wrap_bio` ocurra en un hilo de trabajo es lo normal con anyio.

Las **condiciones obligatorias de la revisión de seguridad de T2** (lista completa, incluidas las de T7 y T11) están en el informe `docs/qa/2026-10-05-f1b-t2-dependencias.md` §12; T6 las cumple todas y las prueba (`tests/llm/`); el adaptador las reúne en `hardening.py` y `litellm_client.py`.

Más hechos de 1.104.0 que afectan al diseño (T2):
- **Importar tarda ~6,5 s** (1040 módulos, incluidos 21 de `litellm.proxy`): el adaptador se importa de forma perezosa, en un hilo, con la primera llamada (`LazyLiteLlmClient`), nunca antes de escribir `ready`.
- El transporte asíncrono por defecto es **aiohttp** (`LiteLLMAiohttpTransport`) dentro de un `httpx.AsyncClient`. La ruta `chat_completions` es `PYTHON_ONLY` en el puente Rust de 1.104 (`litellm/rust_bridge/catalog.py`): el binario nativo (~45 MB) no interviene en `acompletion` y el paquete puede excluirlo (`--exclude-module litellm.rust_bridge._native`; LiteLLM sigue por Python si falta).
- La clave viaja en `authorization` (OpenAI), `x-api-key` (Anthropic) y `x-goog-api-key` (Gemini).

Los hosts de los proveedores son fijos, así que estas llamadas **no** pasan por `faro_engine/net` (ADR 0012 cubre URLs no fijas). Una herramienta de agente que llame a una URL variable sí va por `net` (skill `herramientas-de-agente`).

**Cómo está hecho** (`llm/litellm_client.py`, `llm/hardening.py`). `LlmService` usa `LazyLiteLlmClient`, que importa el adaptador en un hilo con la primera llamada real (`prepare()`), nunca antes de `ready`. Al importarse, el adaptador, en este orden:

1. comprueba el SHA-256 de `cl100k_base` (`hardening.verify_tiktoken_file`);
2. limpia y comprueba el entorno (`clean_environment`, `check_environment`; `required_env()` = `LITELLM_MODE=PRODUCTION`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True` —sin ella, la salida estructurada de Anthropic descarga la tabla de cabeceras beta de LiteLLM de `raw.githubusercontent.com`— y `CUSTOM_TIKTOKEN_CACHE_DIR`);
3. `install_system_trust_for_libraries()`;
4. `import litellm`, `openai` y lo necesario (incluido `vertex_ai.context_caching`, que añade el callback "cache" al importarse: se importa antes de vaciar los callbacks);
5. quita el directorio de trabajo de `sys.path` y vuelve a limpiar y comprobar el entorno;
6. aplica y comprueba los ajustes de LiteLLM y de sus loggers.

Si algo falla, el módulo no se carga y la llamada sale con `llm.provider_error` (`adapter_unavailable`). En cada llamada (`LiteLlmClient.complete`): sin clave → `invalid_key`; **con herramientas → se rechaza sin enviar nada** (`bad_request`: con `tools`, LiteLLM 1.104 lleva los `gpt-6*` por un camino con su propio cliente aiohttp, sin el cliente propio; habilitarlas exige antes una prueba por proveedor con `respx`, pendiente T6-C4); `api_base` = `hardening.official_api_base(provider)`; cliente propio (`build_client`); `acompletion(..., num_retries=0, timeout=60)`; en el `finally`, `_cleanup` cierra el cliente, vacía la caché de clientes (`drop_client_cache`) y vuelve a vaciar los callbacks, cada paso protegido por separado. La excepción se traduce con `classify_exception` **fuera** del `except`, para que el mensaje del proveedor no quede encadenado.

## 3. Catálogo `models.json`

Forma real (extracto):

```json
{
  "version": 1,
  "currency": "USD",
  "models": [
    {"provider": "anthropic", "tier": "economy", "model": "claude-haiku-5-5",
     "litellm_model": "anthropic/claude-haiku-5-5", "context_tokens": 1000000, "max_output_tokens": 128000,
     "input_micros_per_mtok": 100000, "output_micros_per_mtok": 500000,
     "long_context": {"above_input_tokens": 100000, "input_micros_per_mtok": 500000, "output_micros_per_mtok": 2500000},
     "price_changes": [], "source": "<página oficial de precios>", "verified_at": "2026-10-09"}
  ]
}
```

Modelos al cierre de F1b (verificados el 2026-10-09):

| Proveedor | `economy` | `premium` |
| --- | --- | --- |
| Anthropic | `claude-haiku-5-5` | `claude-opus-5-5` |
| OpenAI | `gpt-6-luna` | `gpt-6.1-sol` |
| Gemini (AI Studio) | `gemini-3.5-flash-lite` | `gemini-3.8-flash` (cambio de precio con fecha `2027-01-01` en `price_changes`) |

- `long_context` (opcional): precio distinto cuando los tokens de entrada pasan `above_input_tokens`. `pricing.prices_for(model, día, tokens_de_entrada)` elige el precio por fecha (`price_changes`, `effective_from`) y por tamaño; el máximo de una llamada usa siempre el peor caso.
- `source`: la página oficial de la que salió el precio.

Reglas (las comprueba `catalog.py` al arrancar; si fallan, el motor **no arranca** la capa de IA):
- Exactamente **un** modelo por (`provider`, `tier`): 3 proveedores × 2 niveles = 6 entradas.
- Precios enteros > 0 (micros de USD por millón de tokens). `currency = "USD"`.
- `litellm_model` empieza por `anthropic/`, `openai/` o `gemini/` según `provider`. `vertex_ai/` o cualquier otro prefijo: rechazado.
- `verified_at` con fecha ISO; `max_output_tokens` ≤ lo que admite el modelo; `price_changes` ordenados por fecha; `long_context` con precios > 0.

**Cambiar un modelo o un precio**
1. Toma el dato de la página oficial de precios o modelos del proveedor (no de la tabla de LiteLLM ni de blogs).
2. Cambia la entrada, pon `verified_at` = fecha de hoy y `source` = la página oficial. Un cambio de precio ya anunciado va en `price_changes` con su `effective_from`.
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

- El usuario no elige modelos en F1b; elige **la clave que usan los agentes** (`settings.llm.preferred_provider`). Si no eligió, la primera con clave en el orden Anthropic, OpenAI, Gemini (`routing.choose_provider`). El proveedor se fija en la tarea al encolarla (`agent_runs.provider`) y es el que pide la concesión; el preferido sin clave da `llm.no_key`, nunca otro.
- **Sin cambio automático de proveedor** si uno falla (decisión del usuario, spec §11-4): la llamada falla con su `llm.*`.
- Un `task_kind` nuevo se añade aquí, en `routing.py` y en su prueba.

## 5. Orden de comprobaciones de una llamada

`LlmService.call(run, step, request, *, deadline=None)` hace siempre, en este orden (la cabecera de `service.py` es la referencia). Cada rechazo de los pasos 1–5 ocurre **sin pedir la clave** (hay prueba de cada uno):

0. Un paso de tipo `approval` (el nodo que propone) se rechaza con `ValueError` (condición T8-C2). `max_output_tokens` mayor que el del modelo → `ValueError`.
1. **Pausa**: sin un `agents_control` válido o con los agentes pausados → `agents.paused`.
2. **Clave disponible**: el proveedor de la tarea (o, si no tiene, el preferido o el primero con clave) está en `llm_providers` del último `agents_control` → si no, `llm.no_key`.
3. **Máximo de la llamada** = tokens de entrada máximos × precio de entrada + `max_output_tokens` × precio de salida (`pricing.max_call_cost`). Tokens de entrada máximos = el mayor entre `ceil(caracteres / 3)` y `request.input_tokens_bound` (`pricing.max_input_tokens`); sin cota, `request.input_bytes` (bytes UTF-8 de los mensajes más el JSON del `response_format` y de las `tools`), y `LlmRequest` rechaza una cota por debajo de `input_bytes` (revisión de T14, B1 y B2). Un agente con datos externos pasa siempre la cota de su estimado (un token por byte UTF-8 del prompt más grande más la sobrecarga de la salida estructurada): con chino o emojis `caracteres / 3` se queda corto (condición T9-C1, hecha en el cierre de F1b).
4. **Presupuesto de la tarea** (leído de la base): `cost_micros + máximo ≤ max_cost_micros` y `tokens + tokens máximos ≤ token_budget` → si no, `agent.budget_exhausted`.
5. **Tope diario de la clave**: `gastado hoy + reservado + máximo ≤ límite` → si no, `llm.daily_limit_reached`. Si cabe, **reserva** el máximo (`DailyLimiter`).
6. `client.prepare()` y, si `requires_key`, la **clave** por `secret_request` dentro de la concesión de la ejecución (ADR 0014): una petición por llamada lógica (§9).
7. **Intentos y reintentos** (§6). Antes de cada reintento se vuelven a comprobar la pausa, el presupuesto y el tope (la reserva sigue tomada).
8. **Registro de cada intento** en una transacción (`usage.record_attempt`): paso (`attempts`, tokens, costo, `provider`, `model`, `tier`, `secret_ref`, `prompt_id`, `prompt_version`), acumulados de `agent_runs` y `credential_usage` del **día local en que se registra**. Costo real = tokens informados × precios del catálogo. Un intento sin respuesta que el proveedor pudo procesar (tiempo agotado, red) o una respuesta sin `usage` cuentan su **máximo** con `cost_estimated = 1`. Al final se suelta la reserva.
9. **Cancelada** (cierre del motor, `CancelledError`): el paso queda `cost_estimated = 1` con el máximo como gastado **antes** de soltar la reserva (`finish_despite_cancel`: sigue esperando el registro aunque cancelen otra vez; condición T6-C1).
10. **Salida estructurada inválida** (JSON que no valida con `output_schema`, tras quitar una cerca de Markdown con `_strip_fence`): un reintento con el mismo prompt (cuenta en presupuesto y tope); si vuelve a fallar, `llm.bad_output`. Un `finish_reason` de filtro (`content_filter`, `safety`, `refusal`) → `llm.content_blocked`.

Ejemplo de uso desde un nodo de agente (skill `agentes-langgraph`):

```python
# agents/site_summary/graph.py (real). El nodo lo envuelve StepRecorder; `n` es su NodeContext.
async def classify_content(state: SiteSummaryState, n: NodeContext) -> dict[str, Any]:
    items = items_block(state)
    prompt = n.render(
        CLASSIFY, PROMPT_VERSION,
        instructions_vars={"n_items": len(items.items)},
        data=[site_block(state), items],
    )
    result = await n.call_llm(prompt, input_tokens_bound=classify_input_bound())
    return {"classification": parsed_output(result)}   # dict JSON, nunca el objeto Pydantic
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
- Constantes de `service.py`: `MAX_ATTEMPTS = 3`, `RETRY_DELAYS_SECONDS = (2.0, 6.0)`, `RETRY_JITTER = 0.2`, `MAX_RETRY_AFTER_SECONDS = 20.0`, `BAD_OUTPUT_RETRIES = 1`.
- Antes de cada reintento se vuelven a comprobar la pausa, el presupuesto y el tope. Un intento sin respuesta que pudo consumir tokens (`MAY_HAVE_CONSUMED`: tiempo agotado y red) cuenta su máximo; un 429, un 5xx o un 4xx llegan antes de generar y no se cobran. La clave **no** se vuelve a pedir entre intentos de la misma llamada lógica.
- Una redirección no se sigue ni se reintenta (`llm.provider_error`).
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
| 401, clave inválida o revocada; 403 / permiso; clave con bytes fuera de `0x21`–`0x7E` (se rechaza sin enviar nada) | `llm.invalid_key` |
| Sin saldo, cuota agotada (402, códigos de cuota, `FAILED_PRECONDITION` de Gemini, frases fijas como "credit balance") | `llm.insufficient_quota` |
| 429 tras los reintentos | `llm.rate_limited` |
| 5xx tras los reintentos (incluido el 529 de Anthropic); 400, 404, redirección, adaptador que no se pudo cargar, excepción desconocida | `llm.provider_error` |
| Red, DNS o TLS | `llm.unreachable` |
| Tiempo agotado | `llm.timeout` |
| Bloqueado por políticas del proveedor | `llm.content_blocked` |
| Salida estructurada inválida dos veces | `llm.bad_output` |
| Sin clave del proveedor elegido | `llm.no_key` |
| Tope diario | `llm.daily_limit_reached` |
| Proveedor fuera de la lista (rutas) | `llm.invalid_provider` |
| Límite fuera de rango (rutas) | `llm.invalid_limit` |

- `classify_exception` lee la excepción por atributos (clase, `status_code`, cuerpo ya parseado) sin importar LiteLLM: solo códigos con forma de identificador (`[a-z0-9_]{1,64}`) y unas pocas frases fijas, que nunca salen de la función. Devuelve `LlmCallError(kind)` con un `kind` cerrado (`invalid_key`, `permission`, `insufficient_quota`, `rate_limited`, `server_error`, `unreachable`, `timeout`, `content_blocked`, `bad_request`, `redirect`, `adapter_unavailable`, `unknown`) y el `Retry-After` si lo hay; `KIND_CODES` lo traduce a `llm.*`. Cada fila tiene su prueba con `respx`.
- Cualquier excepción no reconocida → `llm.provider_error` y una línea de log con solo el nombre de la clase.
- Los mensajes para el usuario están en `locales/es/errors.json` (spec §5.5) con `{{provider}}`.

## 9. La clave en memoria

- La clave solo llega por la **concesión de la ejecución** (ADR 0014): una petición `get` de `llm/<proveedor>/default` por llamada lógica. Ninguna operación de `engine-operations.json` declara `llm/*` en F1b.
- `SecretValue` (de `core/secrets.py`) se usa con `with` y se sobrescribe al salir. LiteLLM exige `str`: la copia se crea **dentro** del `with`, se pasa al adaptador y se suelta (`del`) en el `finally`. Python no garantiza borrar esa copia (riesgo aceptado, spec §7); no la guardes en ningún objeto que viva más que la llamada.
- Tras cada llamada se vacía la caché de clientes HTTP de LiteLLM (resuelto en T2 con 1.104.0): es `litellm.in_memory_llm_clients_cache` (`LLMClientCache`, TTL 3600 s). Con **OpenAI** guarda un `AsyncOpenAI` que lleva la clave, y la clave de la caché contiene la clave **en claro** (además de su SHA-256); con Anthropic y Gemini guarda un `AsyncHTTPHandler` sin clave. Se vacía con `await close_litellm_async_clients()` + `litellm.in_memory_llm_clients_cache.flush_cache()`. Tras vaciar y terminar las tareas de registro de LiteLLM en segundo plano no queda en memoria ningún diccionario con la clave (prueba en `tests/llm/test_litellm_isolation.py`). El vaciado es global: con un solo trabajador (spec §4.3) no hay llamadas concurrentes; si algún día las hay, hazlo bajo un candado.
- La clave nunca va a: SQLite (tampoco checkpoints ni `agent_steps`: solo `secret_ref`), prompts, eventos `agent_activity`, logs, excepciones, `details` de errores ni la interfaz.

```python
# llm/service.py (real, dentro de LlmService.call, tras reservar el máximo)
await self.client.prepare()                      # si el adaptador no carga, no se pide la clave
if not self.client.requires_key:                 # FakeLLM (--fake-llm)
    return await self._run(call, None)
with await self.secrets.get(secret_ref_for(provider), max_wait=max_wait) as secret:
    api_key = self._decode(secret, provider)     # copia str inevitable; solo 0x21–0x7E o llm.invalid_key
    try:
        return await self._run(call, api_key)
    finally:
        del api_key
```

`vault.secret_not_allowed` a mitad de una tarea significa que la concesión caducó o se revocó (pausa): lo gestiona el trabajador (`core/jobs/worker.py`), no la capa de IA.

## 10. Pruebas

- **`FakeLLM`** (`llm/fake.py`; `tests/fakes/llm.py` lo reexporta con `FakeReply`, `FakeFailure`, `FakeSecrets` y `make_request`): implementa `LlmClient` con `requires_key = False`; responde por `prompt_id` (`FakeReply`), cuenta tokens con `count_tokens` (caracteres / 4 hacia arriba, siempre por debajo del máximo reservado) y simula cada fallo de §8 (`FakeFailure`), salidas inválidas, llamadas a herramientas y respuestas "que obedecen" a una inyección (skill `prompts-y-evals`). Un `prompt_id` sin respuesta devuelve `DEFAULT_TEXT` (con esquema, termina en `llm.bad_output`).
- **Adaptador** (`tests/llm/`) contra un servidor falso para los tres proveedores (forma de la petición, `api_key` en la cabecera correcta —`authorization`, `x-api-key`, `x-goog-api-key`—, `api_base` fijo al host oficial aunque el entorno o un `.env` en un directorio padre traigan `OPENAI_BASE_URL` y compañía apuntando a una trampa en loopback —que no recibe nada—, tokens leídos de la respuesta, cada error mapeado). Con el cliente propio (transporte de httpx, sin aiohttp) `respx` intercepta las llamadas (`test_litellm_adapter.py`, `test_errors.py`); la sonda `adapter_probe.py` lo repite en un proceso aparte con la red bloqueada y comprueba que no se cargan `litellm.proxy.*`.
- **Sin red**: el gancho de auditoría de `tests/network_guard.py` bloquea toda conexión y DNS fuera de loopback en el proceso de pytest y en sus hijos (skill `pruebas-faro`). `tests/llm/test_litellm_isolation.py` llama a un servidor falso desde un proceso con la red bloqueada: no sale nada a internet y la caché queda vacía (criterio 4).
- **Clave falsa** con forma evidente (`sk-ant-test-…`, `AIzaTEST…`): no aparece en logs capturados, `agent_steps`, checkpoints, respuestas ni excepciones.
- **Obligatorias** (spec §9.2): costo con enteros y redondeo; selección de proveedor y nivel; cada rechazo de §5 con su código y sin pedir la clave; reservas concurrentes no pasan el tope; cambio de día local; reintentos y no-reintentos de §6; `Retry-After`; salida inválida → un reintento → `llm.bad_output`; llamada cancelada → `cost_estimated` con el máximo; catálogo inválido (modelo repetido, precio 0, Vertex) → no arranca; ningún otro módulo importa `litellm`.
- Cobertura 95 % (`strict_modules`) en `pricing.py`, `limits.py`, `service.py`, `errors.py`, `catalog.py`, `hardening.py` y `litellm_client.py`. Pendiente de añadir (spec F1b §12): `client.py`, `fake.py`, `routing.py`, `settings.py`, `usage.py`.
- **Con clave real (manual, nunca en CI):** `scripts/manual_llm_models_check.py` (los seis modelos del catálogo) y `scripts/manual_site_summary_tokens.py` (tokens reales de cada llamada de `site_summary` frente a lo reservado; pide la clave con `getpass`, nada en archivos ni en pantalla). Los usa la lista manual de F1b (mediciones M1, M2 y M6).

### Modo `--fake-llm` (solo desarrollo)

- El motor lo acepta solo sin `sys.frozen` (si no, sale con código 2). El núcleo lo pasa solo en build de depuración **y** con `FARO_FAKE_LLM=1` exacto (entorno o `.env.local`), igual que `--allow-local-sites`.
- Usa `FakeLLM` con `fake_catalog()` (modelo `fake` en los dos niveles de los tres proveedores), las respuestas de cada agente (`AgentSpec.dev_replies` → `registry.dev_fake_replies` → `create_app`; un `prompt_id` sin respuesta usa `DEFAULT_TEXT`) y un **precio de prueba alto** para poder llegar al tope diario con pocas tareas, pero con el "como mucho" de cada agente **por debajo del tope predeterminado** (US$5): si no, el estimado bloquea siempre y no se puede lanzar nada (hallazgo §6.3 de T12; desde T12, US$25/US$125 por Mtok y `site_summary` con un máximo de US$2,29). `tests/agents/site_summary/test_fake_mode.py` lo comprueba; un agente nuevo añade la misma prueba.
- No pide ninguna clave por `secret_request`, pero **sí** pide la concesión de la ejecución (el flujo de concesiones se ejercita igual).
- Marca cada paso con `model = "fake"`.

## Lista de verificación

- [ ] Ningún módulo fuera de `llm/litellm_client.py` importa `litellm`.
- [ ] La configuración endurecida se aplica al importar y el motor no arranca si falla.
- [ ] `LITELLM_MODE=PRODUCTION`; entorno limpiado y comprobado antes **y** después de `import litellm` (incluidos proxies, `AIOHTTP_*` y `SSL_*`).
- [ ] Directorio de trabajo fuera de `sys.path` tras el import; el motor arranca con un directorio de trabajo fijo y no escribible.
- [ ] Cliente propio en cada llamada (`follow_redirects=False`, `trust_env=False`, `verify=tls_context()`), con prueba en loopback de 307 a otro puerto y de proxy del entorno.
- [ ] Solo `acompletion`; nada de `completion` síncrono, `litellm.ssl_verify`, `aclient_session` ni clientes de LiteLLM. `install_system_trust_for_libraries()` antes del import. Prueba que espía `wrap_bio`/`wrap_socket`/`do_handshake`: solo `tls_context()` o `_LockedContext`, intactos tras cada llamada, ningún contexto de `_ssl_context_cache` y ningún `wrap_socket` en el bucle.
- [ ] `api_base` fijo al host oficial del catálogo y `api_key` explícita; ningún `base_url`/cabecera extra; modelos solo del catálogo; Gemini solo `gemini/`.
- [ ] Orden de §5 respetado; los rechazos 1–5 no piden la clave.
- [ ] Costos en micros enteros con redondeo hacia arriba; nada de `float`.
- [ ] Reservas bajo candado; prueba de concurrencia en verde.
- [ ] Errores `llm.*` con `details.provider` y sin cuerpo del proveedor.
- [ ] La clave no aparece en logs, base, checkpoints, eventos ni respuestas (prueba de captura).
- [ ] Cambios de `models.json` con fuente oficial, `verified_at` y efecto en los estimados.
- [ ] `--fake-llm` rechazado con `sys.frozen`.
