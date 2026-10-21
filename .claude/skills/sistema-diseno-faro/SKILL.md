---
name: sistema-diseno-faro
description: Sistema de diseño de Faro - tokens de color (grises neutros, turquesa de acción, azul reservado para la IA), tipografía y espaciado, componentes base y propios, estados obligatorios, patrones de pantalla y accesibilidad. Úsala al crear o cambiar cualquier pantalla o componente de la interfaz.
---

# Sistema de diseño de Faro

Estilo: herramienta profesional sobria. Escala de **grises neutros** (sin tinte azulado), un solo color de acción (**turquesa**) y un color exclusivo para todo lo que hace la IA (**azul claro**). **Sin morado ni violeta** en ninguna parte (ADR 0007).

## Reglas de la paleta (ADR 0007)
- **Gris más oscuro permitido: 80 % (`#333333`).** Nada en la interfaz es más oscuro: ni fondos, ni textos, ni bordes. Prohibido negro y grises por debajo de `#333333`.
- **Modo oscuro:** fondo de ventana `#333333`; superficies algo más claras para separar planos. Texto principal **blanco** `#FFFFFF`; texto secundario **gris 15 %** `#D9D9D9` como máximo de oscuridad. Nunca texto más oscuro que `#D9D9D9` sobre fondos oscuros.
- **Modo claro:** fondos blancos y grises muy claros; texto principal `#333333`, secundario `#595959`.
- **Turquesa** solo para acción: botones principales, enlaces, foco y sección activa. Sobre fondo turquesa el texto va en `#333333` (oscuro) o `#FFFFFF` (claro) según el token `primary-foreground`.
- `success`, `warning` y `critical` se mantienen (verde, ámbar, rojo): son señales de estado y siempre van con icono o texto.

## Tokens (Tailwind, en `apps/desktop/src/styles/tokens.css`)

| Token | Uso | Claro | Oscuro |
| --- | --- | --- | --- |
| `background` | Fondo de la ventana | #FFFFFF | #333333 |
| `surface` | Barra lateral, barra superior | #F5F5F5 | #3B3B3B |
| `card` | Tarjetas, diálogos, menús | #FFFFFF | #424242 |
| `muted` | Hover, esqueletos, fondos suaves | #EBEBEB | #4A4A4A |
| `border` | Bordes 1 px de tarjetas y separadores | #D2D2D2 | #5C5C5C |
| `input` | Borde de campos y controles (≥ 3:1) | #8C8C8C | #8F8F8F |
| `overlay` | Velo detrás de diálogos | #333333 al 60 % | #333333 al 80 % |
| `shadow` | Sombra mínima | #333333 al 10 % | #333333 al 40 % |
| `foreground` | Texto principal | #333333 | #FFFFFF |
| `muted-foreground` | Texto secundario | #595959 | #D9D9D9 |
| `primary` | Botones principales, enlaces, foco, sección activa (turquesa) | #0F766E | #2DD4BF |
| `primary-foreground` | Texto sobre `primary` | #FFFFFF | #333333 |
| `ai` | Todo lo generado o propuesto por agentes (azul claro) | #1D4ED8 | #93C5FD |
| `ai-foreground` | Texto sobre `ai` | #FFFFFF | #333333 |
| `success` | Conectado, aprobado, mejora | #15803D | #86EFAC |
| `warning` | Revisar, límite cerca | #B45309 | #FCD34D |
| `critical` | Error, gasto anómalo, Pausar agentes | #B91C1C | #FCA5A5 |
| `critical-foreground` | Texto sobre `critical` | #FFFFFF | #333333 |
| `neutral-50…900` | Escala de grises neutros | #FAFAFA … #333333 | invertida, #333333 … #FFFFFF |

Los valores exactos pueden ajustarse solo para cumplir contraste (≥ 4,5:1 en texto y ≥ 3:1 en bordes de controles y foco), sin salirse de las reglas de la paleta; la prueba automática de contraste (`tokens.test.ts`) es la que manda.

- Usa siempre tokens (`bg-primary`, `text-ai`), nunca hex en componentes. La paleta de Tailwind está desactivada (`--color-*: initial` en `globals.css`) y `palette.test.ts` falla si aparecen clases de paleta directa, `black`/`white` o hex fuera de `tokens.css`.
- Sección activa de la barra lateral: fondo `muted`, texto `primary` en negrita y barra `primary` de 4 px (con `bg-primary/10` el texto no llegaba a 4,5:1 en claro).
- `ai` no se usa para nada que no venga de un agente. `critical` solo para errores y acciones destructivas o de pausa.

**Tipografía:** Inter (12/14/16/20/24/32 px; pesos 400/500/600). JetBrains Mono para URLs, código y claves enmascaradas. Tablas 14 px, lectura 16 px.
**Espaciado:** múltiplos de 4 px (4, 8, 12, 16, 24, 32, 48). **Radios:** 8 px controles, 12 px tarjetas. Bordes 1 px, sombras mínimas.
**Iconos:** lucide-react, 16 o 20 px, trazo 1.5.

## Layout de la app
- Ventana mínima 1100×700. Barra lateral fija 240 px (colapsable a 64 px) con las 8 secciones: Inicio, Bandeja, Investigación, Contenido, Auditoría, Anuncios, Agentes, Configuración.
- Barra superior: selector de sitio, paleta de comandos (Ctrl/Cmd+K), botón **Pausar agentes** (siempre visible, `critical`), perfil.
- Contenido con ancho máximo 1280 px y márgenes de 32 px.

