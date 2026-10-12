# F1b T10 — Interfaz de agentes

- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §3, §4.6, §5.1–§5.5, §9.4 y fila T10 de §8.3.
- **ADR:** 0002 (errores), 0006 (CSP), 0007 (paleta), [0014](../adr/0014-tareas-de-agentes-en-el-protocolo-nucleo-motor.md) §2–§3, [0016](../adr/0016-autonomia-guardarrailes-y-aprobaciones.md).
- **Contexto usado:** informes de [T7](2026-10-11-f1b-t7-cola-y-programador.md), [T8](2026-10-11-f1b-t8-marco-de-agentes.md) y [T9](2026-10-11-f1b-t9-agente-resumen-del-sitio.md); `packages/shared/{engine.d.ts,engine-operations.json,agent-grants.json}` (28 operaciones); `apps/engine/faro_engine/core/routes/*`; núcleo `src/commands/agents.rs` y `src/agents/activity.rs`.
- **Estado:** implementado y verificado con `mockIPC`; pendiente de `revisor-seguridad` (presentación como texto, ningún secreto en la interfaz) y de la prueba manual con `--fake-llm` en Windows (§9).
- **Núcleo Rust:** sin cambios. Todo lo que la interfaz necesita ya existe desde T5 (§4).

## 1. Qué se construyó

| Archivo | Qué hace |
| --- | --- |
| `lib/api/agents.ts` | Las 21 operaciones de agentes de §5.2 sobre `api.call` (`engine_call`), tipos desde `engine.d.ts` y guardas de forma para `result` (`siteSummaryResult`), `payload` (`saveSummaryPayload`) y `evidence.reason_key`. |
| `lib/api/agentsControl.ts` | `agents_control_state`, `agents_pause_all`, `agents_resume_all` (respuesta validada, falla cerrado) y `listen("engine://agents")` con validación del esquema cerrado (`isAgentActivity`). |
| `lib/api/engineCall.ts` | `EngineResponse` admite `202` (`startAgentRun`). |
| `lib/api/errors.ts` | `{{provider}}` de los errores `llm.*` desde `details.provider` o el proveedor de la tarea; si no se sabe, "tu proveedor de IA". |
| `lib/format/money.ts`, `lib/format/dates.ts` | `formatUsdMicros` (`Intl.NumberFormat`, "menos de 0,01 US$"), lectura de montos del usuario; fechas, horas, días y tiempo relativo con `Intl`. |
| `components/faro/` | `AiBadge`, `RunStatusChip`, `AgentFeed`, `CostEstimate`, `ApprovalCard` (sin **Editar**), `SiteSummaryText` (resumen siempre como texto). |
| `components/ui/switch.tsx` | Interruptor accesible (`role="switch"`) para **Activa**. |
| `app/TopBar.tsx`, `app/PauseAgentsButton.tsx` | Barra superior con **Pausar agentes** en todas las secciones y el aviso de pausa. |
| `app/AppShell.tsx` | Monta una sola vez la escucha de `engine://agents` (`useAgentActivity`). |
| `app/Sidebar.tsx` | Número de propuestas pendientes junto a "Bandeja" (texto + "1 propuesta pendiente" para lectores de pantalla). |
| `features/agents/` | `AgentsPage` (catálogo, gasto de hoy, actividad), `SchedulesSection`, `LaunchAgentDialog`, `RunDetailSheet`, `useAgentActivity` (caché por eventos y resincronización), `useAgentsControl`, `queries.ts`, `queryKeys.ts`, `labels.ts` (nombres por catálogo, genéricos para lo desconocido), `siteHelpers.ts`. |
| `features/approvals/` | `InboxSection` (Bandeja mínima) y `useApprovalActions` (Guardar/Descartar, sugerencia de nivel 0). |
| `features/autonomy/AutonomySection.tsx` | Pestaña **Autonomía** (`?tab=autonomy`). |
| `features/vault/` | `AgentKeyPreference` (clave que usan los agentes), `KeyUsageLine` (gasto de hoy y chip de límite), `DailyLimitDialog`; `ProviderKeyRow` y `VaultSection` los integran; `refreshVaultKeys` invalida también el gasto y el estimado. |
| `features/home/` | `AgentNotices` (tareas recuperadas y tope del día), `PendingApprovalsNotice`, `SiteSummaryCards`, paso 3 en `NextSteps`. |
| `pages/` | `AgentsPage`, `InboxPage`, `HomePage` y `SettingsPage` usan lo anterior. |
| `features/sites/useSites.ts` | Corrección del bucle de reintentos (§7). |
| `locales/{es,en,pt-BR}/` | Claves nuevas en `agents`, `inbox`, `home`, `settings` (`autonomy.*`, `vault.usage.*`) y `common` (`agentsControl.*`, `money.*`, `aiBadge`, `providers.unknown`, plurales del contador). `en` y `pt-BR` con `[TODO] `. `errors.json` ya tenía todos los códigos de §5.5. |
| Pruebas | 13 archivos nuevos y 4 ajustados (§6). Dobles: `test/agentsIpc.ts` (todas las operaciones, comandos `agents_*` y emisión de `engine://agents`); `test/sitesIpc.ts` acepta operaciones de agentes. |

