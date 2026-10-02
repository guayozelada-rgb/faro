# ADR 0009 — Base de datos local cifrada: formato, librería, perfil, llave y compatibilidad de versiones

- **Fecha:** 2026-09-29
- **Estado:** aceptado. Librería elegida: **`sqlcipher3-wheels`** (actualización del 2026-10-01, al final)
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Skills:** `migraciones-sqlite`, `llavero-y-cifrado`, `tauri-sidecar-python`

## Contexto

F1a necesita guardar datos por primera vez (sitios, conexiones, auditoría). Las skills fijan que la base es SQLite cifrada, que solo la abre el motor y que su llave vive en el llavero del SO y llega por stdin. Quedaban abiertas cinco cosas que afectan a todas las fases: qué librería de Python usar, qué formato de archivo, cómo sabe el núcleo qué perfil y qué llave usar, qué pasa cuando falta la llave y cómo se comporta una versión anterior de la app ante migraciones nuevas.

En esta sesión **no se pudo consultar PyPI** para confirmar qué ruedas publica cada librería: la verificación queda como tarea con criterios medibles.

## Decisión

### 1. Formato del archivo: SQLCipher 4 con llave cruda

- Formato **SQLCipher 4 con sus parámetros por defecto** (AES-256, HMAC-SHA512, páginas de 4096 bytes) y **llave cruda** de 32 bytes (`PRAGMA key = "x'<64 hex>'"`, sin PBKDF2: la llave ya es aleatoria).
- Así el formato no depende de la librería elegida: cualquiera de las candidatas lo lee y escribe, y soporte puede abrir una copia con herramientas estándar de SQLCipher si el usuario entrega su llave.

### 2. Librería (a confirmar en T1)

Orden de preferencia, detrás de `faro_engine/core/db/connection.py` (ningún otro módulo importa la librería):

1. **`sqlcipher3-wheels`**: API DB-API igual a `sqlite3`, que es la que usan las skills.
2. **`apsw-sqlite3mc`** (APSW + SQLite3 Multiple Ciphers, del autor de SQLite3MC), configurada en modo compatible: `PRAGMA cipher = 'sqlcipher'; PRAGMA legacy = 4;` antes de `PRAGMA key`.
3. Descartadas: `sqlcipher3` y `sqlcipher3-binary` si no tienen ruedas para Windows y macOS; compilar SQLCipher nosotros solo con un ADR nuevo.

**Criterios de T1** (una librería se acepta solo si cumple **todos**; si la primera falla alguno se prueba la segunda):

| # | Criterio | Cómo se comprueba |
| --- | --- | --- |
| 1 | Ruedas `cp312` para `win_amd64`, `macosx_*_arm64`, `macosx_*_x86_64` (o `universal2`) y `manylinux_*_x86_64` | Entradas de ruedas de ese paquete en `apps/engine/uv.lock` tras `uv add` (el lock lista todas las plataformas con su hash) |
| 2 | Instala y pasa las pruebas de `tests/db/` en CI `ubuntu-latest`, `windows-latest` y `macos-latest` | Trabajo `engine` en verde en los tres |
| 3 | SQLite ≥ 3.37 (tablas `STRICT`) | `select sqlite_version()` en una prueba |
| 4 | El archivo no es legible sin llave y `PRAGMA key` con otra llave falla | Pruebas de `migraciones-sqlite` |
| 5 | Carga de extensiones disponible (`enable_load_extension`) y `sqlite-vec` carga | Prueba marcada `vec` (F1a no usa `sqlite-vec`, pero una fase posterior sí) |
| 6 | Se empaqueta con PyInstaller `--onedir` en Windows | Script de prueba empaquetado en la máquina del desarrollador crea y reabre una base cifrada (el empaquetado completo es de la fase de release) |
| 7 | Mantenida (versión en los últimos 12 meses), licencia compatible con distribución privativa y `pip-audit` limpio | PyPI / repositorio del proyecto |

Si **ninguna** cumple 1–4, F1a se bloquea y el arquitecto propone alternativa (compilar SQLCipher en CI) con ADR nuevo. Si solo falla 5 o 6, se acepta la librería y se deja la tarea anotada para la fase que lo necesite.

### 3. Perfil y llave

- Un **perfil** = una base. En F1a hay un solo perfil, creado automáticamente; no hay interfaz de perfiles.
- El núcleo guarda qué perfil está activo en `<app_data_dir>/profiles.json` (no es secreto): `{"version": 1, "active_profile_id": "<uuid v7>"}`. Se escribe de forma atómica (archivo temporal + renombrar).
- Primer arranque (no existe `profiles.json`): el núcleo genera `profile_id`, genera la llave (32 bytes de `OsRng`, hex), la guarda en `db/<profile_id>/key` y **después** escribe `profiles.json`. Solo después lanza el motor.
- Arranques siguientes: el núcleo lee `profiles.json` y la llave del llavero.
  - Llave presente → la entrega.
  - Llave ausente y **no** existe `<app_data_dir>/profiles/<profile_id>.db` → genera una nueva (no hay datos que perder).
  - Llave ausente y el `.db` **existe** → **nunca** genera otra: entrega el error `db.key_missing`.
  - Llavero caído → error `vault.keyring_unavailable`.
- El núcleo solo comprueba si el archivo existe; nunca lo abre.

