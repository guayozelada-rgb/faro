---
name: herramientas-de-agente
description: Herramientas tipadas para los agentes de Faro - Tool[In, Out] con Pydantic (extra="forbid", entrada plana con longitudes y rangos), alcance tomado del RunContext y nunca del modelo (SCOPE_FIELDS), clases de efecto (solo read es herramienta; internal/publish/spend van por actions.py), ToolRunner con límites de tiempo, llamadas y tamaño, red por faro_engine/net, errores tool.*, salidas externas como UntrustedText y plantillas de herramienta y de prueba. Úsala al crear o cambiar una herramienta de agente o al ofrecer herramientas a un modelo.
---

# Herramientas de agente (`agents/framework/tools.py`)

> Describe el **código real** desde el cierre de F1b (T14, 2026-10-20). Fuentes: spec `docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md` §4.2 y §4.4, ADR 0015 §5, ADR 0016 §1; cabeceras de `agents/framework/tools.py` y `agents/site_summary/tools.py`. En F1b la única herramienta de producción es `read_site_content`, y la llama un nodo: **ningún agente real ofrece herramientas al modelo** (el adaptador de LiteLLM las rechaza; solo el agente de pruebas las usa, con `FakeLLM`). Si cambias el marco, actualiza esta skill en el mismo PR.

Dueño: `ingeniero-ia`. Skills relacionadas: `agentes-langgraph` (dónde se llaman), `capa-llm` (cómo se ofrecen al modelo), `prompts-y-evals` (cómo vuelve su salida a un prompt).

Una **herramienta** es una función de **lectura** con entrada y salida tipadas que un nodo del agente llama directamente o que el modelo puede pedir. Lo que guarda, publica o gasta **no** es una herramienta: es una **acción** (`ActionSpec`, skill `agentes-langgraph` §8).

## 1. `Tool[In, Out]`

```python
# agents/framework/tools.py (código real, genéricos PEP 695)
class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

@dataclass(frozen=True, slots=True)
class Tool[In: ToolInput, Out: ToolOutput]:
    name: str                         # ^[a-z][a-z0-9_]{1,47}$, estable (sale en agent_steps y logs)
    description: str                  # 1–1024 caracteres; para el modelo; sin datos del usuario
    input_model: type[In]
    output_model: type[Out]
    effect: Literal["read"]           # la única clase que existe
    timeout_seconds: float            # 0 < x ≤ 120 (MAX_TIMEOUT_SECONDS)
    max_calls_per_run: int            # 1–50 por tarea (MAX_CALLS_PER_RUN)
    max_output_chars: int             # 1–100 000 al volver a un prompt (MAX_OUTPUT_CHARS)
    fn: Callable[[RunContext, In], Awaitable[Out]]
    to_items: Callable[[Out], list[Item]] | None = None   # cómo se convierte en elementos de un DataBlock
    source: Source = "api"            # origen de sus textos ("site", "api", …)
```

`check_tool` aplica las reglas al registrar el agente (`AgentSpec.validate`) y antes de cada uso; si fallan, `ToolRegistrationError` y el motor no arranca:
- nombre válido, `effect == "read"`, descripción de 1 a 1024 caracteres, `In`/`Out` heredan de `ToolInput`/`ToolOutput`, `In` con `extra="forbid"`, límites dentro de rango;
- **entrada plana**: sin `$defs` ni modelos anidados; cada campo es un valor simple o una lista (un `object` se rechaza);
- **todo campo acotado** en el esquema JSON: cadenas con `maxLength` (o `enum`/`const`), listas con `maxItems`, enteros y números con `maximum` (o `enum`/`const`); las listas declaran sus elementos (`items` o `prefixItems`, que genera `tuple[...]`) y cada elemento se comprueba igual. Enumeraciones con `Literal`;
- **ningún campo de alcance** (`SCOPE_FIELDS`, comparado en minúsculas, igual o como sufijo `_<campo>`, p. ej. `target_site_id`): `site_id`, `site`, `run_id`, `agent_kind`, `provider`, `secret_ref`, `secret`, `api_key`, `token`, `url`, `uri`, `api_root`, `host`, `path`, `file`, `filename`.

## 2. El alcance sale del contexto

