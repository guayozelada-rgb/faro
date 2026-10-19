# F1b T13 — Revisión de seguridad de F1b

- **Fecha:** 2026-10-19
- **Autor:** revisor-seguridad (revisión), tauri-rust, motor-python, devops-release y frontend-react (arreglos)
- **Rama:** `feat/f1b-t6-capa-llm-hz39c9`, reiniciada desde `main` en `89e15dc`, con T1–T12 integradas.
- **Spec:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md) §7 y fila T13 de §8.3.
- **Alcance:** T2, T2b, T3, T5–T11 y T12 con §7. Se leyó el código entero de cada capa, no solo los diffs. La revisión se repartió en tres partes (núcleo Rust; motor Python; interfaz, cadena de suministro, CI y empaquetado).
- **Condiciones que cierra:**
  - condición 7 (a, b, c) de [T5](2026-10-08-f1b-t5-condiciones.md), con plazo "a más tardar T13";
  - observación de [T7](2026-10-11-f1b-t7-cola-y-programador.md) sobre `sites/service.py`;
  - la condición de [T12](2026-10-14-f1b-t12-pruebas.md) §10 "confirmar que `engine` pasa en Windows con la vigilancia de lanzamientos".
- **Estado:**
  - primera revisión: **APROBADO CON CAMBIOS** (núcleo y motor) y **APROBADO** (interfaz y CI);
  - los 4 hallazgos y la observación de T7 están corregidos;
  - segunda revisión: **APROBADO CON CAMBIOS**, con 3 hallazgos nuevos (H1 medio, H2 y H3 bajos), todos corregidos (§5);
  - tercera revisión: **APROBADO** (§6).

## 1. Veredicto de la primera revisión (commit `89e15dc`)

| Parte | Veredicto | Hallazgos |
| --- | --- | --- |
| Núcleo Rust | APROBADO CON CAMBIOS | C1 (media), C2 (baja) |
| Motor Python | APROBADO CON CAMBIOS | M1 (baja) |
| Interfaz, cadena de suministro, CI y empaquetado | APROBADO | I1 (baja) |

No hay ningún hallazgo alto. No se encontró ninguna fuga de claves, ninguna forma de saltarse la pausa o la Bandeja, ni concesiones fuera de la tabla. En condiciones realistas, el límite de gasto por tarea se respeta (§3.2).

## 2. Hallazgos y arreglos