## Componentes
Base (shadcn/ui personalizado, en `src/components/ui/`): Button (primary, secondary, ghost, destructive), Input, Label, Switch, Tabs, Tooltip, Dialog, Sheet (panel lateral), DropdownMenu, Toast (`sonner`), Skeleton, Badge. Select se agregará cuando una pantalla lo use.

Propios de Faro (en `src/components/faro/`):
- **AiBadge**: chip azul claro (`ai`) con icono ✦ y texto "Hecho por IA". Marca todo lo que generó o propone un agente.
- **AgentFeed**: actividad en vivo, una fila por tarea (agente, sitio, paso actual, costo acumulado y estado); `aria-live="polite"`; Enter o clic abre el detalle. Se actualiza con los eventos `engine://agents` (`useAgentActivity`).
- **RunStatusChip**: estado de una tarea con icono y texto, nunca solo color: `queued`, `paused` y `cancelled` en neutro, `running` en `ai`, `waiting_approval` ("Espera tu OK") en `warning`, `succeeded` en `success`, `failed` en `critical`. Un estado desconocido muestra un genérico.
- **ApprovalCard**: qué quiere hacer el agente, por qué (evidencia), vista previa **como texto**, y **Guardar resumen** / **Descartar** (sin **Editar** en F1b). Una acción desconocida solo se puede descartar.
- **CostEstimate**: costo antes de lanzar ("unos …, como mucho …"), la clave que se usará y el gasto de hoy; desglose de modelos y tokens en un tooltip; si algo impide lanzar, el motivo en lugar del costo. Dinero siempre con `formatUsdMicros` (`lib/format/money.ts`, `Intl.NumberFormat` en el idioma del usuario: "0,12 US$"; por debajo de un centavo, "menos de 0,01 US$").
- **SiteSummaryText**: resumen del sitio escrito por la IA, siempre como texto (`{texto}` en JSX, saltos de línea con CSS; nunca HTML, Markdown ni enlaces automáticos).
- **ConnectionChip**: estado de una integración (Conectada, Falla, Vencida, Sin conectar).
- **EmptyState**: estado vacío con el valor de la sección y, si hay algo que hacer, una acción.
- **MetricCard**, **DifficultyMeter** (semáforo con texto, nunca solo color), **ClusterCard**, **EditorialCalendar**, **ContentEditor** (dos columnas), **AdPreview**: se agregan en fases posteriores.

En la estructura de la app (`src/app/`):
- **PauseAgentsButton** (barra superior, siempre visible, `critical`): llama a los comandos `agents_pause_all`/`agents_resume_all` del núcleo. Con los agentes en pausa, aviso visible y **Lanzar** deshabilitado. Si la pausa no se pudo guardar (`saved: false` de `agents_control_state`, o `agents.control_unavailable` al pausar), aviso extra con icono `critical`: "si cierras Faro, los agentes volverán a trabajar". Nunca sugiere reiniciar Faro.
- **RouteError**: `errorElement` alrededor de las secciones; un error al dibujar una sección no tumba la barra lateral ni la superior (**Pausar agentes** sigue disponible) y nunca muestra el contenido del error.

Reglas para lo que viene del motor:
- Textos de agentes (resultados, propuestas, resúmenes) siempre como texto: sin `dangerouslySetInnerHTML`, sin Markdown a HTML, sin enlaces automáticos.
- Identificadores del motor (`status`, `step`, `error_code`, `trigger`, nivel de modelo, proveedor, acción) se traducen **por catálogo** (`features/agents/labels.ts` y `locales/*`); uno desconocido muestra un texto genérico ("Un agente", "Trabajando", "Sin información"), nunca la clave en crudo.
- Lecturas que fallan porque el motor no está listo se reintentan cuando vuelve (`useRefetchWhenEngineReady`), con reintentos acotados: nunca en bucle.
- Autonomía: solo se ofrecen niveles hasta el máximo de las acciones del agente (`maxLevelFor`); subir a "Piloto automático" pide confirmación.

## Estados obligatorios en cada pantalla
1. **Vacío**: ilustración lineal simple, una frase del valor y una acción principal ("Conecta tu sitio para ver su salud").
2. **Cargando**: esqueletos con la forma final; si tarda más de 3 s, muestra el AgentFeed.
3. **Error**: qué pasó y cómo arreglarlo en lenguaje simple, con botón de acción. Nunca códigos ni trazas.
4. **Éxito**: el resultado primero, luego el detalle.

## Patrones
- Cada pantalla empieza con **qué hacer ahora** (1–3 acciones recomendadas) y después los datos.
- **Modo simple / experto**: las columnas técnicas y filtros avanzados solo se muestran en experto.
- Acciones de riesgo (publicar, activar campaña, cambiar presupuesto): diálogo de confirmación con el impacto en grande.
- Tablas: máximo 6 columnas en modo simple; paginación o scroll virtual desde 200 filas.

## Accesibilidad (WCAG 2.2 AA)
- Contraste de texto ≥ 4,5:1 sobre `background`, `surface`, `card` y `muted`, en ambos modos (lo verifica `tokens.test.ts`).
- Todo accesible con teclado; foco visible con anillo `primary` de 2 px.
- Etiquetas y `aria-*` en controles; `aria-live="polite"` en AgentFeed y toasts.
- El color nunca es la única señal: acompaña con icono o texto.
- Respeta `prefers-reduced-motion`.