## 2. Decisiones (ambigüedades de la spec, opción más simple y conservadora)

1. **Formato del dinero:** el que da `Intl.NumberFormat` con el idioma de la app (`es` → "0,12 US$", `en` → "$0.12"), como dice §3 ("los ejemplos son ilustrativos"). Dos decimales. `0` → "0,00 US$"; de 1 a 9 999 micros → "menos de 0,01 US$".
2. **Actualización por eventos** (`applyActivity`): el `seq` es por tarea. `seq` ya visto → se ignora; el siguiente → se actualizan estado, paso, costo y tokens con `setQueryData`; salto de `seq`, tarea que no está en la lista o agente, estado o paso desconocidos → se vuelve a pedir la lista. Un cambio de estado distinto de `running` aplica el estado al momento **y** pide la lista, porque `status_reason`, `pending_approval_id` y las fechas no viajan en el evento. El detalle abierto se vuelve a leer con cada evento de su tarea.
3. **"Listo · descartado" en la actividad:** `AgentRunOut` no dice si la propuesta se descartó; se usa `listApprovals?status=rejected` (operación existente) para marcar esas tareas. En el detalle sale de `approval.status`.
4. **Motivos (`status_reason`):** "En cola hasta mañana" con su tooltip (spec) y, además, una línea de texto para `interrupted`, `agents_paused`, `cancel_requested`, `dependency_failed` y `user_cancelled`. Un motivo desconocido no se muestra.
5. **Error del comando de pausa:** tras cualquier error se vuelve a leer `agents_control_state`. Con `agents.control_unavailable` al pausar, el núcleo sí aplicó la pausa en memoria; la barra muestra ese estado real (Reanudar + aviso) en vez de "volver al estado anterior", que sería falso. Al reanudar, el estado vuelve a "en pausa".
6. **Mientras no se sabe el estado de la pausa** (o si `agents_control_state` falla), se ofrece **Pausar agentes**: pausar siempre es seguro.
7. **Propuesta de una acción desconocida:** se muestra un texto genérico y solo **Descartar**. No se aprueba lo que la interfaz no puede mostrar.
8. **"Cambiar límite diario":** la fila de Claves de IA no tiene menú; el botón va en la línea del gasto de cada fila con clave (con `aria-label` por proveedor).
9. **Clave que usan los agentes:** opciones "Automática (la primera que tengas)" (`null`) y los proveedores con clave en la Bóveda.
10. **Aviso del tope del día en Inicio:** sin **Entendido** (no hay operación para confirmarlo); desaparece cuando la tarea empieza. El de tareas recuperadas sí lo tiene (`acknowledgeAgentNotices`). Los dos salen de la misma lista de 20 tareas de la actividad.
11. **Paso 3 de Inicio:** "hecho" si algún sitio activo tiene resumen guardado. Mientras se leen los sitios o sus resúmenes, el paso 3 muestra su propio esqueleto (antes parpadeaba entre "pendiente" y "cargando" y el botón desaparecía bajo el clic: encontrado por una prueba).
12. **Agentes en pausa al programar:** el botón **Programar** también se deshabilita (la spec solo habla de lanzar); igual que con cualquier `blocking_code`.
13. **Regla de autonomía nueva para un sitio:** empieza con el nivel de la regla general; si se elige "Piloto automático", también pide la confirmación.
14. **`SiteSummaryV1.language`** es el idioma del contenido del sitio (T9, decisión 4), no el del resumen: no se usa como atributo `lang`.
15. **Bloque "Lo que ningún agente hace nunca":** fijo en la interfaz (los cuatro textos de §3.7), no depende de la respuesta del motor.