| Id | Sev. | Dónde | Problema | Arreglo | Pruebas |
| --- | --- | --- | --- | --- | --- |
| C1 | Media | `src-tauri/src/secrets/run_grants.rs`, `secrets/mod.rs` | Faltaba la condición 7 (a, b) de T5: no había límite de ritmo por agente al emitir concesiones ni tope de líneas `secret.used`/emitida/liberada. Un motor comprometido o en bucle podía agotar en minutos la reserva de 16 MiB del log de decisiones, y el resto del día se perdían pausa, reanudación, reinicios y concesiones. También chocaba con la condición 1 de T5. | Módulo nuevo `secrets/decisions.rs`, con tres límites: (a) cubo de 10 concesiones por tipo de agente, que recupera 1 por minuto, comprobado al final de `decide_run_grant`. Al superarlo, el motor recibe `agents.grant_rate_limited` y la auditoría registra `agent.grant_denied` con `reason=rate_limited`. (b) Por concesión, referencia y operación, solo los 3 primeros usos van al log uno a uno. El resto se resume con `count` y `errors` al liberar, caducar, pausar, terminar `engine_call` o parar el motor. (c) Tope común de 200 líneas del motor, que recupera 1 cada 15 s. Lo que no cabe se cuenta y sale en "decisiones del motor sin registrar". Cota diaria: unas 11 700 líneas, unos 3 MiB. La auditoría no cambia. | `run_grants_tests.rs`: ritmo por agente, recuperación del cubo, resumen de usos con los 5 finales, contador sin fichas, e inundación (1 500 ciclos con 20 `get` y 3 agentes, más 10 `engine_call` con 300 `get`) con `CappedWriter` real. La pausa, el reinicio y el apagado siguen en el log. Más 4 pruebas unitarias en `decisions.rs`. |
| C2 | Baja | `src-tauri/src/logging/capped.rs` | Una línea se reconocía como decisión buscando la subcadena `"target":"faro_lib::decision"` en toda la línea. | Primero filtra rápido por la marca y después lee con serde solo la clave `target` de nivel superior. No sirve comprobar un prefijo fijo, porque el formateador escribe `fields` antes que `target`. | `un_target_anidado_no_pasa_por_decision` |
| I1 | Baja | `engine/scripts/pyinstaller_offline/sitecustomize.py`, `scripts/bundle_smoke_build.py` | El bloqueo de red del build aceptaba `*.localhost`, y `build_env` pasaba a PyInstaller `HTTP(S)_PROXY`/`ALL_PROXY`. Con un proxy en loopback, el build podía salir a internet aunque el resumen dijera "Red en el build: ninguna". | `build_env` quita `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` y `NO_PROXY` sin distinguir mayúsculas, como `tests/deps/helpers.py::run_probe`. `_is_loopback` solo acepta `localhost` exacto y las IP de loopback. | `tests/test_bundle_smoke_scripts.py`: 29 casos nuevos (proxies, loopback y nombres que no son loopback). |
| M1 | Baja | `engine/faro_engine/core/jobs/worker.py`, `queue.py` | Una cancelación pedida mientras corría `propose_save` se perdía. La tarea acababa en `waiting_approval` con la propuesta `pending`, o en nivel 0 en `succeeded` con una sugerencia aceptable. Incumplía §4.3. | Si el final deja trabajo abierto (`waiting_approval`, o `succeeded` con sugerencia) y se pidió cancelar, la tarea acaba `cancelled`/`user_cancelled`. `RunQueue.transition(unless_reason=CANCEL_REQUESTED)` comprueba la marca en la misma transacción, y `queue.cancel(include_running=True)` cierra la propuesta. Un `succeeded` sin sugerencia (acción ya hecha) no cambia. | `test_flow.py::test_cancelar_mientras_propone_no_deja_nada_abierto`, con niveles 0 y 1, cada uno con aviso al trabajador y solo con la marca en la base. Falla sin el arreglo. |
| T7 | Obs. | `engine/faro_engine/sites/service.py` | Si una cancelación o `asyncio.timeout` llegaba después del commit de `_insert`, `_undo_connect` borraba el secreto y revocaba, pero el sitio quedaba activo sin clave. | `connect` marca `insert_sent` y, al deshacer, borra también la fila (`repository.delete_site`, con la conexión en cascada) en el orden secreto, fila, revocar. El `site_id` es nuevo (`new_id()`), así que no puede borrar otro sitio. Si el borrado falla, solo queda en el log la clase del error. | `tests/sites/test_service.py`: plazo y cancelación tras el commit, y borrado que falla. |

### 2.1 Consecuencia de C1 en el producto: el límite de ritmo no hace fallar tareas

El trabajador del motor ejecuta las tareas de una en una. Con solo el límite de C1, un `catch_up` con más de 10 sitios habría fallado desde la tarea 11.ª. Por eso el rechazo por ritmo tiene su propio código (`agents.grant_rate_limited`), distinto de `agent.grant_denied`:

- **Al pedir la primera concesión:** la tarea vuelve a `queued` con el motivo `grant_rate_limited`, sin gastar nada. El trabajador espera unos 60 s antes de pedir otra, y la pausa, el apagado o la cancelación cortan la espera.
- **Al renovar a mitad de tarea:** la tarea queda `paused` (`grant_rate_limited`) y se vuelve a encolar para seguir desde el último checkpoint.
- **En la interfaz:** el motivo tiene texto propio en `features/agents/labels.ts` y en `locales/*/agents.json`, y el código de error tiene su mensaje en `locales/*/errors.json`.
- **Protocolo:** el código está en la spec (tabla del protocolo y trabajador), en ADR 0014 §1 y en la skill `tauri-sidecar-python`.
- **Pruebas:** `tests/jobs/test_worker_rate_limit.py`.

