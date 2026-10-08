# F1b T5 · Condiciones de la revisión de T5 (revisión de seguridad, 2026-10-08)

La revisión de seguridad del PR #40 (T5: protocolo v3 con concesiones por ejecución, pausa global y actividad de agentes) dio **APROBADO CON CAMBIOS**. Los cambios se corrigieron en la misma rama:

| Hallazgo | Corrección |
| --- | --- |
| Medio: la cola de auditoría del núcleo no tenía límite y `forward` esperaba el stdin sin plazo. | Búfer compartido acotado a 500 eventos; `record` nunca bloquea y descarta el más viejo (contado y registrado muestreado). `StdinWriter` con plazo de atasco de 10 s: al superarlo deja de aceptar líneas y el supervisor reinicia el motor. Auditorías repetidas de `malformed` y `not_active` agregadas con `details.count`. Prueba con un motor que inunda stdout y no lee stdin. |
| Bajo: logs sin muestreo. | La primera y una de cada 100 (con el total): líneas de actividad inválidas, líneas no reconocidas, solicitudes sin `id` o malformadas, liberaciones malformadas o inexistentes y descartes de la cola del canal de secretos. Tope de 64 MiB por día (UTC) en el log del núcleo, con una línea de aviso al llegar. |
| Bajo: orden de la pausa. | Revoca en memoria, avisa al motor y después guarda (ADR 0014, actualización 2026-10-08). |
| Bajo: `prepare` y la generación. | Cada concesión tiene un número de serie; `prepare` exige el mismo que vio `check_grant` y vuelve a comprobar referencia y operación. |
| Bajo: `providers_with_key` leía las claves. | `SecretStore::exists`: en Windows lee solo los atributos (`get_attributes`); en macOS `keyring` 3.6 no lo permite y el valor se lee en memoria que se borra al soltarse. |
| Bajo: `agent` fuera de la tabla. | El núcleo descarta la actividad de un `agent` que no esté en `agent-grants.json`. |
| Bajo: liberación inexistente auditada como `ok`. | Se audita como `denied` (`not_active`), agregada. |
| Bajo: prueba de `env_clear`. | `engine_command` arma el comando del motor; la prueba lanza con él un proceso real que imprime su entorno y comprueba que una variable del padre fuera de la lista no llega. |

## Condiciones para las tareas siguientes

1. **Auditoría frente a un motor comprometido.** El log `tracing` del núcleo (`faro.<fecha>.log`) es la fuente fiable de lo que decidió el núcleo: `audit_log` vive en la base del motor, y un motor comprometido puede no insertar o falsear filas. Cualquier investigación de un incidente con el motor parte del log del núcleo.
2. **T11, lanzador de release.** El lanzador del sidecar PyInstaller debe partir de `engine::launcher::engine_command` (o repetir `env_clear()` con `ENGINE_ENV_ALLOWLIST`) y tener su propia prueba con un proceso real.
3. **T7 y T11, condición 14 (pendiente).** Directorio de trabajo del motor fijo y no escribible por el usuario. Hoy el lanzador de desarrollo usa `apps/engine`; el de release debe fijar uno que cumpla esta condición.
4. **T10, interfaz de actividad.** La interfaz traduce `status`, `step` y `error_code` de `engine://agents` **siempre por catálogo** (claves conocidas → texto en español) y nunca muestra el valor en crudo; uno desconocido se muestra con un texto genérico. El núcleo solo garantiza la forma de identificador y que `agent` sea de la tabla.
5. **ADR 0014 §2.** `arquitecto` debe ratificar el nuevo orden de la pausa (actualización 2026-10-08 del ADR) y reflejarlo en la spec F1b.
