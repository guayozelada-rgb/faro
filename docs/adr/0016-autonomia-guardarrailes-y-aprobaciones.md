# ADR 0016 — Autonomía de los agentes, guardarraíles fijos y modelo de aprobaciones

- **Fecha:** 2026-10-05
- **Estado:** Aceptado (2026-10-05, con la aprobación de la spec F1b); actualizado el 2026-10-07 (F1b T4, ver al final)
- **Spec:** [F1b — Capa de IA y motor de agentes](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md)
- **Relacionados:** ADR 0014 (pausa global y concesiones por ejecución), ADR 0015 (motor de agentes)
- **Reglas de `CLAUDE.md` que concreta:** 3 (nada que publique o gaste sin autonomía y Bandeja) y 4 (campañas en pausa)

## Contexto

Los agentes van a preparar contenido, cambiar metadatos de WordPress y tocar campañas de Google Ads. La regla 3 de `CLAUDE.md` exige que nada que publique o gaste dinero se ejecute sin pasar por las reglas de autonomía y la Bandeja de aprobación. F1b no publica ni gasta fuera de la llamada al LLM, pero define ahora el modelo de datos y el mecanismo para que F2–F5 no tengan que rediseñarlo, y lo ejercita con un agente de demostración. La Bandeja completa (editar, aprobar por lotes, historial, deshacer) es de F4.

Falta decidir: qué niveles hay y qué hace cada uno; qué límites no se pueden configurar; cómo se representa una propuesta; cómo un agente queda parado días esperando una decisión; y si el costo de las llamadas al LLM es "gasto" a efectos de la regla 3.

## Decisión

### 1. Toda acción con efectos se declara en un catálogo

Cada acción que un agente puede ejecutar fuera de su propio cálculo se declara en `faro_engine/agents/actions.py` con un `action_kind` y una **clase de efecto**:

| Clase | Ejemplos | ¿Pasa por autonomía? |
| --- | --- | --- |
| `read` | leer contenido del sitio, consultar métricas | No. Sin efectos. |
| `internal` | guardar un resultado dentro de Faro (resumen del sitio en F1b) | Sí |
| `publish` | crear o actualizar borradores y metadatos en WordPress (F4) | Sí |
| `spend` | crear o cambiar campañas y presupuestos de Google Ads (F5) | Sí |

No existe la clase "borrar": **ninguna** acción que borre contenido, campañas o datos del usuario puede declararse. El registro de acciones falla al arrancar si alguna lo intenta, y una prueba lo comprueba.

Las acciones `internal`, `publish` y `spend` **nunca** se ofrecen al modelo como herramientas: el modelo propone (datos estructurados) y el motor ejecuta después de decidir la autonomía, con una clave de idempotencia y guardando el valor anterior para poder deshacer.

### 2. Niveles de autonomía por agente y por sitio

| Nivel | Nombre en la interfaz | Qué hace con una acción `internal`/`publish`/`spend` |
| --- | --- | --- |
| 0 | Solo sugerir | No crea propuesta ejecutable: deja el resultado como sugerencia en la tarea. Si el usuario la acepta a mano, se crea la aprobación ya decidida por él (`decided_by = user`) y el ejecutor la aplica como cualquier otra. |
| 1 | Preparar y pedirte OK (**predeterminado**) | Crea una aprobación y pausa el agente hasta que decidas. |
| 2 | Actuar con límites | Ejecuta si cumple los límites de la regla (`limits`, por acción: cantidad por día, monto); si no, pide aprobación. |
| 3 | Piloto automático | Ejecuta dentro de los guardarraíles y lo registra. |

- Regla efectiva = la del agente **y** sitio si existe; si no, la del agente para todos los sitios; si no, nivel 1.
- F1b solo permite niveles 2 y 3 para acciones `internal`. Para `publish` y `spend` el máximo será el que fije la spec de cada fase (por defecto, 1), con su propia decisión del usuario. Se aplica en código y en la base (Actualización C).
- Cambiar una regla queda en `audit_log` (`autonomy.changed`, actor `user`). Subir a nivel 3 pide confirmación en la interfaz.

### 3. Guardarraíles fijos (ningún nivel los salta, se aplican en código)

1. **Nunca borrar** contenido, campañas ni datos del usuario (§1).
2. **Tope de gasto diario por clave de IA**, comprobado antes de cada llamada al LLM (ADR 0015); y, cuando exista, tope diario de gasto publicitario por cuenta.
3. **Nada que publique o gaste** se ejecuta sin la decisión de autonomía de este ADR; el ejecutor de acciones es el único camino y comprueba que la aprobación (o la regla de nivel 2–3) existe y no se usó.
4. **Campañas de Google Ads siempre en PAUSA** al crearse (regla 4 de `CLAUDE.md`), con o sin aprobación.
5. **Pausa global** (ADR 0014): con los agentes pausados no se ejecuta ninguna acción, tampoco una ya aprobada; queda pendiente hasta reanudar.