## 3. Revisado sin hallazgos

### 3.1 Núcleo Rust

- **Concesiones por ejecución:**
  - el orden de comprobaciones es el de ADR 0014 §1, y se vuelven a comprobar bajo el candado;
  - solo `get`;
  - `wp/<site>/token` solo con un sitio del perfil;
  - la pausa borra solo las concesiones `Run`;
  - ninguna operación de `engine-operations.json` concede `llm/*`.
- **Pausa:**
  - arranca en pausa;
  - un archivo ilegible deja en pausa;
  - escritura atómica, en el orden revocar → avisar → guardar → auditar.
- **Actividad (`engine://agents`):** esquema cerrado, 4 KB, 20/s, `emit_to("main")`; nunca registra contenido.
- **ACL, CSP y `--fake-llm`:**
  - ACL con 3 permisos y sin `core:default`;
  - CSP sin cambios desde F1a;
  - `--fake-llm` solo con `debug_assertions`.
- **Lanzador:** `env_clear` con lista permitida y rutas absolutas.
- **Protocolo:** líneas de 64 KB como máximo.

### 3.2 Motor Python

- **Clave de IA:**
  - se pide una vez por llamada lógica, después de `prepare`, de la pausa, del presupuesto y del tope;
  - se suelta en `finally`;
  - los errores no copian el cuerpo del proveedor.
- **LiteLLM endurecido:**
  - única importación;
  - `api_base` fijo, `trust_env=False`, sin redirecciones;
  - callbacks vacíos y entorno limpio;
  - herramientas rechazadas.
- **Checkpointer:** solo JSON, sin `pickle`.
- **Aprobaciones:**
  - atómicas;
  - el ejecutor relee la base dentro de la transacción;
  - `publish`/`spend` no son ejecutables.
- **Contenido remoto:** la regla `untrusted` está en `system` de los prompts v1.
- **Presupuesto con reintentos:** 150 títulos, `cl100k` real, tiempos agotados y salida inválida en cada llamada. Ninguna tarea pasó su `max_cost_micros`, y las tres terminaron en `agent.budget_exhausted`:

  | Proveedor | Gastado | Máximo |
  | --- | --- | --- |
  | Anthropic | 99 044 | 117 146 |
  | OpenAI | 53 142 | 61 762 |
  | Gemini | 36 864 | 41 500 |
- **Suites:** `tests/agents tests/jobs tests/llm tests/routes`, 1 077 pasan.

### 3.3 Interfaz, cadena de suministro, CI y empaquetado

- **Interfaz:**
  - sin HTML crudo, `eval`, `fetch` ni `console.*`;
  - todo pasa por `invoke`;
  - i18next sin `<Trans>`, con interpolaciones de listas cerradas.
- **Cadena de suministro:**
  - `cargo-audit` sin avisos (base del 2026-10-17);
  - las excepciones de `.cargo/audit.toml` son solo de Linux, comprobado con `cargo tree --target` en Windows y macOS (vacío) y en Linux (control positivo);
  - `uv.lock` con hash en sus 115 paquetes;
  - `cl100k_base` con el SHA-256 esperado.
- **Generador de `agent-grants.json`:** rechaza `db/*`, `oauth/*`, accesos que no sean `get` y plazos fuera de 60–900.
- **Workflows:**
  - sin `pull_request_target`, `workflow_run`, secretos ni runners propios;
  - acciones fijadas por SHA;
  - `persist-credentials: false`.
- **Empaquetado del motor:** sin `dev` ni módulos de prueba; `--fake-llm`, `--dev` y `--allow-local-sites` salen con código 2 cuando está congelado.
- **Script manual:** `scripts/manual_site_summary_tokens.py` sin hallazgos (pedido por T12).

### 3.4 CI de Windows (condición de T12)

`engine (windows-latest)`, `core (núcleo Rust, Windows)`, `audit` y `ci-ok` están en verde en `46e98c5` y en `89e15dc` (consultado con la API de check-runs de GitHub). Condición cerrada.

## 4. Condiciones anteriores

