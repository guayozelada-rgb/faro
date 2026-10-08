# ADR 0014 — Tareas de agentes en el protocolo núcleo ↔ motor: concesiones por ejecución, pausa global y eventos en vivo

- **Fecha:** 2026-10-05
- **Estado:** Aceptado (2026-10-05, con la aprobación de la spec F1b)
- **Spec:** [F1b — Capa de IA y motor de agentes](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md)
- **Amplía:** ADR 0010 (protocolo v2, concesiones por operación). **Relacionados:** ADR 0015 (capa de IA), ADR 0016 (autonomía y aprobaciones)
- **Skills afectadas:** `llavero-y-cifrado`, `tauri-sidecar-python`, `contratos-api-local`, `tauri-comandos-y-permisos`, `faro-arquitectura`

## Contexto

Hasta F1a toda petición que necesita un secreto nace en la interfaz: `engine_call` crea una concesión que vive lo que dura la llamada HTTP (como mucho `timeout_seconds + 5 s`) y desaparece al volver (`GrantGuard`). ADR 0010 §3 dejó escrito que las tareas de agentes usarían "concesiones que duran toda la tarea, cerradas con un evento `run_finished` o por caducidad", a decidir en su spec.

F1b introduce tres cosas que no encajan en ese modelo:

1. **Tareas largas y en segundo plano.** Un agente puede trabajar varios minutos, quedarse esperando una aprobación durante días y reanudarse después. La llamada que lo lanzó (`startAgentRun`) responde `202` en milisegundos: su concesión ya no existe cuando el agente pide la clave de IA.
2. **Tareas que no inicia la interfaz.** El programador del motor lanza tareas recurrentes y las que no corrieron mientras la app estaba cerrada. Nadie llama a `engine_call` en ese momento.
3. **Control y visibilidad en vivo.** El usuario necesita ver qué hace cada agente mientras trabaja y poder pararlos a todos con un botón, también si el motor se porta mal.

Hoy no hay ningún canal del motor a la interfaz salvo las respuestas de `engine_call`. La skill `contratos-api-local` preveía que el núcleo retransmitiera un flujo SSE del motor como eventos `engine://run/{run_id}`; no está construido.

## Decisión

### 1. Concesiones por ejecución (run grants)

Una **ejecución** es un tramo continuo de trabajo de una tarea de agente (`agent_runs.id`): desde que el trabajador del motor la toma de la cola hasta que termina, falla, se cancela, se pausa o se queda esperando una aprobación. Una tarea reanudada días después es una ejecución nueva de la misma tarea, con su propia concesión.

- **El motor la pide** por stdout antes de empezar cada ejecución:
  `{"event":"run_grant_request","id":"<uuid>","run_id":"<uuid>","agent":"site_summary","site_id":"<uuid>"|null,"provider":"anthropic"|"openai"|"gemini"|null,"trigger":"user"|"schedule"|"catch_up"}`
- **El núcleo decide** con una tabla compilada, igual que con las operaciones: `packages/shared/agent-grants.json`, generada por `npm run contracts` desde el registro de agentes del motor e incrustada al compilar (`include_str!`). Por cada tipo de agente declara `requires_site`, `max_grant_seconds` (60–900) y `secrets`. Reglas de la tabla (las comprueban el motor al arrancar, el generador y una prueba del núcleo):
  - Solo acceso `get`. Un agente nunca crea, reemplaza ni borra secretos.
  - Plantillas admitidas: `llm/<anthropic|openai|gemini>/default` (literal) y `wp/{site_id}/token`. Nada más (`db/*` y `oauth/*` rechazadas).
  - Cualquier cambio del archivo requiere revisión de `revisor-seguridad` (casilla en la plantilla de PR, como `engine-operations.json`).
