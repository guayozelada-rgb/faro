---
name: migraciones-sqlite
description: Base de datos local de Faro - un archivo SQLite cifrado con SQLCipher por perfil, apertura con la llave que entrega el núcleo, convenciones de tablas y columnas, migraciones numeradas con schema_migrations, copias de seguridad y pruebas con pytest. Úsala al crear o cambiar tablas, escribir migraciones o tocar la conexión a la base en el motor.
---

# Base de datos y migraciones

## Dónde vive

- Un archivo por perfil: `<data-dir>/profiles/<perfil>.db`, donde `<data-dir>` es el argumento `--data-dir` del motor y `<perfil>` el `id` del perfil (UUID v7, nunca el nombre visible).
- Copias de seguridad: `<data-dir>/profiles/backups/<perfil>-v<NNNN>-<YYYYMMDDTHHMMSSZ>.db` (se guardan las 3 últimas).
- Solo el motor abre la base. El núcleo Rust y la interfaz nunca la leen directamente.

## Librería SQLCipher

- Elegida: **`sqlcipher3-wheels`** (`>=0.5.7,<0.6` en `pyproject.toml`; misma API DB-API que `sqlite3`, ruedas con SQLCipher 4). Resultado de los criterios en ADR 0009 (actualización 2026-10-01). Por verificar: carga de `sqlite-vec` y empaquetado con PyInstaller.
- Formato SQLCipher 4 con parámetros por defecto y **llave cruda** (sin PBKDF2): no depende de la librería.
- Todo el código usa `faro_engine/core/db/connection.py` (`open_encrypted`, `transaction`, `Connection`, `DatabaseError`, `complete_statement`); ningún otro módulo importa `sqlcipher3`.
- Acceso desde el motor: `Database` (`core/db/database.py`) con **una conexión y un candado**; las consultas van en un hilo (`await database.run(fn)` → `anyio.to_thread`). Dependencia FastAPI `get_db()`: si la base no está disponible, `503` con el código de su estado (`db.*`).

## Apertura

La llave (32 bytes aleatorios en hex) llega del núcleo por stdin al arrancar (ver `llavero-y-cifrado`). Nunca se lee de disco, variables de entorno ni argumentos, y nunca se registra.

Resumen de `open_encrypted` (`core/db/connection.py`; úsala, no la copies):

```python
conn = _sqlcipher.connect(str(path), isolation_level=None, check_same_thread=False)  # transacciones explícitas
conn.execute("PRAGMA cipher_log_level = NONE")            # antes de la llave: stderr limpio
statement = "PRAGMA key = \"x'" + key_hex.decode("ascii") + "'\""  # llave cruda; str inevitable
try:
    conn.execute(statement)
finally:
    del statement
try:
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
except DatabaseError as exc:
    raise DbUnavailableError(DB_WRONG_KEY, "key_rejected") from exc  # → base no disponible
conn.execute("PRAGMA foreign_keys = ON")
conn.execute("PRAGMA journal_mode = WAL").fetchone()
conn.execute("PRAGMA busy_timeout = 5000")
```

- Un fallo al abrir o migrar **no** tumba el motor: `Database.unavailable(code)` y `/health` lo informa (`database.state`, `error_code`, `newer_schema`). Códigos: `db.key_missing`, `db.wrong_key`, `db.migration_failed`, `db.migration_tampered`, `db.too_new`, `db.unavailable`.
- `open_encrypted` **no** sobrescribe `key_hex`; lo hace quien la recibió (`__main__.open_database`, en un `finally`).

- `PRAGMA cipher_log_level = NONE` va **antes** de `PRAGMA key` en cada conexión: sin él, SQLCipher escribe texto UTF-16 en stderr (el canal de logs JSON) cuando la llave es incorrecta. Implementado y probado en `core/db/connection.py` (`open_encrypted`).
- `PRAGMA key` es siempre la **primera** sentencia que toca el archivo. `key_hex` es `bytearray` para poder sobrescribirlo después.
- `foreign_keys` se activa en **cada** conexión (SQLite no lo recuerda).
- Los archivos `-wal` y `-shm` también quedan cifrados; se copian y borran junto con el `.db`.

## Convenciones de esquema (de `faro-arquitectura`)

- Tablas en plural `snake_case`, `STRICT`. Clave primaria `id TEXT PRIMARY KEY` con UUID v7 (`faro_engine.core.ids.new_id()`).
- Fechas `*_at TEXT` en UTC ISO-8601 con `Z` (`2026-09-29T12:00:00Z`). Dinero `*_minor INTEGER` + `currency TEXT` (o `*_micros` para Google Ads). Nunca `REAL` para dinero.
- Booleanos `INTEGER NOT NULL CHECK (x IN (0, 1))`. Claves foráneas `<tabla_singular>_id` con `ON DELETE` explícito.
- Ningún secreto en ninguna tabla: `credentials` guarda solo metadatos (`secret_ref`, `provider`, `last4`, estado, fechas); `site_connections` guarda URL y hash del token.