**Cerradas en T13:**
- T5-1, en lo que le tocaba de C1;
- T5-7 (a, b, c);
- la observación de T7 sobre `sites/service.py`;
- la condición de Windows de T12.

**Confirmadas como cerradas en el código actual:**
- T2-10, T2-11, T2-12 y T2-17;
- T6-C1, T6-C2, T6-C5 y T6-C6;
- hallazgo 3 de T7;
- T8-C1 y T8-C2;
- T10 1–4;
- T11-C2 y la parte de T11-C3 que no es de release;
- T3 1–7.

**Siguen abiertas, para T14 o la lista manual:**
- **T5-5:** el texto principal de ADR 0014 §2 aún tiene el orden antiguo (T14).
- **T9-C1:** `LlmService` reserva `ceil(caracteres/3)` y no usa `input_tokens_bound` (T14, `capa-llm`).
- **T6-C4, resto:** prueba por proveedor con `respx` antes de habilitar herramientas (hoy están rechazadas).
- **T6-C3:** mediciones M1 y M2 de la [lista manual](2026-10-14-f1b-verificacion-manual.md) y Gemini.
- **T10 → T14:**
  - `locales/es/errors.json` aún dice "Reinicia Faro" para `agents.control_unavailable`;
  - `agents_control_state` no devuelve `saved`.
- **Pruebas manuales del usuario:**
  - la lista de F1b;
  - OpenAI y Gemini (T2 §8);
  - `manual_tls_check.py` y WordPress real por HTTPS (T2b §4).

**Pendientes de release, que no bloquean F1b:**
- conectar `SidecarLauncher` con `externalBin` y la verificación de integridad;
- T11-C1: carpeta de instalación no escribible, o un manifiesto de la carpeta entera; mientras tanto, T5-3 y T2-14 siguen parciales;
- T12-N1: lista permitida de entradas `s` del CArchive;
- comprobación del contenido sobre el artefacto firmado;
- build sin red a nivel del sistema operativo;
- excluir el proxy de LiteLLM (T11 §10.4);
- archivo de avisos de terceros (T2 §3);
- que el release no restaure cachés escritas por PR;
- T5-6: en la fase macOS, `KeyringStore::exists` sin leer el valor.

**Límites conocidos, sin acción:**
- `GrantGuard::drop` borra por clave sin mirar la serie. Solo afecta a la concesión del propio motor y no da acceso extra.
- Si la cola de 32 del canal de secretos está llena, un `run_grant_release` se descarta. La concesión vive hasta caducar (900 s como máximo).

## 5. Segunda revisión (arreglos de §2 sin commit, sobre `89e15dc`)

**Veredicto:** APROBADO CON CAMBIOS.
- C1, C2, I1, M1 y la observación de T7 quedan cerrados, cada uno con una prueba que fallaría sin el arreglo.
- `agents.grant_rate_limited` no permite saltarse la pausa ni la Bandeja, no permite gastar de más y no abre ningún canal de información: la respuesta no lleva detalles.
- Borrar la fila al deshacer no puede alcanzar otro sitio: el `site_id` es nuevo y el `DELETE` va por clave primaria.
- Pausa, reanudación, reinicios y apagado no pasan por el tope común. Al detenerse el motor, el resumen de lo que no se registró sale siempre.
- **Cerrojo de T2b (`net/tls.py:171-252`): confirmado.**
  - `_faro_lock` cubre toda la ventana degradada de `wrap_bio` y `wrap_socket`, `do_handshake` incluido, que es donde `truststore` lee `check_hostname`.
  - `OP_NO_RENEGOTIATION` se aplica antes de cualquier `wrap`, también en los contextos inyectados y en el respaldo con `certifi`.
  - Se contrastó con `truststore` 0.10.4. Así se cierra lo que quedaba pendiente en el [informe de T2b](2026-10-06-f1b-t2b-truststore.md).