- **Comprobaciones del núcleo**, en orden: agentes no pausados (`agents.paused`) → `agent` está en la tabla → `run_id` UUID canónico y sin otra concesión activa → `site_id` presente si `requires_site` y en el índice de sitios del perfil activo (ADR 0010, actualización C) → `provider` de la lista y con plantilla `llm/<provider>/default` en la tabla del agente → como mucho **4** concesiones de ejecución activas. Si todo pasa, la concesión contiene **solo** `llm/<provider>/default: get` y, si el agente lo declara, `wp/<site_id>/token: get`. Cualquier fallo responde `agent.grant_denied` (motivo solo en el log y la auditoría).
- **Respuesta** por stdin: `{"event":"run_grant_response","id":"<uuid>","ok":true,"expires_in_seconds":900}` o `{"event":"run_grant_response","id":"<uuid>","error":"agents.paused"|"agent.grant_denied"}`. El motor espera como mucho 10 s.
- **Uso:** el trabajador fija `run_id` en el mismo `ContextVar` que hoy rellena `X-Faro-Run-Id` (`core/run_id.py`); el canal `secret_request` no cambia de formato. La concesión se guarda en el mismo mapa de `SecretBroker` con un tipo `Run` (además de `Operation`), atada como las demás a **generación del motor** y **perfil**, y se revalida tras el candado del llavero.
- **Fin:** el motor envía `{"event":"run_grant_release","run_id":"<uuid>","status":"succeeded"|"failed"|"cancelled"|"waiting_approval"|"paused"}` al terminar la ejecución. Es el `run_finished` previsto en ADR 0010 §3, con otro nombre porque una tarea que espera una aprobación no ha terminado. El núcleo borra la concesión y no responde.
- **Caducidad:** `max_grant_seconds` (900 s para el agente de F1b). Si una ejecución dura más, el motor puede volver a pedir la concesión para el mismo `run_id` una vez caducada la anterior; la nueva pasa por las mismas comprobaciones (incluida la pausa). La caducidad acota cuánto vive una concesión olvidada, no cuánto dura una tarea.
- **Reinicio del motor:** todas las concesiones (de operación y de ejecución) desaparecen, como hoy.

Las concesiones de ejecución **no** dependen de que la interfaz haya iniciado la tarea. Se acepta: un motor comprometido podría pedir concesiones de ejecución cuando quisiera, igual que hoy podría usar cualquier secreto que una operación en curso le conceda. Lo que la concesión sí garantiza es el **alcance** de cada ejecución: un agente que sufre una inyección de prompts no puede leer la clave de otro proveedor, el token de otro sitio ni ningún secreto que su tipo no declare, y no puede crear, cambiar ni borrar secretos. Como dice ADR 0010 (actualización E), las concesiones no son un límite frente a malware que ya corre como el usuario.

### 2. Pausa global en el núcleo

- El botón **Pausar agentes** llama a comandos del **núcleo** (`agents_pause_all`, `agents_resume_all`, `agents_control_state`), no a `engine_call`: la pausa tiene que funcionar aunque el motor no responda o se porte mal.
- El estado se guarda en `<app_data_dir>/agents-control.json` = `{"version":1,"paused":true,"changed_at":"…Z"}` (no es secreto; escritura atómica). Archivo ausente = no pausado; ilegible = **pausado** (falla cerrado) con aviso en el log.
- Al pausar, el núcleo, en este orden: guarda el archivo → borra **todas** las concesiones de ejecución (las de operación de `engine_call`, que inicia el usuario, no se tocan) → rechaza nuevas con `agents.paused` → avisa al motor por stdin `{"event":"agents_control","paused":true,"llm_providers":[…]}` → audita `agents.paused` (actor `user`).
- El motor deja de sacar tareas de la cola y detiene las que corren en el siguiente límite entre pasos (con su checkpoint). Una llamada al LLM ya enviada termina (como mucho su plazo) y su costo se registra; la siguiente petición de la clave fallaría igualmente porque la concesión ya no existe.
- `agents_control` también se envía justo después de `ready` en cada arranque del motor. **Hasta recibirlo, el motor no ejecuta ninguna tarea** (arranca pausado por defecto). En modo desarrollo externo (`--dev`) nunca llega y los agentes no corren (tampoco hay canal de secretos, ADR 0010 §5).
- `llm_providers` es la lista de proveedores con clave en la Bóveda (no es secreto: solo los nombres). El núcleo la reenvía en `agents_control` al arrancar y cada vez que la Bóveda agrega o borra una clave, para que el motor elija proveedor y estime costos sin pedir ninguna clave.

### 3. Eventos en vivo por stdout, no por SSE

- El motor publica la actividad de los agentes como líneas del protocolo: `{"event":"agent_activity","run_id":"<uuid>","seq":12,"occurred_at":"…Z","kind":"run_status"|"step_started"|"step_finished"|"approval_requested","agent":"site_summary","site_id":"<uuid>"|null,"status":"…","step":"read_site"|null,"step_cost_micros":0,"run_cost_micros":5678,"run_tokens":4321,"error_code":"llm.rate_limited"|null}`.
- El núcleo valida cada línea con un esquema **cerrado**: solo esas claves; identificadores (`agent`, `kind`, `status`, `step`, `error_code`) con forma `^[a-z][a-z0-9_.]{0,47}$`; UUID canónicos; enteros entre 0 y 10^12; `occurred_at` RFC 3339 UTC; línea ≤ 4 KB. **Nunca texto libre**: ni resúmenes, ni títulos del sitio, ni salidas del LLM. Una línea inválida se descarta con aviso en el log (sin contenido).
- Si es válida, la emite a la ventana `main` (`emit_to`) como evento Tauri **`engine://agents`** con la misma carga sin `event`. Límite de 20 eventos por segundo (cubo de fichas); el exceso se descarta y la interfaz lo detecta por el salto de `seq` y vuelve a pedir el estado con `engine_call`.
- La interfaz ya puede escuchar eventos (`core:event:allow-listen`); no hace falta permiso nuevo. La interfaz no emite eventos (prueba existente de `tests/acl.rs`).
- El texto (resultados, pasos con detalle) se obtiene siempre con `engine_call` (`getAgentRun`), con su validación, y se muestra como texto.