## Migraciones

- Archivos: `apps/engine/faro_engine/core/db/migrations/NNNN_descripcion.sql` (4 dígitos, `snake_case`). Usa `.py` con `def migrate(conn) -> None` solo si hace falta lógica (p. ej. `ALTER TABLE ADD COLUMN` condicionado a `PRAGMA table_info`, o copiar datos).
- Tabla de control:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,      -- sha256 del archivo
  applied_at TEXT NOT NULL
) STRICT;
```

- El ejecutor aplica en orden las versiones pendientes, **una transacción por migración**: `BEGIN IMMEDIATE` → sentencias una a una → `INSERT INTO schema_migrations` → `COMMIT`; ante cualquier error, `ROLLBACK` y el motor responde `db.migration_failed`.
- **No uses `executescript`**: hace `COMMIT` implícito y rompe la atomicidad. Separa sentencias con `sqlite3.complete_statement`.
- Si el `checksum` de una migración ya aplicada no coincide, el motor no arranca la base (`db.migration_tampered`). El checksum es el sha256 del archivo **con finales de línea normalizados a LF** (así un checkout con CRLF en Windows no parece alterado).
- Antes de migrar: `PRAGMA wal_checkpoint(TRUNCATE)`, cerrar conexiones y copiar el `.db` a `backups/` (la copia sigue cifrada con la misma llave; `core/db/backups.py`, se guardan las 3 últimas). Sin copia en una base recién creada (versión 0).
- **Piso de compatibilidad** (`PRAGMA user_version`, ADR 0009 §5): lo fija el ejecutor, no el `.sql`, desde `COMPATIBILITY_FLOORS` de `core/db/migrations.py` (hoy `{1: 1}`). Una app que encuentra migraciones que no conoce: si `user_version` ≤ su última migración, abre sin migrar ni copiar y marca `newer_schema`; si es mayor, `db.too_new`. Subir el piso rompe la compatibilidad hacia atrás y requiere ADR.
- `plan()` solo lee; `apply()` escribe; la copia de seguridad la hace quien los llama (`database.py`).

Migraciones existentes:

| Versión | Archivo | Contenido | Piso |
| --- | --- | --- | --- |
| 1 | `0001_initial.sql` (F1a) | `sites` (URL única normalizada), `site_connections` (`kind = 'wp_plugin'`, `token_sha256`, `secret_ref`, `status` `active`/`revoked`, metadatos y conteos; `ON DELETE CASCADE`), `audit_log` (sin clave foránea a `sites`, solo se inserta) | 1 |

No hay tabla `organizations`: se añadirá cuando se use (columna `NULL`, regla 3). Fixture de la versión 1 para probar la 2: `tests/fixtures/db/v0001.sql`.

## Reglas

1. **Nunca edites una migración publicada**: crea otra nueva.
2. **Nunca borres ni renombres** tablas o columnas que use la versión anterior de la app (para poder volver atrás). Patrón ampliar → migrar datos → dejar de usar; el borrado va, como pronto, dos versiones después y con ADR.
3. Columnas nuevas: `NULL` o con `DEFAULT`, para que la versión anterior pueda seguir insertando.
4. Idempotentes: `IF NOT EXISTS` en `CREATE`; `.py` comprueba antes de alterar.
5. Una versión anterior que encuentra migraciones más nuevas que las suyas no falla ni migra: abre la base y registra un aviso.
6. Nada de datos de usuario ni secretos en las migraciones.

## Pruebas (`apps/engine/tests/db/`)

Archivos: `test_connection.py` (cifrado, llave incorrecta con stderr vacío, pragmas, SQLite ≥ 3.37), `test_migrations.py`, `test_database.py`, `test_backups.py`, con utilidades en `tests/db/helpers.py`. Patrón:

```python
from tests.db.helpers import open_db  # TEST_KEY_HEX = "00" * 32, llave fija solo de pruebas

def test_open_creates_encrypted_file_not_readable_without_key(tmp_path: Path) -> None:
    db = tmp_path / "perfil.db"
    conn = open_db(db)
    conn.execute("CREATE TABLE t (x TEXT) STRICT")
    conn.execute("INSERT INTO t VALUES ('texto-visible-de-prueba')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    raw = db.read_bytes()
    assert not raw.startswith(b"SQLite format 3\x00")
    assert b"texto-visible-de-prueba" not in raw
```

Obligatorias: todas las migraciones sobre base vacía; cada migración nueva sobre una base con datos de la versión anterior (fixture `tests/fixtures/db/v<NNNN>.sql`); aplicar dos veces no cambia nada; llave incorrecta → `db.wrong_key`; `checksum` alterado → `db.migration_tampered`; fallo a mitad → `ROLLBACK` y la base queda en la versión previa; `foreign_keys` activo; piso de compatibilidad (`db.too_new` y `newer_schema`). `core/db/connection.py` y `core/db/migrations.py` exigen 95 % de cobertura. Comando: `npm run test:engine`.