## 3. Reglas que nunca se rompen (cómo se cumplen)

| Regla | Cómo |
| --- | --- |
| La interfaz no habla con el motor | Todo pasa por `engine_call` (`lib/api/agents.ts`) o por los comandos `agents_*` del núcleo. |
| Claves fuera de la interfaz | Ninguna operación de agentes pide ni devuelve secretos; el gasto es por proveedor (`LlmUsageOut` no trae `last4`). Prueba: ninguna petición lleva `llm/`, `secret`, `token` ni `key` (`agents.test.ts`). |
| Nada que publique o gaste sin la Bandeja | La única acción (`site_summary.save`, `internal`) se decide en la Bandeja o en el detalle; el costo de IA se ve antes de lanzar y se acepta con `accepted_max_cost_micros`. |
| Textos como texto | `SiteSummaryText` y `ApprovalCard` usan `{texto}` y `white-space: pre-line`. Guarda estática `test/noHtml.test.ts` (sin `dangerouslySetInnerHTML`, `innerHTML`, Markdown ni enlaces automáticos) y pruebas con `<script>`, `<b>`, `<img>`, `<a>` y Markdown que se ven literales. |
| Identificadores desconocidos | `labels.ts`: "Un agente", "Trabajando", "En proceso"; nunca el identificador en crudo (probado). |
| Sin textos en duro | `i18next/no-literal-string` en verde; `locales.test.ts` exige las mismas claves con `[TODO] ` en `en` y `pt-BR`. |
| Paleta | Azul `ai` solo en lo de los agentes (icono de agente, "Trabajando", "Hecho por IA", bordes de las vistas previas); turquesa en acciones; **Pausar agentes** en `critical`. `tokens.test.ts` y `palette.test.ts` en verde. |

## 4. Núcleo Rust: qué necesita la interfaz

**Nada nuevo.** Comprobado en `apps/desktop/src-tauri/`:

- `agents_pause_all`, `agents_resume_all` y `agents_control_state` registrados en `generate_handler!` (`src/lib.rs`), con `permissions/agents.toml` y en `capabilities/main.json`; devuelven `{paused, changed_at}`.
- `engine://agents` se emite a la ventana `main` con la carga validada (`src/agents/activity.rs`); la interfaz solo necesita `core:event:allow-listen`/`unlisten`, ya concedidos.
- `engine_call` acepta las 28 operaciones de `engine-operations.json` (todas las de agentes con `secrets: []`), reenvía el cuerpo de un `202` (`client.rs`, `status.is_success()`) y valida los parámetros de ruta (`run_id`, `approval_id`, `schedule_id`, `rule_id` y `provider` cumplen `^[A-Za-z0-9_-]{1,64}$`).
- `--fake-llm` con doble llave (build de depuración + `FARO_FAKE_LLM=1`, desde el entorno o `.env.local`) en `src/engine/mod.rs`.