La función recibe `(ctx: RunContext, args: In)`. Sitio, tarea, proveedor y límites salen **solo** de `ctx` (skill `agentes-langgraph` §2). Aunque una inyección convenza al modelo de pedir "lee el sitio X" o "usa el token de Y", no hay argumento que lo permita, y la concesión de la ejecución (ADR 0014) tampoco daría ese secreto.

```python
# Bien (agents/site_summary/tools.py): el sitio es el de la tarea.
async def _read(ctx: RunContext, args: ReadSiteContentIn) -> ReadSiteContentOut:
    site_id = require_site(ctx)       # agent.site_required si la tarea no tiene sitio
    ...

# Mal: el modelo elige el sitio.
class BadIn(ToolInput):
    site_id: str                      # check_tool lo rechaza al registrar
```

Las dependencias (lector de sitios, clientes) entran **por cierre** al construir la herramienta para cada ejecución (`make_read_site_content(reader)`); la función solo recibe `(ctx, args)`.

## 3. Clases de efecto y cómo se llaman

| Clase | ¿Herramienta? | ¿Se ofrece al modelo? | Camino |
| --- | --- | --- | --- |
| `read` | Sí | Solo si el agente lo decide (ninguno real en F1b) | `n.tools.invoke` o `n.run_tool_calls` |
| `internal` | **No** | **Nunca** | `ActionSpec` tras `n.propose` (autonomía) |
| `publish` | **No** | **Nunca** | `ActionSpec` tras `n.propose` y aprobación |
| `spend` | **No** | **Nunca** | `ActionSpec` tras `n.propose` y aprobación |

- **Llamada desde un nodo** (lo normal): `out = await n.tools.invoke(n.ctx, tool, {"kinds": [...], "per_kind": 50})`. Así lo hace `read_site` en `site_summary`: el modelo no decide qué leer.
- **Ofrecida al modelo**: `n.call_llm(prompt, tools=(tool,))` pasa `tool_spec(tool)` (nombre, descripción y `input_model.model_json_schema()`) en `LlmRequest.tools`; las llamadas que devuelve el modelo (`LlmResult.tool_calls`) se ejecutan con `n.run_tool_calls(result, tools)` (`ToolRunner.invoke_call`: una herramienta no ofrecida → `tool.invalid_arguments`), nunca a mano. **En F1b el adaptador de LiteLLM rechaza cualquier petición con `tools`** sin enviar nada (`llm.tools_not_supported` en el log): con ellas LiteLLM 1.104 crea su propio cliente para `gpt-6*`. Habilitarlas exige comprobar el camino de cada proveedor (pendiente de `ingeniero-ia`, F2; skill `capa-llm`).
- El `ToolRunner` de un nodo `approval` está desactivado (`enabled=False`): `NodeKindError` (condición T8-C2, el nodo que propone no gasta).
- La salida vuelve a un prompt con `result_block(tool, output)`: un `DataBlock` de `source=tool.source`, acotado a `max_output_chars` con marca de recorte; nunca entra en las instrucciones.
- No existe herramienta que borre. Si algo "lee" pero cambia estado en el servicio remoto (marca como leído, consume cuota de pago), no es `read`: trátalo como acción. Actualizar en la base de Faro los conteos que devuelve el sitio (como hace `checkSiteConnection`) se acepta.

## 4. `ToolRunner.invoke` y límites

Orden real de `ToolRunner.invoke(ctx, tool, raw_args)` (condición T6-C4):

1. Runner desactivado → `NodeKindError`.
2. **Llamadas por tarea**: si ya llegó a `max_calls_per_run` → `tool.limit_reached`. El contador vive en el estado (`AgentState.tool_calls`, lo guarda `StepRecorder` al terminar el nodo): sobrevive a la reanudación; las lecturas de un nodo interrumpido se repiten (costo aceptado).
3. **Validación** con `In`: un campo de más (también uno de alcance) o un valor fuera de rango → `tool.invalid_arguments`, **sin ejecutar nada**; la llamada cuenta. El log (`agents.tool_invalid_arguments`) lleva solo el nombre de la herramienta y los nombres **canónicos** de `SCOPE_FIELDS` que coincidieron: nunca los valores ni las claves que mandó el modelo (hallazgo 1 de la revisión de T8).
4. **Ejecución** con `asyncio.timeout(timeout_seconds)` → `tool.timeout`.
5. Un `FaroError` del dominio (p. ej. `site.revoked`) sube tal cual; otra excepción → `internal.unexpected` (solo la clase en el log); una salida que no es `Out` → `internal.unexpected`.