### 4. El costo del LLM no pasa por la Bandeja

Las llamadas al LLM cuestan dinero del usuario, pero pedir aprobación por cada llamada haría inservibles a los agentes. Se controlan así, y con eso se considera cumplida la regla 3 para este tipo de gasto:

- el usuario ve el **costo estimado y el máximo** antes de lanzar una tarea y la lanza él (o crea la programación viendo ese estimado);
- cada tarea tiene un **presupuesto de tokens** que no puede pasar (el máximo mostrado se calcula con él);
- cada clave tiene un **tope diario** configurable;
- todo el gasto queda registrado por paso, por tarea y por clave y día, y es visible.

Una tarea programada cuyo máximo ya no cabe en lo que queda del tope del día no empieza: queda en cola hasta el día siguiente con aviso.

### 5. Aprobaciones (`approvals`) y pausa del grafo

- Una propuesta es una fila de `approvals` con: tarea y paso de origen, sitio, `agent_kind`, `action_kind`, clase de efecto, nivel aplicado, `payload` JSON validado por un esquema propio de cada `action_kind` (lo que se ejecutará y su vista previa), `evidence` (por qué), costo estimado si aplica, `idempotency_key` única, `previous_value` (para deshacer), `expires_at` (14 días por defecto) y estado.
- Estados: `pending` → `approved` | `rejected` | `expired` | `cancelled`; `approved` → `executed` | `failed` | `cancelled` (esta última añadida el 2026-10-07, ver Actualización A). Cada transición es una actualización condicional (`UPDATE … WHERE status = 'pending'`): decidir dos veces responde `approval.already_decided`.
- **Pausa:** el nodo que propone crea la fila y llama a `interrupt({"approval_id": …})`. LangGraph guarda el checkpoint; la tarea pasa a `waiting_approval` y libera su concesión (ADR 0014). No queda nada en memoria: puede esperar días y sobrevivir a reinicios.
- **Reanudación:** `decideApproval` solo cambia el estado y vuelve a encolar la tarea; el trabajador la retoma con `Command(resume={"approval_id", "decision"})` y una concesión nueva. El nodo ejecutor relee la aprobación de la base (nunca confía en lo que traiga el estado del grafo), comprueba `approved` y que no se ejecutó, ejecuta con la clave de idempotencia y marca `executed`.
- **Caducidad:** al arrancar y una vez al día se marcan `expired` las vencidas y sus tareas pasan a `cancelled` con `approval.expired`.
- Rechazar no es un error: la tarea termina `succeeded` con la propuesta `rejected` y el resultado sigue visible.

## Consecuencias

- Toda fase con acciones nuevas añade su `action_kind`, su esquema de `payload` y su clase de efecto, y decide en su spec el nivel máximo permitido.
- La Bandeja de F4 se construye sobre esta tabla sin migración incompatible (editar = nueva columna `edited_payload` o versión de `payload`, a decidir en F4).
- Los límites del nivel 2 (`limits`) se definen por acción en la fase que la introduzca; en F1b la acción `internal` de demostración no tiene límites configurables.
- El costo del LLM queda fuera de la Bandeja por diseño (§4); si el usuario quisiera aprobar el gasto de IA tarea a tarea, se cambiaría este ADR.

## Actualización (2026-10-07, F1b T4)

Decisiones tomadas al implementar la migración `0002_agents` y los repositorios de `core/store`, y en su revisión de seguridad ([condiciones de T4](../qa/2026-10-07-f1b-t4-condiciones.md)). Completan §2 y §5; el resto no cambia.

### A. §5: cancelar una tarea cancela también lo aprobado sin ejecutar (ratificado)

Los estados de §5 quedan así:

- `pending` → `approved` | `rejected` | `expired` | `cancelled`
- `approved` → `executed` | `failed` | **`cancelled`**

`approved → cancelled` solo ocurre al cancelar la tarea (`cancelAgentRun`, o el trabajador al detener una tarea marcada) y solo si la aprobación no se ejecutó (`executed_at IS NULL`). Lo hace `core/store/run_control.py::cancel_run`, que cambia la tarea a `cancelled` y sus propuestas abiertas (`pending`, o `approved` sin ejecutar) a `cancelled` **en una sola transacción**.

Por qué:

