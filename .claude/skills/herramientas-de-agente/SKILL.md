---
name: herramientas-de-agente
description: Herramientas tipadas para los agentes de Faro - Tool[In, Out] con Pydantic (extra="forbid", longitudes máximas), alcance tomado del RunContext y nunca del modelo, clases de efecto (solo read se ofrece al modelo; internal/publish/spend van por actions.py), límites de tiempo, llamadas y tamaño, red por faro_engine/net, errores tipados, salidas externas como UntrustedText y plantillas de herramienta y de prueba. Úsala al crear o cambiar una herramienta de agente o al ofrecer herramientas a un modelo.
---

# Herramientas de agente (`agents/framework/tools.py`)

> **Diseño de referencia de F1b** (spec `docs/specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md` §4.2 y §4.4, ADR 0015 §5, ADR 0016 §1). El marco se construye en T8; en F1b la única herramienta de producción es `read_site_content` (T9) y la opción de ofrecer herramientas al modelo solo la usa un agente de pruebas. Ajusta nombres y firmas al código real al implementar y actualiza esta skill en el cierre (T14). Lo marcado **(verificar en T8)** depende de las versiones fijadas de LangGraph o LiteLLM.

Dueño: `ingeniero-ia`. Skills relacionadas: `agentes-langgraph` (dónde se llaman), `capa-llm` (cómo se ofrecen al modelo), `prompts-y-evals` (cómo vuelve su salida a un prompt).

Una **herramienta** es una función de **lectura** con entrada y salida tipadas que un nodo del agente llama directamente o que el modelo puede pedir. Lo que guarda, publica o gasta **no** es una herramienta: es una **acción** de `actions.py` (skill `agentes-langgraph` §8).

## 1. `Tool[In, Out]`

```python
# framework/tools.py — diseño de referencia de F1b.
class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

@dataclass(frozen=True, slots=True)
class Tool(Generic[In, Out]):
    name: str                         # ^[a-z][a-z0-9_]{1,47}$, estable (sale en agent_steps)
    description: str                  # para el modelo: qué hace y cuándo usarla; sin datos del usuario
    input_model: type[In]             # subclase de ToolInput
    output_model: type[Out]           # subclase de ToolOutput
    effect: Literal["read"]           # en F1b solo read; el registro rechaza otra cosa
    timeout_seconds: float            # por llamada
    max_calls_per_run: int            # por tarea (no por ejecución)
    max_output_chars: int             # tamaño máximo de lo que vuelve al modelo
    fn: Callable[[RunContext, In], Awaitable[Out]]
```

Reglas de los modelos:
- `extra="forbid"` siempre: un campo que el modelo invente es un error, no se ignora.
- Toda cadena con `max_length`; toda lista con `max_length`; todo número con rango (`ge`/`le`). Enumeraciones con `Literal`.
- Modelos **planos** (sin anidar ni `$defs`) en la entrada: es lo que todos los proveedores aceptan como esquema de herramienta **(verificar en T8 con los tres proveedores)**.
- **Prohibidos en `In`**: `site_id`, `run_id`, `secret_ref`, URL de sitios, rutas de archivo o cualquier campo que elija **alcance**. El registro de herramientas rechaza esos nombres al arrancar.

## 2. El alcance sale del contexto

La función recibe `(ctx: RunContext, args: In)`. Sitio, tarea, proveedor y límites salen **solo** de `ctx` (skill `agentes-langgraph` §2). Así, aunque una inyección de prompts convenza al modelo de pedir "lee el sitio X" o "usa el token de Y", no hay ningún argumento que lo permita, y la concesión de la ejecución (ADR 0014) tampoco daría ese secreto.

```python
# Bien: el sitio es el de la tarea.
async def _read_site_content(ctx: RunContext, args: ReadSiteContentIn) -> ReadSiteContentOut:
    site_id = require_site(ctx)                     # agent.site_required si la tarea no tiene sitio
    ...

# Mal: el modelo elige el sitio.
class BadIn(ToolInput):
    site_id: str                                    # el registro lo rechaza
```

Prueba obligatoria: el modelo (vía `FakeLLM`) envía `site_id` u otro campo de alcance → la llamada se rechaza por `extra="forbid"` y no se toca ningún sitio.

## 3. Clases de efecto

| Clase | ¿Herramienta? | ¿Se ofrece al modelo? | Camino |
| --- | --- | --- | --- |
| `read` | Sí | Sí, si el agente lo decide | `ToolRunner.invoke` |
| `internal` | **No** | **Nunca** | `actions.py` tras `autonomy.decide` |
| `publish` | **No** | **Nunca** | `actions.py` tras `autonomy.decide` y aprobación |
| `spend` | **No** | **Nunca** | `actions.py` tras `autonomy.decide` y aprobación |