| Límite | Dónde | Al pasarlo |
| --- | --- | --- |
| Tiempo por llamada | `ToolRunner` | `tool.timeout` |
| Llamadas por tarea | `AgentState.tool_calls` | `tool.limit_reached` |
| Tamaño al volver a un prompt | `result_block` / `DataBlock` | se recorta con marca; nunca se pasa entero |
| Plazo de la red | `Deadline` de `faro_engine/net/client.py` dentro del servicio | error del dominio (`site.unreachable`…) |

- **Red**: toda petición a una URL que no sea fija en el código va por `faro_engine/net` (SSRF, IP fijada, plazos, tamaño; ADR 0012). Los sitios WordPress, por `faro_engine/wordpress` a través de `SitesService` (p. ej. `sites/reader.py`: `SiteReader.snapshot`). Nunca `httpx` directo en una herramienta.
- **Secretos**: solo los de la concesión de la ejecución, pedidos por el servicio que usa la herramienta con `with await secrets.get(ref, max_wait=…) as secret:` y soltados al terminar (skill `llavero-y-cifrado`). Nunca en la salida de la herramienta.
- Las herramientas no guardan resultados: lo que el agente quiera conservar va al estado del grafo o a una acción.

## 5. Errores tipados

- Códigos del marco: `tool.invalid_arguments`, `tool.timeout`, `tool.limit_reached` (en `locales/es/errors.json`). Del dominio: `site.*` del cliente de WordPress (`site.revoked`, `site.unreachable`, …) y `vault.*` del canal de secretos. Un código nuevo se añade al catálogo y a `locales/es/errors.json` en el mismo PR.
- Nunca pongas en `message` ni en `details` el contenido leído, la URL completa con parámetros, cabeceras ni secretos.
- Cómo llega a la tarea: el error sube al nodo, `StepRecorder` cierra el paso `failed` con el `error_code` y la tarea termina `failed` con ese código (skill `agentes-langgraph` §9). El nodo solo captura un error si tiene un plan concreto.

## 6. Salidas de fuera = `UntrustedText`

- Todo texto que viene de fuera se convierte en `UntrustedText(source, value, max_chars)` **dentro** de la herramienta, antes de devolverlo (normalización: NFC, sin controles, formato `Cf` ni marcas bidireccionales, `<`/`>` → `‹`/`›`, longitud máxima; skill `prompts-y-evals` §3).
- En `Out`, esos campos son `UntrustedText`, nunca `str`. Identificadores, números y enumeraciones que valida la propia herramienta pueden ser tipos normales.
- Para guardarla en el estado del grafo (solo JSON) se guarda `.value` y se vuelve a envolver como `UntrustedText` al armar el prompt (así lo hace `site_summary`).

## 7. Plantillas

### Herramienta (código real: `agents/site_summary/tools.py`)

