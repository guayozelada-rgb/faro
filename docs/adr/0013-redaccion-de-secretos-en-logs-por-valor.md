# ADR 0013 — Redacción de secretos en los logs por valor (segunda defensa)

- **Fecha:** 2026-09-29
- **Estado:** aceptado; actualizado el 2026-10-01 (cierre de F1a, al final)
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Skill:** `llavero-y-cifrado` ("Pendiente de F1a: filtro por valor")

## Contexto

Hoy los logs se protegen por **nombre** de campo (Python `drop_sensitive_keys`) y por disciplina (Rust no registra secretos; `SecretString` imprime `[REDACTED]`). En F1a aparecen más secretos en circulación (token y secreto HMAC del sitio, llave de la base, código de vinculación) y el motor manda sus logs al núcleo, que los escribe en `faro.AAAA-MM-DD.log`. Un secreto metido por error dentro de un texto libre (un mensaje de excepción, un campo con otro nombre) no lo detecta el filtro por nombre.

## Decisión

1. **Filtro por valor en ambas capas**, aplicado a la línea final que se escribe:
   - Python: procesador de `structlog` (después de `drop_sensitive_keys`) que recorre todos los valores de texto, también anidados, y el formateador del `logging` estándar.
   - Rust: un `MakeWriter` que envuelve el escritor de `tracing-appender` y filtra cada línea antes de escribirla (cubre también las líneas del stderr del motor reenviadas).
2. **Patrones** (se sustituyen por `[redactado]`):
   - `sk-[A-Za-z0-9_-]{16,}` (OpenAI y Anthropic `sk-ant-…`), `AIza[0-9A-Za-z_-]{35}` (Google);
   - `Bearer\s+\S+`;
   - JWT `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`;
   - 64 hex seguidos (llave de la base) `\b[0-9a-fA-F]{64}\b`;
   - exactamente 43 caracteres base64url seguidos (token de sesión, token y secreto del sitio): en Python `(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])`; en Rust (el crate `regex` no admite lookaround) `(^|[^A-Za-z0-9_-])[A-Za-z0-9_-]{43}([^A-Za-z0-9_-]|$)`, conservando los grupos 1 y 2 al sustituir;
   - el código de vinculación **no** se filtra por valor (6 dígitos darían falsos positivos): se protege por nombre (`pairing_code`) y porque las peticiones no registran cuerpos.
3. **Filtro por nombre ampliado** en Python: `hmac_secret`, `refresh_token`, `key`, `db_key`, `value`, `pairing_code`, `x-faro-token`, `x-faro-signature`. El campo `code` **no** se filtra (se usa para códigos de error).
4. Cada capa tiene una prueba que registra un secreto falso de cada tipo y comprueba que no aparece en la salida.

## Consecuencias

- Posibles falsos positivos (un hash sha256 en hex se verá como `[redactado]`); se acepta, porque en los logs no debe haber hashes de tokens.
- El filtro es una **segunda** defensa: la regla sigue siendo no registrar secretos. Una prueba que falle por un secreto en el log es un error del código que lo registró, no del filtro.
- Rust gana la dependencia `regex`.

## Actualización (2026-10-01, cierre de F1a)

Lo implementado amplía la decisión; la regla de fondo (segunda defensa) no cambia.

1. **Filtro por nombre también en Rust**, con las **mismas listas** que Python (`SENSITIVE_NAMES` y `NON_SENSITIVE_NAMES` de `logging/redact.rs` y `faro_engine/core/redact.py`); una prueba de Rust lee `redact.py` y comprueba que coinciden. Si cambias una, cambia la otra.
2. **Nombres sensibles** (sin distinguir mayúsculas): `authorization`, `headers`, `token`, `secret`, `cookie`, `password`, `api_key`, `hmac_secret`, `refresh_token`, `key`, `db_key`, **`key_hex`** (nombre interno de la llave de la base en el motor), `value`, `pairing_code`, `x-faro-token`, `x-faro-signature`. Además, los que terminan en `_token`, `_secret`, `_key` (o con `-`), en `token`/`secret` sin separador, en `…Key` en camelCase y los que contienen `password`. Excepciones (en cualquier forma): `secret_ref`, `public_key`, `cache_key`, `sort_key`, `primary_key`, `foreign_key`, `idempotency_key`. `code` sigue sin filtrarse.
3. **Patrones de valor** añadidos o ajustados: `Basic\s+[A-Za-z0-9+/_-]{8,}={0,2}`; `Bearer` sin consumir comillas ni barras invertidas (`Bearer\s+[^\s"\\]+`) para no romper el JSON; `sk-…` con límite de palabra o justo tras un escape `\n`, `\r`, `\t`.
4. **43 base64url y 64 hex**: en las dos capas se buscan secuencias máximas de `[A-Za-z0-9_-]` (o de `[A-Za-z0-9_]` para el hex) y se redactan las que miden exactamente 43 (o son 64 hex), también tras un escape `\n`, `\r`, `\t`. Sustituye a las expresiones con lookaround o grupos del punto 2.
5. En Rust, si el filtro no se puede construir, la línea se sustituye por `[registro omitido: filtro de secretos no disponible]`: se prefiere perder la línea a escribir un secreto.
6. Pendientes menores arrastrados (redacción de `\"` en consola, `proxy-authorization`, `apikey`, `credentials`): spec F1a §12.