No se tocó Rust; `cargo fmt --check` en verde (dentro de `npm run format:check`).

## 5. Pantallas × estados

| Pantalla | Vacío | Cargando | Error | Éxito |
| --- | --- | --- | --- | --- |
| Barra superior (Pausar agentes) | — (siempre visible) | Mientras se lee el estado: **Pausar agentes** | Toast con el mensaje del código y estado real del núcleo | Pausar / Pausando… / Reanudar + aviso `warning` |
| Agentes · Qué hacer ahora | "Esta versión de Faro todavía no tiene agentes." | Esqueleto de tarjeta | Mensaje del código + Intentar de nuevo (sin botón con `db.*`, como F1a) | Tarjeta con **Lanzar**; sin sitio: **Conectar tu sitio**; sin clave: **Agregar clave de IA** |
| Agentes · Gasto de hoy | 0,00 US$ | Esqueleto de línea | "No pudimos leer el gasto de hoy." + Intentar de nuevo | Total + tooltip por proveedor + **Ver límites** |
| Agentes · Actividad | "Todavía no hay actividad." + **Lanzar Resumen del sitio** | Tres filas esqueleto | `engine.not_ready`/`db.*` con su mensaje (+ Intentar de nuevo salvo `db.*`) | Filas con los 7 estados, paso, costo, motivo; en vivo por eventos |
| Agentes · Programadas | "No tienes tareas programadas." + **Programar** | Esqueleto | Mensaje + Intentar de nuevo | Frecuencia, próxima fecha, **Activa**, ⋯ → Quitar programación |
| Diálogo Lanzar | Sin sitios: "Conecta tu sitio…" + Conectar tu sitio | "Calculando el costo…" + esqueleto | Mensaje + Intentar de nuevo; `blocking_code`; `agent.estimate_changed`; `schedule.duplicate` bajo Repetir | Costo, clave y gasto; **Lanzar por unos …** o **Programar** |
| Detalle de la tarea | "Todavía no empezó ningún paso." | Esqueleto | `agent.run_not_found` sin botón; otros + Intentar de nuevo; tarea fallida + **Lanzar de nuevo** | Pasos y costos, resultado como texto, propuesta, sugerencia (nivel 0), Cancelar tarea |
| Bandeja | "Nada por aprobar" (texto de F0) | Dos tarjetas esqueleto | Mensaje + Intentar de nuevo; decisiones: toast y la lista se refresca | Tarjetas con qué, por qué, caducidad, vista previa, Guardar / Descartar |
| Claves de IA (cambios) | Sin claves: sin selector | Selector deshabilitado | "No pudimos leer el gasto de hoy." + Intentar de nuevo; errores del límite en el diálogo | Gasto por fila, chip de límite, diálogo del límite, selector |
| Autonomía | Sin reglas: nivel 1 por defecto | Esqueleto | Mensaje + Intentar de nuevo; toast al guardar con error | Niveles, reglas por sitio, confirmación del piloto automático, guardarraíles |
| Inicio (cambios) | Sin avisos ni propuestas: no se muestran | Paso 3 con esqueleto | Sin lista de tareas: sin avisos (Inicio no se rompe) | Avisos + Entendido, propuestas, paso 3, "Tu sitio en pocas palabras" |

## 6. Capturas

Reales, de Chromium headless (Playwright 1.56) sobre la app con `mockIPC` (arnés fuera del repositorio; viewport 1280×860, `prefers-reduced-motion`, zona `America/Guatemala`). En [`2026-10-12-f1b-t10/`](2026-10-12-f1b-t10/):