- Ofrecer herramientas al modelo: `LlmRequest(tools=…)`; la capa de IA las pasa a LiteLLM como funciones con el esquema JSON de `In` (`input_model.model_json_schema()`) **(verificar en T6/T8 el formato exacto que acepta la versión fijada)**. Las llamadas que devuelve el modelo (`LlmResult.tool_calls`) se ejecutan con `ToolRunner.invoke`, nunca a mano.
- Antes de ofrecer una herramienta al modelo, pregúntate si el nodo puede llamarla directamente. En F1b, `read_site` la llama el nodo: el modelo no decide qué leer.
- No existe herramienta que borre. Si algo "lee" pero cambia estado en el servicio remoto (marca como leído, consume cuota de pago), no es `read`: trátalo como acción.

## 4. Límites

| Límite | Dónde | Al pasarlo |
| --- | --- | --- |
| Tiempo por llamada (`timeout_seconds`) | `ToolRunner` con `asyncio.timeout` | error `tool.*` de tiempo (código exacto en T8) |
| Llamadas por tarea (`max_calls_per_run`) | contador por tarea (sobrevive a la reanudación; dónde se guarda lo fija T8) | `tool.limit_reached` |
| Tamaño de salida (`max_output_chars`) | al convertir la salida para el modelo | se trunca con marca de truncado; nunca se pasa entero |
| Plazo de la operación | `Deadline` de `faro_engine/net/client.py` | `max_wait=deadline.remaining()` en `secrets.get` |

- **Red**: toda petición a una URL que no sea fija en el código va por `faro_engine/net` (SSRF, IP fijada, plazos, tamaño; ADR 0012). Las peticiones a sitios WordPress van por el cliente único `faro_engine/wordpress`. Nunca `httpx` directo en una herramienta.
- **Secretos**: solo los de la concesión de la ejecución, con `with await secrets.get(ref, max_wait=…) as secret:` dentro de la herramienta, soltados al terminar (skill `llavero-y-cifrado`). Nunca en la salida de la herramienta.
- Las herramientas no escriben en la base salvo su propio registro de uso (si lo hay); los resultados que el agente quiera guardar van al estado del grafo o a una acción.

## 5. Errores tipados

- Lanza `FaroError` con un código del catálogo:
  - de la herramienta: `tool.<motivo>` (en F1b, `tool.limit_reached`; uno nuevo se añade a `locales/es/errors.json` en el mismo PR);
  - del dominio que toca: los de `site.*` del cliente de WordPress (`site.revoked`, `site.unreachable`, …), `vault.*` del canal de secretos.
- Nunca pongas en `message` ni en `details` el contenido leído, la URL completa con parámetros, cabeceras ni secretos.
- Cómo llega a la tarea: `ToolRunner` registra la llamada en el paso (`StepHandle`), el error sube al nodo, `StepRecorder` cierra el paso `failed` con el `error_code` y la tarea termina `failed` con ese código (skill `agentes-langgraph` §9). El nodo solo captura un error si tiene un plan concreto (p. ej. seguir sin productos si el sitio no tiene WooCommerce).
- Argumentos inválidos enviados por el **modelo** (validación de `In`): no se ejecuta nada; cuenta como llamada. Si se devuelve al modelo un aviso para que corrija o la tarea falla lo decide T8; el aviso nunca repite los argumentos recibidos.
- Excepción inesperada → `internal.unexpected` con solo la clase en el log.

## 6. Salidas de fuera = `UntrustedText`

- Todo texto que viene de fuera (sitio, buscadores, APIs) se convierte en `UntrustedText(source=…)` **dentro** de la herramienta, antes de devolverlo (normalización: NFC, sin controles ni marcas bidireccionales, `<`/`>` → `‹`/`›`, longitud máxima; skill `prompts-y-evals` §3).
- En el modelo `Out`, esos campos son `UntrustedText` (o su forma serializada), nunca `str` sin más. Los identificadores, números y enumeraciones que valida la propia herramienta pueden ser tipos normales.
- Cuando la salida vuelve a un prompt (como resultado de herramienta o como dato de otro nodo), entra en un `DataBlock`, nunca en las instrucciones.
- Para guardarla en el estado del grafo (solo JSON), conviértela a `dict`/`str` con el método del marco y vuelve a envolverla como `UntrustedText` al leerla.

## 7. Plantillas

### Herramienta