### 4. Arranque del motor con la base

- El motor abre la base y aplica migraciones **antes** de escribir `ready`. Si falla (llave ausente o incorrecta, migración fallida o alterada, esquema demasiado nuevo), el motor **arranca igual**, escribe `ready` y queda en "base no disponible": `/health` informa el código del problema y las rutas que necesitan la base responden `503` con ese código. Así no hay bucles de reinicio y la interfaz puede explicar qué pasa.
- Riesgo aceptado: en fases futuras una migración larga podría acercarse al límite de 20 s de `ready`. Si ocurre, se añadirá un estado `migrating` (ADR nuevo).

### 5. Compatibilidad entre versiones de la app

- Las migraciones siguen las reglas de `migraciones-sqlite` (solo agregar; columnas nuevas `NULL` o con `DEFAULT`; nunca borrar ni renombrar lo que usa la versión anterior).
- **Piso de compatibilidad** en `PRAGMA user_version`: indica la versión de esquema **mínima** que una app debe conocer para abrir la base. F1a lo fija en `1`. Una migración futura que rompa la compatibilidad hacia atrás debe subirlo (y requiere ADR).
- Una app que abre una base con migraciones que no conoce:
  - si `user_version` ≤ su última migración conocida → abre la base, **no** migra ni hace copia, registra `db.newer_schema` como aviso y sigue funcionando;
  - si `user_version` > su última migración conocida → base no disponible con `db.too_new` ("Actualiza Faro").
- Los checksums solo se comprueban para las migraciones que la app conoce.
- Copia de seguridad antes de migrar, salvo en una base recién creada (versión 0).

## Consecuencias

- Todo el acceso a la base pasa por `core/db/connection.py`; cambiar de librería no afecta al esquema ni a los datos.
- El trabajo `engine` de la CI añade `macos-latest` desde F1a para verificar la librería nativa en macOS arm64; macOS x86_64 solo se verifica por el lock (runners Intel en retirada) y se prueba de verdad en la fase de release.
- `db.key_missing` en F1a no tiene restauración desde la interfaz: se muestra el error. La restauración de copias y llaves queda para una fase posterior.
- En modo desarrollo externo (`--dev`, ADR 0004) el motor usa una base **de desarrollo** propia (`--data-dir` distinto) cuya llave viene de `.env.local` (`FARO_ENGINE_DEV_DB_KEY`). Es una excepción consciente a "la llave nunca en archivos": solo en `--dev`, rechazado con `sys.frozen`, nunca con datos reales del usuario.
- `profiles.json` se convierte en el sitio donde se añadirán más perfiles en el futuro.

## Actualización (2026-10-01, cierre de F1a)

### Librería elegida: `sqlcipher3-wheels`

Fijada en `apps/engine/pyproject.toml` como `sqlcipher3-wheels>=0.5.7,<0.6` (0.5.7 en `uv.lock`). Solo la importa `faro_engine/core/db/connection.py` (`from sqlcipher3 import dbapi2`). No hizo falta probar `apsw-sqlite3mc`.

| # | Criterio | Resultado | Evidencia en el repositorio |
| --- | --- | --- | --- |
| 1 | Ruedas `cp312` para las cuatro plataformas | Cumplido | `uv.lock`: `win_amd64`, `macosx_11_0_arm64`, `macosx_10_13_x86_64`, `macosx_10_13_universal2`, `manylinux_2_28_x86_64` (y más) |
| 2 | Pruebas de `tests/db/` en las tres plataformas de CI | Cumplido | Trabajo `engine` con matriz `ubuntu-latest`, `windows-latest`, `macos-latest`, requerido por `ci-ok` |
| 3 | SQLite ≥ 3.37 | Cumplido | `tests/db/test_connection.py::test_sqlite_supports_strict_tables` |
| 4 | Ilegible sin llave; otra llave falla | Cumplido | `test_open_creates_encrypted_file_not_readable_without_key`, `test_wrong_key_is_db_wrong_key_and_stderr_stays_empty` |
| 5 | `enable_load_extension` y `sqlite-vec` | **Por verificar** | No hay prueba marcada `vec` en el repositorio. Se acepta la librería (el criterio no bloquea, §2) y queda para la fase que use `sqlite-vec` |
| 6 | PyInstaller `--onedir` en Windows | **Por verificar** | Sin script ni evidencia en el repositorio; queda para la fase de release |
| 7 | Mantenida, licencia compatible, `pip-audit` limpio | `pip-audit` cumplido en CI (trabajo `audit`); mantenimiento y licencia **por verificar** (no documentados en el repositorio) | `.github/workflows/ci.yml` |

### Decisiones de implementación que afectan a todas las fases

- **`PRAGMA cipher_log_level = NONE` antes de `PRAGMA key`** en cada conexión: sin él, SQLCipher escribe texto UTF-16 en stderr (el canal de logs JSON) cuando la llave es incorrecta.
- La llave llega como `bytearray`; `open_encrypted` no la sobrescribe (lo hace quien la recibió). La sentencia `PRAGMA key` es un `str` inevitable que vive lo mínimo (pendiente "copias de la llave en memoria de Python", spec F1a §12).
- La única migración de F1a es `0001_initial.sql` (`sites`, `site_connections`, `audit_log`); `user_version = 1`.