| Archivo | Qué muestra |
| --- | --- |
| `01-agentes-exito.jpg` / `02-agentes-exito-oscuro.jpg` | Catálogo, gasto de hoy y actividad con "Trabajando" (punto azul), "Espera tu OK", "En cola hasta mañana" (con su ⓘ), "Listo", fallida; tema claro y oscuro (nada más oscuro que `#333333`). |
| `03-agentes-vacio.jpg`, `04-agentes-cargando.jpg`, `05-agentes-error.jpg` | Estados vacío, esqueletos y `engine.not_ready` con Intentar de nuevo. |
| `06-lanzar-dialogo.jpg`, `07-lanzar-limite-del-dia.jpg` | Costo estimado y máximo con `Intl`, clave y gasto; bloqueo por el tope del día con **Cambiar límite**. |
| `08-detalle-espera-ok.jpg` | Panel lateral: propuesta con "Hecho por IA", por qué, vista previa, Guardar resumen / Descartar y pasos con costo. |
| `09-detalle-resultado-como-texto.jpg` | Un resumen con `<script>`, `<b>` y Markdown se ve literal. |
| `10-bandeja-exito.jpg`, `11-bandeja-vacia.jpg`, `17-bandeja-oscuro.jpg` | Bandeja con una propuesta (caduca el …) y vacía; contador "1" en la barra lateral. |
| `12-inicio-avisos-y-resumen.jpg` | Aviso de tarea recuperada + Entendido, "Tienes 1 propuesta por revisar.", "Todo listo…" y "Tu sitio en pocas palabras". |
| `13-claves-de-ia-gasto.jpg` | Selector "Clave que usan los agentes", "Hoy: 0,12 US$ de 5,00 US$" y **Cambiar límite diario**. |
| `14-autonomia.jpg`, `15-autonomia-piloto-automatico.jpg` | Regla general y por sitio, guardarraíles fijos y la confirmación del piloto automático. |
| `16-agentes-pausando.jpg`, `16b-agentes-en-pausa.jpg` | "Pausando…" mientras una tarea sigue `running`; **Reanudar agentes** + aviso. |

## 7. Hallazgo corregido: bucle de reintentos con `engine.not_ready`

Al capturar el estado de error, la página de Chromium se quedó colgada. Causa: si el motor ya estaba `ready` y una lectura respondía `engine.not_ready`, el efecto que "vuelve a leer cuando el motor queda listo" se repetía sin fin (el mismo patrón venía de `useSites`, F1a). Corrección en `useRefetchWhenEngineReady` (nuevo) y en `useSites`: como mucho **un** reintento automático por cada estado "listo" del motor y consulta; después queda **Intentar de nuevo**. El comportamiento de F1a se mantiene (`SitesSection.test.tsx`, "motor no listo: vuelve a leer sola…", en verde).

Comprobación: con la lógica anterior la captura `05` vuelve a colgarse en Chromium; con la corrección termina. La prueba nueva (`AgentsPage.test.tsx`, "reintentos acotados, sin bucle") comprueba que las lecturas dejan de crecer, pero en jsdom la lógica anterior también la pasa (el bucle depende de la planificación del navegador real): queda como guarda del comportamiento, no como reproducción. Lo reproduce el arnés de capturas.

## 8. Pruebas (§9.4) y verificaciones