| Id | Sev. | Problema | Arreglo | Pruebas |
| --- | --- | --- | --- | --- |
| H1 | Media | Si una tarea con cancelación pedida topaba al renovar su concesión con `agents.grant_rate_limited` (o con `agents.paused`), quedaba `paused` y se perdía la marca `cancel_requested`. Al reanudarse, la tarea seguía gastando y podía dejar en la Bandeja una propuesta que el usuario quería cancelar. Lo mismo pasaba en una ventana mínima de `_grant_failed`. | `_Ending.yields_to_cancel`: toda salida de `running` que dejaría la tarea viva (`paused`, `queued`, `waiting_approval`, `succeeded` con sugerencia) se hace con `unless_reason=CANCEL_REQUESTED` en la misma transacción y, si la marca está puesta, acaba `cancelled` con sus propuestas cerradas. `_requeue` pasa por `_finish`. `stop.reason == "user_cancelled"` gana sobre esos finales. Cambio de comportamiento: un apagado con la cancelación pedida deja la tarea `cancelled` en vez de `interrupted`. | `test_cancelar_mientras_la_renovacion_se_deniega_no_vuelve_a_la_cola` (ritmo y pausa, con y sin aviso), `test_cancelar_entre_comprobar_y_devolver_a_la_cola` |
| H2 | Baja | `_not_started` vivía solo en memoria. Si se reiniciaba el motor, una tarea devuelta a la cola sin haber empezado se tomaba como reanudada y se saltaba la comprobación previa del tope diario. | Al devolverla a la cola en su primer arranque se borra `started_at` en la misma transacción (`reset_started`, `runs.clear_started_at`). La base es la única fuente de verdad. | `test_tras_reiniciar_sigue_siendo_primer_arranque`, `test_clear_started_at_only_in_its_status` |
| H3 | Baja | En macOS y Windows, sin variables de proxy, `urllib` lee el proxy del sistema (el registro o SystemConfiguration). | `build_env` quita toda variable `*_PROXY`, no solo las cuatro estándar: en el contenedor había `GLOBAL_AGENT_HTTPS_PROXY`. Además fija `NO_PROXY=*` y `no_proxy=*`, así que nunca se consulta el proxy del sistema. | `test_el_entorno_del_build_no_lleva_proxies`, `test_con_el_entorno_del_build_urllib_no_consulta_el_proxy_del_sistema` |

**Límite conocido:** mientras el trabajador espera unos 60 s por el límite de ritmo, no procesa otras tareas, tampoco las de otros agentes. Con un solo tipo de agente (F1b) da igual. Se revisará cuando haya más de uno.

## 6. Tercera revisión

**Veredicto final de T13: APROBADO.**
- H1, H2 y H3 quedan cerrados y no hay nada nuevo. Las pruebas `tests/jobs`, `tests/agents`, `tests/store/test_runs.py` y `tests/test_bundle_smoke_scripts.py` pasan: 716.
- El cambio de comportamiento de H1 (apagado con cancelación pedida → `cancelled`) es aceptable: respeta lo que pidió el usuario.
- La espera de unos 60 s es un tema de disponibilidad, no de seguridad. Como mejora de experiencia, el trabajador podría pasar a la siguiente tarea de otro tipo de agente mientras espera.

## 7. Comandos ejecutados (2026-10-19, este contenedor, Linux)

| Capa | Comando | Resultado |
| --- | --- | --- |
| Motor | `uv run --locked ruff check .`, `ruff format --check .` | sin errores; 277 archivos formateados |
| Motor | `uv run --locked mypy .` y `mypy --platform win32 .` | sin errores en 272 archivos |
| Motor | `uv run --locked pytest` (suite completa, con cobertura) | 2 820 pasan, 2 omitidas (`SSL_CERT_FILE` en Linux, sin IPv6); cobertura 99,89 % |
| Núcleo | `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` | sin avisos |
| Núcleo | `cargo test` | 348 pasan y 2 ignoradas (llavero real); `tests/acl.rs`, 16 |
| Interfaz | `npm run lint`, `npm run typecheck`, `npm run test` | sin errores; 34 archivos, 689 pruebas |

Para ejecutar `cargo test` en Linux hacen falta las dependencias de sistema de Tauri (`libgtk-3-dev`, `libwebkit2gtk-4.1-dev`, `libayatana-appindicator3-dev`, `librsvg2-dev`).