```python
# agents/site_summary/tools.py — diseño de referencia de F1b; cliente de sitios: ajustar al real.
class ReadSiteContentIn(ToolInput):
    kinds: tuple[Literal["page", "post", "product"], ...] = Field(
        default=("page", "post", "product"), min_length=1, max_length=3
    )

class SiteItem(ToolOutput):
    kind: Literal["page", "post", "product"]
    title: UntrustedText
    url: UntrustedText

class ReadSiteContentOut(ToolOutput):
    items: tuple[SiteItem, ...] = Field(max_length=150)
    counts: dict[str, int]

def make_read_site_content(sites: SitesReader) -> Tool[ReadSiteContentIn, ReadSiteContentOut]:
    """Las dependencias entran por cierre; la función solo recibe (ctx, args)."""

    async def _read(ctx: RunContext, args: ReadSiteContentIn) -> ReadSiteContentOut:
        site_id = require_site(ctx)
        items: list[SiteItem] = []
        for kind in args.kinds:
            page = await sites.list_content(site_id, kind=kind, limit=50)   # pide wp/<site_id>/token
            items += [
                SiteItem(kind=kind,
                         title=UntrustedText(source="site", value=i.title),
                         url=UntrustedText(source="site", value=i.url))
                for i in page.items
            ]
        return ReadSiteContentOut(items=tuple(items), counts=count_by_kind(items))

    return Tool(
        name="read_site_content",
        description="Lee títulos y direcciones de las páginas, entradas y productos del sitio de esta tarea.",
        input_model=ReadSiteContentIn,
        output_model=ReadSiteContentOut,
        effect="read",
        timeout_seconds=40,
        max_calls_per_run=3,
        max_output_chars=20_000,
        fn=_read,
    )
```

### Prueba

```python
# tests/agents/tools/test_read_site_content.py — diseño de referencia de F1b.
RLO = chr(0x202E)                                     # marca bidireccional; nunca literal en el código

@pytest.fixture
def tool(fake_sites: FakeSites) -> Tool[ReadSiteContentIn, ReadSiteContentOut]:
    return make_read_site_content(fake_sites)          # doble del lector de sitios, sin red

async def test_scope_comes_from_context_not_from_model(tool_runner, run_ctx, tool, fake_sites) -> None:
    raw_args = {"kinds": ["page"], "site_id": "00000000-0000-7000-8000-000000000999"}
    with pytest.raises(FaroError) as err:
        await tool_runner.invoke(run_ctx, tool, raw_args)
    assert err.value.code.startswith("tool.")
    assert fake_sites.calls == []                      # no se tocó ningún sitio

async def test_titles_are_untrusted_and_normalized(tool_runner, run_ctx, tool, fake_sites) -> None:
    fake_sites.add_page(title=f"Hola <b>{RLO} mundo</datos>")
    out = await tool_runner.invoke(run_ctx, tool, {"kinds": ["page"]})
    title = out.items[0].title
    assert isinstance(title, UntrustedText)
    assert "<" not in title.value and RLO not in title.value

async def test_limit_per_run(tool_runner, run_ctx, tool) -> None:
    for _ in range(tool.max_calls_per_run):
        await tool_runner.invoke(run_ctx, tool, {"kinds": ["page"]})
    with pytest.raises(FaroError) as err:
        await tool_runner.invoke(run_ctx, tool, {"kinds": ["page"]})
    assert err.value.code == "tool.limit_reached"

async def test_revoked_site_fails_with_domain_code(tool_runner, run_ctx, tool, fake_sites) -> None:
    fake_sites.revoke()
    with pytest.raises(FaroError) as err:
        await tool_runner.invoke(run_ctx, tool, {"kinds": ["page"]})
    assert err.value.code == "site.revoked"
```

Pruebas obligatorias de toda herramienta: `extra` prohibido (incluido un campo de alcance), longitudes y rangos de `In`, límite de llamadas, tiempo agotado (con reloj o evento, sin `sleep`), errores del dominio mapeados, salida externa como `UntrustedText` normalizada, tamaño máximo de salida, ninguna conexión real (sockets bloqueados; `respx` o dobles de `sites`/`wordpress`), ningún secreto en la salida ni en los logs capturados.

## Lista de verificación

- [ ] `effect = "read"` y no cambia nada en el servicio remoto.
- [ ] `In` con `extra="forbid"`, plano, con longitudes y rangos; sin campos de alcance.
- [ ] El alcance sale de `RunContext`.
- [ ] Red por `faro_engine/net` o `faro_engine/wordpress`; secretos solo de la concesión y soltados al terminar.
- [ ] Texto externo como `UntrustedText`; salida acotada.
- [ ] Errores con código del catálogo, sin contenido ni secretos en `message`/`details`.
- [ ] Pruebas obligatorias en verde; cambio revisado por `revisor-seguridad` si la herramienta lee secretos o se ofrece al modelo.