| Requisito de §9.4 | Pruebas |
| --- | --- |
| Pausa: en todas las secciones, tres estados, persistencia, error | `app/PauseAgentsButton.test.tsx` (17: las 8 secciones, estado inicial desde `agents_control_state`, Pausando… hasta que no hay `running`, Reanudar con toast, error al pausar y al reanudar, tooltip con teclado, una sola escucha que se suelta al desmontar) |
| Agentes: catálogo, gasto, actividad (4 estados, 7 estados de tarea), eventos, resincronización, programadas | `features/agents/AgentsPage.test.tsx` (25) y `useAgentActivity.test.ts` (10) |
| Lanzar: estimado (cargando, éxito, cada `blocking_code`, error), `agent.estimate_changed`, pausa | `features/agents/LaunchAgentDialog.test.tsx` (14, también programar y `schedule.duplicate`) |
| Detalle: pasos y costos, resultado como texto (`<script>`, `<b>`), propuesta, nivel 0, cancelar, error | `features/agents/RunDetailSheet.test.tsx` (12) |
| Bandeja mínima y contador | `features/approvals/InboxSection.test.tsx` (10) |
| Claves de IA (gasto, límite con validación, preferencia) | `features/vault/VaultUsage.test.tsx` (7) |
| Autonomía (niveles, regla por sitio, piloto automático, guardarraíles) | `features/autonomy/AutonomySection.test.tsx` (7) |
| Inicio (aviso de recuperadas y confirmar, propuestas, paso 3, tarjeta) | `features/home/HomeAgents.test.tsx` (7) y `pages/HomePage.test.tsx` (+2) |
| `formatUsdMicros` (0, < 1 centavo, centavos, dólares) con `es` y `en` | `lib/format/money.test.ts` (23) y `dates.test.ts` (4) |
| `api.call` con cada operación; ningún comando ni respuesta con una clave | `lib/api/agents.test.ts` (25) y `agentsControl.test.ts` (14) |
| Ningún resultado como HTML | `test/noHtml.test.ts` (7) |
| `{{provider}}` de los errores | `lib/api/errors.test.ts` (+3) |

Ajustes a pruebas existentes por cambios de la spec: `pages.test.tsx` (Bandeja ya no es "próximamente"; Agentes tiene sus propias pruebas; tres pestañas de Configuración) y `HomePage.test.tsx` ("Todo listo" exige el resumen guardado).

Verificaciones, desde la raíz, 2026-10-12:

| Comando | Resultado |
| --- | --- |
| `npm run format:check` | `All matched files use Prettier code style!`; `cargo fmt --check` sin diferencias; `264 files already formatted` (ruff) |
| `npm run lint -w apps/desktop` | `eslint . --max-warnings 0` sin errores |
| `npm run typecheck` | `tsc --noEmit` (desktop y shared) sin errores; mypy `Success: no issues found in 259 source files` |
| `npm run test -w apps/desktop` | `Test Files 32 passed (32)`, `Tests 669 passed (669)` (antes 483); cobertura: líneas 96 %, sentencias 96,09 %, ramas 88,62 %, funciones 95,1 % (umbral 70 %); `tokens.test.ts` y `palette.test.ts` en verde |
| `npm run test:contracts` | `tests 120`, `pass 120`, `fail 0` |

No se ejecutaron aquí: `npm run test:core` (el núcleo no compila sin GTK; no cambió) ni `npm run dev` con la ventana Tauri.

## 9. Prueba manual con `--fake-llm` (Windows 11)

Requisitos: build de depuración (`npm run dev`), wp-env con el plugin y una clave de IA guardada en la Bóveda. Con `--fake-llm` los agentes no llaman a ningún proveedor ni gastan, pero necesitan una clave guardada para elegir proveedor, y la Bóveda prueba la clave con el proveedor al guardarla (núcleo, ADR 0003): usa una clave real; no se gastará nada con ella en estos pasos.

