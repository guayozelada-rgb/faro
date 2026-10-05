# ADR 0016 — Autonomía de los agentes, guardarraíles fijos y modelo de aprobaciones

- **Fecha:** 2026-10-05
- **Estado:** propuesto
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
- F1b solo permite niveles 2 y 3 para acciones `internal`. Para `publish` y `spend` el máximo será el que fije la spec de cada fase (por defecto, 1), con su propia decisión del usuario.
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
- Estados: `pending` → `approved` | `rejected` | `expired` | `cancelled`; `approved` → `executed` | `failed`. Cada transición es una actualización condicional (`UPDATE … WHERE status = 'pending'`): decidir dos veces responde `approval.already_decided`.
- **Pausa:** el nodo que propone crea la fila y llama a `interrupt({"approval_id": …})`. LangGraph guarda el checkpoint; la tarea pasa a `waiting_approval` y libera su concesión (ADR 0014). No queda nada en memoria: puede esperar días y sobrevivir a reinicios.
- **Reanudación:** `decideApproval` solo cambia el estado y vuelve a encolar la tarea; el trabajador la retoma con `Command(resume={"approval_id", "decision"})` y una concesión nueva. El nodo ejecutor relee la aprobación de la base (nunca confía en lo que traiga el estado del grafo), comprueba `approved` y que no se ejecutó, ejecuta con la clave de idempotencia y marca `executed`.
- **Caducidad:** al arrancar y una vez al día se marcan `expired` las vencidas y sus tareas pasan a `cancelled` con `approval.expired`.
- Rechazar no es un error: la tarea termina `succeeded` con la propuesta `rejected` y el resultado sigue visible.

## Consecuencias

- Toda fase con acciones nuevas añade su `action_kind`, su esquema de `payload` y su clase de efecto, y decide en su spec el nivel máximo permitido.
- La Bandeja de F4 se construye sobre esta tabla sin migración incompatible (editar = nueva columna `edited_payload` o versión de `payload`, a decidir en F4).
- Los límites del nivel 2 (`limits`) se definen por acción en la fase que la introduzca; en F1b la acción `internal` de demostración no tiene límites configurables.
- El costo del LLM queda fuera de la Bandeja por diseño (§4); si el usuario quisiera aprobar el gasto de IA tarea a tarea, se cambiaría este ADR.