- Una tarea cancelada no se reanuda nunca, así que su nodo ejecutor no llegará a correr. Sin esta transición quedaría una aprobación `approved` colgada de una tarea `cancelled`, que `get_approval_for_execution` seguiría devolviendo como ejecutable: cualquier camino futuro (Bandeja de F4, un reintento, un error de programación) podría aplicar la acción de una tarea que el usuario canceló. Eso choca con la regla 3 de `CLAUDE.md`: cancelar es la última palabra del usuario.
- `failed` no sirve: no se intentó nada y mezclaría cancelaciones con errores reales en la Bandeja y en las métricas. `executed` sería falso.
- Hacerlo en la misma transacción que la tarea evita las dos mezclas a medias: tarea `cancelled` con aprobación ejecutable, o tarea en `waiting_approval` con su propuesta ya decidida.

Condición para F4 y F5 (acciones con efecto fuera de Faro): una aprobación `approved` cuya ejecución **puede haber empezado** (el ejecutor llamó al sitio o al relay y el motor se cerró antes de marcar `executed`) no se puede pasar a `cancelled` a ciegas: la acción quizá ya se aplicó. Antes de habilitar `publish` o `spend`, la spec de esa fase decide cómo marcar "ejecución en curso" y cómo comprobarla con la clave de idempotencia al cancelar o al recuperar. En F1b no hace falta: la única acción es `internal` y el ejecutor guarda el resultado y marca `executed` en la misma transacción.

### B. §5: rechazar vuelve a encolar la tarea, igual que aprobar (ratificado)

`decideApproval` con `reject` hace lo mismo que con `approve`: la propuesta pasa a `rejected` y la tarea de `waiting_approval` a `queued` (prioridad de reanudada), en una transacción (`run_control.py::decide_and_requeue`). El trabajador la reanuda con `Command(resume={"approval_id", "decision": "reject"})`, el grafo sigue por su rama de rechazo y la tarea termina `succeeded` con la propuesta `rejected` y el resultado visible. No se cancela la tarea al rechazar.

Por qué:

- Es lo que ya decían §5 ("rechazar no es un error: la tarea termina `succeeded`") y la spec F1b §4.3 (`waiting_approval → queued` al decidir). Cancelar mostraría la tarea como "Cancelada por ti" (`user_cancelled`), que no es lo que hizo el usuario, y escondería el resultado que sí quiere ver.
- Un solo camino para decidir: la decisión solo cambia estados y es el grafo quien cierra la tarea (paso de aprobación cerrado, último checkpoint, `run_status`). Los agentes de F2–F5 pueden necesitar hacer algo al rechazar (por ejemplo, proponer otra versión), y eso solo es posible si el grafo se reanuda.

Condiciones para T8 y los agentes: la rama de rechazo no llama al LLM ni ejecuta acciones (no gasta), salvo que la spec del agente diga otra cosa y lo incluya en su costo máximo. Con los agentes en pausa, la tarea rechazada espera en la cola como cualquier otra; se acepta, porque no tiene efectos pendientes.

### C. §2: una regla solo decide efectos `internal`

Que una regla de nivel 2 o 3 apruebe sola (`decided_by = 'rule'`) solo vale para acciones `internal`. Se comprueba en dos sitios:

- en código, `RULE_DECIDABLE_SIDE_EFFECTS = {"internal"}` (`core/store/approvals.py`), en `insert_approval` y `decide_approval`;
- en la base, `CHECK (decided_by IS NOT 'rule' OR side_effect = 'internal')` en `approvals` (`0002_agents.sql`).

Las aprobaciones decididas por el usuario (`decided_by = 'user'`, incluida la sugerencia de nivel 0 aceptada a mano) valen para cualquier clase. Para que una regla decida `publish` (F4) o `spend` (F5) hacen falta, las tres: la decisión del usuario en la spec de esa fase, ampliar `RULE_DECIDABLE_SIDE_EFFECTS` y una **migración nueva** que cambie el `CHECK` de `approvals` (la 0002 ya está publicada y no se edita). Mientras tanto, `autonomy_rules.limits` solo admite `{}` (`LIMITS_CONFIGURABLE = False`); cada fase define el esquema de `limits` de sus acciones y lo valida con Pydantic antes de exponer el nivel 2.

Pendiente para F5: decidir si una aprobación `approved` que lleva tiempo sin ejecutarse (por ejemplo, con los agentes en pausa) caduca y con qué plazo. Hoy solo caducan las `pending`.

### D. Auditoría de autonomía y aprobaciones

Las claves de `details` de `autonomy.changed`, `approval.decided` y `approval.executed` son propias de cada acción y tienen forma exacta (`ACTION_DETAIL_KEYS` y `DETAIL_VALUE_PATTERNS` en `core/audit.py`). Se describen en [ADR 0010, actualización 2026-10-07](0010-protocolo-nucleo-motor-secretos-y-auditoria.md#actualización-2026-10-07-f1b-t4-claves-de-auditoría-por-acción).