1. En la raíz, `.env.local` con `FARO_FAKE_LLM=1` y `FARO_ALLOW_LOCAL_SITES=1` (copia de `.env.local.example` si no existe). No escribas claves en ese archivo.
2. `npm run setup` (si hace falta) y `npm run dev`. En el log del núcleo debe aparecer `motor lanzado con --fake-llm (solo desarrollo)`.
3. Configuración → Sitios conectados: conecta el WordPress de wp-env (`http://localhost:8888`). Configuración → Claves de IA: agrega tu clave de Anthropic (o de otro proveedor).
4. Barra superior: **Pausar agentes** (rojo) en todas las secciones; pásale el ratón o el teclado: tooltip.
5. Agentes → **Lanzar**: el diálogo muestra "Costo estimado: unos … Como mucho: …" y "Usará tu clave de Anthropic…". **Lanzar por unos …** → toast "El agente empezó a trabajar.".
6. Actividad: la fila pasa por "En cola" → "Trabajando" (punto azul) con "Leyendo tu sitio…", "Ordenando tu contenido…", "Escribiendo el resumen…" y el costo subiendo → "Espera tu OK". La Bandeja muestra "1" en la barra lateral.
7. Clic en la fila: panel con pasos y costos y la propuesta "Guardar este resumen en Faro" con "Hecho por IA" y el texto "Resumen simulado de tu sitio (modo de desarrollo)". **Guardar resumen** → toast "Guardamos el resumen." y la tarea termina en "Listo".
8. Inicio: paso 3 "Conoce tu sitio con IA" hecho, "Todo listo…" y "Tu sitio en pocas palabras" con **Ver resumen** y **Actualizar**.
9. Lanza otra vez y pulsa **Pausar agentes** mientras trabaja: "Pausando…" y luego **Reanudar agentes** con el aviso amarillo; **Lanzar** queda deshabilitado. Cierra y abre Faro: sigue en pausa. **Reanudar agentes** → toast y la tarea termina.
10. Bandeja: con una propuesta pendiente, **Descartar** → "Descartamos la propuesta."; en Actividad, "Listo · descartado".
11. Configuración → Autonomía: "Solo sugerir" y lanza: en el detalle, "Sugerencia: guarda este resumen si te sirve." + **Guardar resumen**. "Piloto automático" pide confirmación.
12. Claves de IA → **Cambiar límite diario** → escribe `0,2`: error de rango; `0,5` → "Guardamos el límite de Anthropic.". Lanza varias veces con `--fake-llm` (spec §10, paso 12) hasta llegar al tope: el diálogo muestra "Esta tarea podría pasar tu límite de hoy…" con **Cambiar límite**, y la fila, el chip "Límite de hoy alcanzado".
13. Agentes → Programadas → **Programar** "Cada día" a la hora actual + 2 min: aparece con "Próxima: …"; se lanza sola. **Activa** la pausa; ⋯ → **Quitar programación** pide confirmación.
14. Tema oscuro de Windows: azul solo en lo de IA, turquesa en acciones, **Pausar agentes** en rojo claro, nada más oscuro que `#333333`.
15. Teclado: Tab recorre barra lateral, **Pausar agentes**, catálogo, filas de la actividad (Enter abre el detalle) y Escape cierra panel y diálogos.

## 10. Pendiente

- **`revisor-seguridad` (T10, spec §7):** presentación como texto (resumen y propuestas), ningún secreto en la interfaz, eventos validados antes de usarlos, guarda `noHtml.test.ts`.
- **Prueba manual en Windows** (§9): no se pudo ejecutar aquí.
- **T11 (`devops-release`):** `.env.local.example` con `FARO_FAKE_LLM` (los pasos de §9 lo usan); README con el flujo de agentes en desarrollo; nada de la interfaz bloquea la CI.
- **T12 (`qa-pruebas`):** lista manual de la spec §10 con clave real (pasos 3, 5 y 13) y con `--fake-llm`; medir en un equipo real la cadencia de eventos de una tarea (no debería haber resincronizaciones salvo saltos de `seq`); revisar con lector de pantalla (NVDA) los anuncios de la actividad (`aria-live="polite"` en la lista: cada cambio de costo se anuncia; si resulta ruidoso, limitarlo a cambios de estado es un cambio de spec).
- **T14 (`arquitecto`):** la skill `sistema-diseno-faro` gana `RunStatusChip`, `SiteSummaryText` y `Switch`; anotar en la spec las decisiones de §2 (formato de `Intl` "0,12 US$", "Listo · descartado" vía `listApprovals?status=rejected`, botón de límite sin menú, acción desconocida solo descartable) y el reintento acotado de §7.
- **Traducciones** `en` y `pt-BR`: siguen con `[TODO] `.