**Por qué stdout y no SSE:** el canal ya existe, está autenticado por ser la tubería del proceso hijo, separa por líneas y tiene un escritor con candado en cada lado. Un flujo SSE exigiría una conexión HTTP larga del núcleo al motor con token, reconexión y control de contrapresión, para un volumen de unos pocos eventos por segundo. La contrapartida es que stdout lleva más tráfico: por eso los eventos son pequeños, sin texto y con límite, y la inserción de auditoría deja de hacerse en el hilo que reparte las respuestas de secretos (pendiente F1a §12.2-3, se resuelve en F1b).

### 4. Resumen de las líneas nuevas

| Dirección | Evento | Respuesta |
| --- | --- | --- |
| motor → núcleo | `run_grant_request` | `run_grant_response` (`ok` + `expires_in_seconds`, o `error`) |
| motor → núcleo | `run_grant_release` | — |
| motor → núcleo | `agent_activity` | — (se retransmite como `engine://agents`) |
| núcleo → motor | `agents_control` (`paused`, `llm_providers`) | — (tras `ready` y en cada cambio) |

El resto del protocolo (token, `db_key`, `ready`, `shutdown`, `secret_request`/`secret_response`, `audit`) no cambia. Acciones de auditoría nuevas del núcleo: `agents.paused`, `agents.resumed`, `agent.grant_issued`, `agent.grant_denied`, `agent.grant_released`.

## Consecuencias

- `SecretBroker` gana un segundo tipo de concesión. Las reglas de ADR 0010 (orden de comprobaciones, revalidación tras el candado, generación y perfil, `db/*` nunca) aplican igual; `{new}` no existe en las de ejecución.
- `npm run contracts` genera un archivo más (`agent-grants.json`) y la CI comprueba que está al día.
- La skill `contratos-api-local` deja de mencionar SSE: las operaciones largas devuelven `202` con el `run_id` y el progreso llega por `engine://agents`.
- La pausa vive en el núcleo y sobrevive a reinicios de la app y del motor. Reanudar es siempre una acción explícita del usuario.
- Una tarea en curso al pausar puede terminar su llamada al LLM ya enviada: la pausa es inmediata para todo lo nuevo y, como mucho, tarda lo que tarde esa llamada (≤ 60 s) en quedar quieta.
- Riesgo aceptado: el motor es quien informa del estado de cada tarea; si mintiera, el feed mostraría datos falsos, pero no podría obtener secretos fuera de su concesión ni saltarse la pausa.

## Actualización (2026-10-08, revisión de seguridad de T5)

Notas del agente `tauri-rust` para que `arquitecto` las ratifique:

- **§2, orden de la pausa.** El núcleo revoca primero en memoria y guarda después: borra las concesiones de ejecución y rechaza las nuevas con `agents.paused` → avisa al motor → guarda `agents-control.json` → audita `agents.paused`. Así la pausa nunca espera al disco. Si el archivo no se puede guardar, la pausa en memoria ya está aplicada y el comando devuelve `agents.control_unavailable` (igual que antes). Reanudar no cambia: si no se puede guardar, no reanuda.
- **§3, `agent` de la tabla.** Además del esquema, el núcleo descarta una línea `agent_activity` cuyo `agent` no esté en `agent-grants.json`. La interfaz (T10) traduce `status`, `step` y `error_code` siempre por catálogo, nunca en crudo.
- **stdin atascado.** Si el motor deja de leer su stdin más de 10 s, el núcleo lo trata como no sano y lo reinicia (cuenta para el límite de reinicios). La cola de auditoría del núcleo está acotada a 500 eventos; las auditorías repetidas de solicitudes malformadas y de liberaciones de concesiones que no existen se agregan con `details.count` (ADR 0010, actualización 2026-10-08). La liberación de una concesión que no existe se audita como `denied`.
