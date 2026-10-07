# F1b T3 · Condiciones para T5 (revisión de seguridad, 2026-10-07)

La revisión de seguridad de T3 (PR #37) dio **APROBADO**. El núcleo Rust es quien decide las concesiones por ejecución (ADR 0014), así que su validación de `agent-grants.json` es la barrera real. T5 debe cumplir lo siguiente:

1. **Validación propia en Rust, no solo en una prueba.** El núcleo lee el `include_str!` con tipos cerrados al arrancar el `SecretBroker`:
   - `#[serde(deny_unknown_fields)]` en la entrada y en el secreto;
   - `max_grant_seconds` como entero sin signo, de modo que se rechacen `900.0`, los negativos y lo que no sea numérico;
   - `access` como enum de un solo valor `Get`;
   - `ref` como enum cerrado de las cuatro plantillas.

   Si la tabla no es válida, no se concede nada a ningún agente: falla cerrado. Una prueba debe rechazar los 56 vectores inválidos y aceptar los 4 válidos de `packages/shared/fixtures/agent-grants-cases.json`.
2. **Claves duplicadas.** Se deserializa a structs con derive (que dan error de campo duplicado), nunca a `serde_json::Value` para leerlo después. Hace falta una prueba con `access` duplicado.
3. **Comprobaciones en ejecución, en el orden de ADR 0014 §1:**
   1. pausa;
   2. que `agent` esté en la tabla;
   3. que `run_id` sea un UUID canónico y no tenga otra concesión activa;
   4. que exista `site_id` si `requires_site` lo exige, y que esté en el índice del perfil activo;
   5. que `provider` esté en la lista y `llm/<provider>/default` esté en la tabla del agente;
   6. que no haya más de 4 concesiones activas.

   Casos especiales:
   - con `site_id` presente y `requires_site: false`, nunca se concede `wp/*`;
   - con `provider: null`, se deniega;
   - un agente sin plantilla `llm/*` no recibe ninguna clave de IA.
4. **La concesión sale solo de la tabla y de la petición validada:**
   - `llm/<provider>/default: get` y `wp/<site_id>/token: get`, con `{site_id}` sustituido por un UUID canónico del índice;
   - sin `{new}` ni `set`, `create` o `delete`;
   - se revalida después de tomar el candado del llavero y queda atada a la generación del motor y al perfil;
   - `db/*` no se concede nunca.
5. **Caducidad:**
   - el núcleo usa `min(max_grant_seconds, 900)`;
   - la concesión se renueva solo para el mismo `run_id`, después de caducar la anterior y repitiendo todas las comprobaciones;
   - la pausa borra todas las concesiones de ejecución;
   - `run_grant_release` solo borra la concesión de su propio `run_id`.
6. **Paridad del archivo incrustado:** la tabla que lee Rust coincide con `packages/shared/agent-grants.json`, que queda fijada a `[]` hasta T9.
7. **Denegaciones:** se responde `agent.grant_denied` sin motivo. El motivo va solo al log y a la auditoría, sin valores.

## Hallazgos bajos aceptados
- **Números escritos de otra forma.** `JSON.parse` acepta `900.0`, `9e2` y `6e1` como enteros, mientras que Python los rechaza. Hoy no se puede explotar: la entrada de JS es la salida ya validada de Python, y un `900.0` escrito a mano lo detectan la prueba de forma canónica y el diff de la CI. La condición 1 cierra esto en el núcleo.
- **Revisión obligatoria solo por casilla.** La revisión de `revisor-seguridad` sobre `agent-grants.json` depende de una casilla de la plantilla de PR. No se añade `CODEOWNERS` con revisión obligatoria porque el repositorio tiene un solo responsable, y GitHub no deja aprobar el propio PR.
