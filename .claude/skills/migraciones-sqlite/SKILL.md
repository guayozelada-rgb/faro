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

- Candidata: **`sqlcipher3-wheels`** (misma API DB-API que `sqlite3`, ruedas precompiladas con SQLCipher 4). Alternativa: **`apsw-sqlite3mc`** (APSW + SQLite3 Multiple Ciphers con esquema compatible con SQLCipher). `sqlcipher3` / `sqlcipher3-binary` quedan descartadas si no tienen ruedas para Windows y macOS.
- La elección final va en el **ADR de F1a**, tras verificar: ruedas para `win_amd64`, `macosx_arm64` y `macosx_x86_64` con Python 3.12; empaquetado con PyInstaller; carga de `sqlite-vec` (`enable_load_extension`); versión de SQLite ≥ 3.37 (tablas `STRICT`).
- Todo el código usa `faro_engine/core/db/connection.py`; ningún módulo importa la librería directamente.

## Apertura

La llave (32 bytes aleatorios en hex) llega del núcleo por stdin al arrancar (ver `llavero-y-cifrado`). Nunca se lee de disco, variables de entorno ni argumentos, y nunca se registra.

```python
def open_profile(path: Path, key_hex: bytearray) -> Connection:
    conn = sqlcipher.connect(path, isolation_level=None)  # transacciones explícitas
    conn.execute(f"PRAGMA key = \"x'{key_hex.decode()}'\"")  # llave cruda: sin PBKDF2
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlcipher.DatabaseError:
        conn.close()
        raise FaroError("db.wrong_key", "No pudimos abrir los datos de este perfil.", status=500)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
```

- `PRAGMA key` es siempre la **primera** sentencia. `key_hex` es `bytearray` para poder sobrescribirlo después.
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
- Si el `checksum` de una migración ya aplicada no coincide, el motor no arranca la base (`db.migration_tampered`).
- Antes de migrar: `PRAGMA wal_checkpoint(TRUNCATE)`, cerrar conexiones y copiar el `.db` a `backups/` (la copia sigue cifrada con la misma llave).

Ejemplo `0001_initial.sql`:

```sql
CREATE TABLE IF NOT EXISTS sites (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS sites_organization_id_idx ON sites(organization_id);
```

## Reglas

1. **Nunca edites una migración publicada**: crea otra nueva.
2. **Nunca borres ni renombres** tablas o columnas que use la versión anterior de la app (para poder volver atrás). Patrón ampliar → migrar datos → dejar de usar; el borrado va, como pronto, dos versiones después y con ADR.
3. Columnas nuevas: `NULL` o con `DEFAULT`, para que la versión anterior pueda seguir insertando.
4. Idempotentes: `IF NOT EXISTS` en `CREATE`; `.py` comprueba antes de alterar.
5. Una versión anterior que encuentra migraciones más nuevas que las suyas no falla ni migra: abre la base y registra un aviso.
6. Nada de datos de usuario ni secretos en las migraciones.

## Pruebas (`apps/engine/tests/db/`)

```python
TEST_KEY = bytearray(b"00" * 32)  # llave fija solo de pruebas

def test_archivo_no_es_legible_sin_llave(tmp_path: Path) -> None:
    db = tmp_path / "perfil.db"
    migrate(open_profile(db, TEST_KEY))
    assert not db.read_bytes().startswith(b"SQLite format 3\x00")
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(db).execute("SELECT * FROM sqlite_master").fetchall()
```

Obligatorias: todas las migraciones sobre base vacía; cada migración nueva sobre una base con datos de la versión anterior (fixture `tests/fixtures/db/v<NNNN>.sql`); aplicar dos veces no cambia nada; llave incorrecta → `db.wrong_key`; `checksum` alterado → `db.migration_tampered`; fallo a mitad → `ROLLBACK` y la base queda en la versión previa; `foreign_keys` activo. Comando: `npm run test:engine`.