```python
TOOL_NAME: Final = "read_site_content"
TIMEOUT_SECONDS: Final = 40.0
MAX_CALLS_PER_RUN: Final = 3
URL_MAX_CHARS: Final = 2_048
MAX_OUTPUT_CHARS: Final = 60_000
Kind = Literal["page", "post", "product"]

class ReadSiteContentIn(ToolInput):
    kinds: tuple[Kind, ...] = Field(default=("page", "post", "product"), min_length=1, max_length=3)
    per_kind: int = Field(default=PER_KIND_LIMIT, ge=1, le=PER_KIND_LIMIT)        # 50

class SiteItem(ToolOutput):
    kind: Kind
    title: UntrustedText
    url: UntrustedText

class ReadSiteContentOut(ToolOutput):
    site_name: UntrustedText
    counts: SiteCounts                     # pages, posts, products | None
    woocommerce_active: bool
    items: tuple[SiteItem, ...] = Field(max_length=MAX_ITEMS)                    # 150

def make_read_site_content(reader: SiteReader | None) -> Tool[ReadSiteContentIn, ReadSiteContentOut]:
    """`None` solo al validar el registro; el grafo la construye con g.services.sites."""

    async def _read(ctx: RunContext, args: ReadSiteContentIn) -> ReadSiteContentOut:
        site_id = require_site(ctx)
        if reader is None:
            raise FaroError.of(INTERNAL_UNEXPECTED, 500)
        snapshot = await reader.snapshot(site_id, args.kinds, args.per_kind)   # pide wp/<site_id>/token
        items = tuple(
            SiteItem(kind=item.kind,
                     title=UntrustedText("site", item.title, TITLE_MAX_CHARS),
                     url=UntrustedText("site", item.url, URL_MAX_CHARS))
            for item in snapshot.items if item.kind in args.kinds
        )
        return ReadSiteContentOut(
            site_name=UntrustedText("site", snapshot.site_name, SITE_NAME_MAX_CHARS),
            counts=SiteCounts(pages=snapshot.pages, posts=snapshot.posts, products=snapshot.products),
            woocommerce_active=snapshot.woocommerce_active,
            items=items[:MAX_ITEMS],
        )

    return Tool(name=TOOL_NAME, description="Lee el nombre, los conteos y los títulos y direcciones …",
                input_model=ReadSiteContentIn, output_model=ReadSiteContentOut, effect="read",
                timeout_seconds=TIMEOUT_SECONDS, max_calls_per_run=MAX_CALLS_PER_RUN,
                max_output_chars=MAX_OUTPUT_CHARS, fn=_read, source="site")
```

### Pruebas

Modelos: `tests/agents/test_tools.py` (marco, con una herramienta de prueba) y `tests/agents/site_summary/test_units.py` / `test_flow.py` (`read_site_content` con un `SiteReader` doble, sin red). Casos de inyección relacionados: `tests/fixtures/injection/12-url-interna.json` y `13-herramienta-otro-sitio.json`.

```python
# Forma de las pruebas del marco (tests/agents/test_tools.py)
async def test_el_alcance_sale_del_contexto_no_del_modelo() -> None:
    raw = {"kinds": ["page"], "site_id": OTHER_SITE}
    with pytest.raises(FaroError) as err:
        await runner.invoke(ctx, tool, raw)
    assert err.value.code == "tool.invalid_arguments"
    assert reader.calls == []                          # no se tocó ningún sitio

async def test_limite_de_llamadas_por_tarea() -> None:
    for _ in range(tool.max_calls_per_run):
        await runner.invoke(ctx, tool, {"kinds": ["page"]})
    with pytest.raises(FaroError) as err:
        await runner.invoke(ctx, tool, {"kinds": ["page"]})
    assert err.value.code == "tool.limit_reached"
```

Pruebas obligatorias de toda herramienta: reglas de registro (`check_tool`), `extra` prohibido (incluido un campo de alcance, y el log sin claves ni valores del modelo), longitudes y rangos de `In`, límite de llamadas, tiempo agotado (con evento, sin `sleep`), errores del dominio con su código, excepción inesperada y salida de otro tipo, salida externa como `UntrustedText` normalizada, `result_block` acotado, ninguna conexión real (`network_guard` del motor; dobles de `SiteReader`, `sites` o `wordpress`, o `respx`), ningún secreto en la salida ni en los logs capturados.

## Lista de verificación

- [ ] `effect = "read"` y no cambia nada en el servicio remoto.
- [ ] `In` con `extra="forbid"`, plano, con longitudes y rangos; sin campos de alcance (`SCOPE_FIELDS`, también como sufijo).
- [ ] El alcance sale de `RunContext`; dependencias por cierre.
- [ ] Red por `faro_engine/net` o `faro_engine/wordpress`; secretos solo de la concesión y soltados al terminar.
- [ ] Texto externo como `UntrustedText`; salida acotada (`max_output_chars`).
- [ ] Errores con código del catálogo, sin contenido ni secretos en `message`/`details`.
- [ ] Si se ofrece al modelo: antes, habilitar `tools` en el adaptador de LiteLLM con la comprobación de cada proveedor (hoy se rechaza).
- [ ] Pruebas obligatorias en verde; cambio revisado por `revisor-seguridad` si la herramienta lee secretos o se ofrece al modelo.
