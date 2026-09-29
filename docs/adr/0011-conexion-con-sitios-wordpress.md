# ADR 0011 — Conexión con sitios WordPress: quién llama, firma, secreto y una conexión por sitio

- **Fecha:** 2026-09-29
- **Estado:** aceptado
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Skills:** `wordpress-plugin`, `llavero-y-cifrado`. **Relacionados:** ADR 0008 (licencia), 0010 (secretos), 0012 (red saliente)

## Contexto

F1a vincula la app con el plugin mediante un código de 6 dígitos y un token con firma HMAC. En F2–F4, agentes que corren en el motor leerán el sitio (auditoría), crearán borradores y escribirán metas SEO dentro de sus tareas. Había que decidir quién hace las peticiones al sitio, el formato exacto de la firma y del secreto guardado, cómo se protege el secreto en WordPress y qué pasa cuando WordPress cambia sus salts.

## Decisión

### 1. Todas las peticiones al sitio las hace el motor Python

- Incluida la vinculación (`/pair`) y la revocación. El núcleo Rust **no** habla con sitios WordPress.
- Motivos: (a) en F2–F4 las peticiones salen de tareas de agentes que viven en el motor; tener dos clientes (Rust para vincular, Python para usar) duplicaría la canonicalización de la firma, la protección SSRF y el manejo de errores; (b) el núcleo sigue siendo el guardián del llavero: el motor recibe el secreto del sitio al vincular y se lo entrega al núcleo con `create` (ADR 0010), y lo pide con `get` solo dentro de una concesión.
- Diferencia con ADR 0003 (claves de IA probadas en el núcleo): allí el secreto lo escribe el usuario en la interfaz, que ya pasa por el núcleo; aquí el secreto lo genera el sitio remoto y responde a quien hace la petición.

### 2. Firma v1 (canónica y exacta en ambos lados)

```
canonical = METHOD "\n" ROUTE "\n" TIMESTAMP "\n" NONCE "\n" hex(sha256(raw_body))
signature = base64_standard( HMAC-SHA256( key = base64url_decode(hmac_secret), canonical ) )
```

- `METHOD` en mayúsculas. `ROUTE` = ruta REST (`/faro/v1/posts`) sin barra final; si hay parámetros de consulta distintos de `rest_route`, se añade `?` + pares `rawurlencode(clave)=rawurlencode(valor)` (RFC 3986) ordenados por clave y valor, unidos con `&`. Vale igual si el sitio usa `/wp-json/` o `?rest_route=`.
- `TIMESTAMP` = segundos Unix en decimal. `NONCE` = 16 bytes aleatorios en base64url sin relleno. Cuerpo vacío = hash de la cadena vacía. Hex en minúsculas.
- La **clave HMAC son los 32 bytes decodificados**, no el texto base64url.
- Cabeceras: `X-Faro-Connection`, `X-Faro-Token`, `X-Faro-Timestamp`, `X-Faro-Nonce`, `X-Faro-Signature`.
- Vectores de prueba compartidos en `packages/shared/fixtures/wp-signature-v1.json` (valores de prueba evidentes, en la lista de excepciones de gitleaks). PHPUnit y pytest deben calcular exactamente lo mismo.
- Orden de verificación en el plugin: cabeceras presentes → conexión existe y `connection_id` coincide (`wp.revoked`) → descifrar el secreto (`wp.connection_broken`) → hash del token con `hash_equals` (`wp.invalid_signature`) → ventana de ±300 s (`wp.stale_request`) → HMAC con `hash_equals` (`wp.invalid_signature`) → nonce no usado (`wp.invalid_signature`) → **solo entonces** se guarda el nonce (así un atacante sin firma no llena la base de transients).

### 3. Secreto del sitio en el llavero

- Referencia `wp/<site_id>/token` (una por sitio). Valor: JSON compacto `{"v":1,"token":"<43 base64url>","hmac_secret":"<43 base64url>"}` (~120 bytes, muy por debajo del límite de ~2,5 KB).
- El núcleo valida esa forma exacta antes de guardarlo (`create`/`set`). El campo `v` permite cambiar el formato en el futuro.
- En la base solo quedan la URL, el `connection_id` remoto (no es secreto) y `sha256(token)` en hex; el motor comprueba que el token del llavero coincide con ese hash antes de usarlo (si no, `site.secret_missing`, que pide volver a conectar).

### 4. Secreto HMAC dentro de WordPress

- `faro_connection` (`autoload = no`) guarda `hash('sha256', token)` y el `hmac_secret` cifrado: `sodium_crypto_secretbox` con clave `hash_hmac('sha256', 'faro-hmac-secret-v1', wp_salt('auth'), true)`, almacenado como `v1:` + base64(nonce de 24 bytes ‖ texto cifrado). Sodium está en PHP ≥ 7.2 y WordPress trae `sodium_compat`.
- Un volcado de la base de datos sin `wp-config.php` no basta para firmar peticiones ni para insertar una conexión propia.
- **Rotación de salts**: si cambian las salts (a mano o por un plugin de seguridad), el descifrado falla. El plugin no intenta recuperar: responde `401 wp.connection_broken`, y la pantalla de Faro en wp-admin dice que la conexión dejó de funcionar y ofrece un código nuevo. La app lo muestra como "Vuelve a conectar tu sitio". Se acepta por su rareza y porque volver a conectar tarda un minuto.

### 5. Una conexión activa por sitio

- En F1a el plugin guarda una sola conexión. Vincular de nuevo (desde la misma u otra computadora) **reemplaza** la anterior, que empieza a recibir `wp.revoked`. wp-admin lo avisa antes de generar un código cuando ya hay conexión.
- Varias computadoras conectadas al mismo sitio a la vez quedan fuera; si llega a hacer falta, `faro_connection` pasa a una lista por `connection_id` (ADR nuevo).
- `app_instance_id` que se envía en `/pair` es un UUID aleatorio **por conexión** (el id local de la conexión), no el del perfil: así distintos sitios no pueden relacionar la misma instalación de Faro.

### 6. Versión de la API del plugin

`/pair` y `/status` devuelven `api_version: 1`. El motor exige `1`; otro valor → `site.plugin_outdated`. Los cambios incompatibles suben este número.

## Consecuencias

- Hay un solo cliente WordPress (`faro_engine/wordpress/`) que reutilizarán auditoría (F2) y publicación (F4). Las escrituras (`/drafts`, `/seo-meta`) se añadirán ahí con `Idempotency-Key`, y en el plugin con adaptadores `Faro_Seo_*`.
- El núcleo necesita conocer la forma del secreto de `wp/*/token` para validarlo, sin llegar a usarlo.
- La skill `wordpress-plugin` se actualiza en el cierre de F1a con: clave HMAC en bytes, orden de verificación, `wp.stale_request`, `wp.connection_broken`, `api_version` y cabeceras `Cache-Control: no-store`.
