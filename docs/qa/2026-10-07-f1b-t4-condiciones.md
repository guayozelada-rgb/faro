# F1b T4 · Condiciones de la revisión de T4 (revisión de seguridad, 2026-10-07)

La revisión de seguridad de T4 (migración `0002_agents`, repositorios de `core/store` y claves de auditoría) dio **APROBADO CON CAMBIOS**. Los cambios se corrigieron en la misma rama, antes de publicar `0002_agents.sql` en `main`:

| Hallazgo | Corrección |
| --- | --- |
| Medio: `insert_approval` aceptaba `approved` con `decided_by = 'rule'` en `publish` y `spend`. | `RULE_DECIDABLE_SIDE_EFFECTS = {"internal"}` en `core/store/approvals.py`, comprobado en `insert_approval` y `decide_approval`; `CHECK (decided_by IS NOT 'rule' OR side_effect = 'internal')` en `approvals`. |
| Bajo: `CHECK` del esquema. | `max_cost_micros > 0` en `agent_runs`; `secret_ref GLOB 'llm/*'` en `agent_steps` (admite `NULL`), `credential_usage` y `credential_limits`; `type = 'json'` en `agent_checkpoints` y `agent_checkpoint_writes`. Se usa `GLOB` en lugar de `LIKE` porque `LIKE` no distingue mayúsculas (`LLM/…` pasaría). |
| Bajo: claves de `details` de auditoría. | `approval_id`, `decision` y `level` solo en su acción del motor (`ACTION_DETAIL_KEYS` de `core/audit.py`), nunca en eventos del núcleo; `decision` ∈ `approve`, `reject`; `level` de `0` a `3`; `approval_id` UUID. |
| Bajo: `set_rule(limits)` aceptaba cualquier `Mapping`. | Solo `None` o vacío (`LIMITS_CONFIGURABLE = False`). |
| Bajo: componer transacciones. | `core/store/run_control.py`: `decide_and_requeue` y `cancel_run`, cada una en una transacción `atomic`. |

**Valores de `type` en `langgraph-checkpoint` 4.2.0.** `JsonPlusSerializer.dumps_typed` produce `null`, `bytes`, `bytearray`, `msgpack` y, con `pickle_fallback`, `pickle`; `loads_typed` acepta además `json`. ADR 0015 §2 fija para el checkpointer propio `type = "json"` y rechazo de todo lo demás, así que el `CHECK` admite solo `json`. El serializador de T8 representa `None` y los bytes dentro de JSON (no con los tipos `null` o `bytes` de LangGraph).

## Condiciones para las tareas siguientes

1. **T8, checkpointer.** `FaroCheckpointSaver` rechaza **al leer** cualquier `type` distinto de `json` (sin intentar deserializarlo), con dos pruebas que terminan la tarea en `failed` con `agent.state_unreadable`: un `BLOB` `pickle` con `type = 'pickle'` (insertado con `PRAGMA ignore_check_constraints = ON` para saltar el `CHECK`), y un `BLOB` `pickle` con `type = 'json'`. Nunca se configura `pickle_fallback` ni se usa `JsonPlusSerializer` para leer. `core/store/checkpoints.py` ya rechaza escribir otro `type` (`CHECKPOINT_TYPE`).
2. **T7 y T8, funciones compuestas.** Las rutas y el trabajador usan `core/store/run_control.py` y no encadenan las operaciones sueltas:
   - `decideApproval` → `decide_and_requeue`. `outcome`: `not_found` → `approval.not_found` (404); `already_decided` → `approval.already_decided` (409); `expired` → `approval.expired` (410); `run_not_waiting` → la propuesta existe pero la tarea aún está `running` (entre el nodo que propone y el `interrupt`). T7 elige el código (409 que se puede reintentar) y lo añade al catálogo.
   - `cancelAgentRun` → `cancel_run`. `not_found` → `agent.run_not_found`; `not_cancellable` → `agent.not_cancellable`; `running` → marcar la tarea para que el trabajador se detenga, y el trabajador llama a `cancel_run(..., include_running=True)` en el límite entre pasos.
   - Cancelar una tarea pasa sus propuestas `pending` **y** las `approved` sin ejecutar a `cancelled`. Esto añade la transición `approved → cancelled` a ADR 0016 §5 (que hoy solo lista `executed` y `failed`). `arquitecto` debe reflejarla en el ADR y en la spec.
3. **F5, caducidad de lo aprobado.** Antes de habilitar `spend`, decidir si una aprobación `approved` que lleva tiempo sin ejecutarse (por ejemplo, con los agentes en pausa) caduca, y con qué plazo. Hoy `expire_due_approvals` solo caduca las `pending`.
4. **F4 y F5, `limits`.** Antes de exponer el nivel 2 para una acción, definir el esquema de `limits` de esa acción (claves, tipos y rangos), validarlo con Pydantic en la ruta y en `set_rule`, y quitar `LIMITS_CONFIGURABLE = False`.
5. **F4 y F5, reglas para `publish` y `spend`.** Para que una regla decida `publish` o `spend` hacen falta la decisión del usuario de esa fase, ampliar `RULE_DECIDABLE_SIDE_EFFECTS` y una migración nueva que cambie el `CHECK` de `approvals` (la 0002 ya no se podrá editar).
6. **T5, auditoría del núcleo.** `DetailKey` de Rust (`secrets/audit.rs`) puede añadir `agent_kind` para los eventos `agent.grant_*` y `agents.*`, pero nunca `approval_id`, `decision` ni `level`: el motor rechazaría el evento.
7. **Spec F1b §6.** La copia del SQL de la spec no tiene aún los `CHECK` nuevos. `arquitecto` debe sincronizarla con `0002_agents.sql`.
