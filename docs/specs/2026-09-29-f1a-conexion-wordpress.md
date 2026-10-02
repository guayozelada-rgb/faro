# F1a — Conexión con WordPress

- **Fecha:** 2026-09-29
- **Autor:** arquitecto
- **Estado:** implementada (2026-10-01). Historial: aprobada (2026-09-29, por el usuario) → implementada (2026-10-01, T0–T13 integradas en los PR #13 a #25; cierre T14). Las diferencias entre este documento y el código están en §13 y lo que queda abierto en §12. Verificación manual: parcial (§10 y [lista manual](../qa/2026-10-01-f1a-verificacion-manual.md)).
- **ADR nuevos:** [0008](../adr/0008-licencia-gpl-del-plugin-wordpress.md) (licencia GPL del plugin, decisión del usuario), [0009](../adr/0009-base-de-datos-local-cifrada.md) (base cifrada), [0010](../adr/0010-protocolo-nucleo-motor-secretos-y-auditoria.md) (protocolo, secretos y auditoría), [0011](../adr/0011-conexion-con-sitios-wordpress.md) (conexión con WordPress), [0012](../adr/0012-red-saliente-del-motor.md) (red saliente y SSRF), [0013](../adr/0013-redaccion-de-secretos-en-logs-por-valor.md) (logs por valor)
- **ADR anteriores que aplican:** 0001–0007 (sobre todo 0002 errores, 0004 arranque, 0005 CI pública, 0006 CSP, 0007 paleta)
- **Parte de F0 que se retoma:** pendientes 4 y 5 de §14 de [F0](2026-09-28-f0-esqueleto.md) (quitar `core:default` y cobertura de Rust). El 6 (pruebas extremo a extremo) se pospone otra vez (§2.4).

---

## 1. Objetivo

Que Ana conecte su sitio WordPress/WooCommerce a Faro de forma segura, sin dar su contraseña, y Faro pueda **leer** sus páginas, entradas y productos.

## 2. Alcance

### 2.1 Entra en F1a

1. **Base de datos local cifrada** (prerequisito): SQLCipher 4, un perfil creado automáticamente, llave en el llavero entregada por stdin, migraciones con `schema_migrations`, tablas `sites`, `site_connections`, `audit_log` (ADR 0009).
2. **Protocolo núcleo ↔ motor v2**: línea `db_key`, canal `secret_request`/`secret_response` con operaciones y concesiones por operación, eventos `audit` (ADR 0010).
3. **`engine_call`** en el núcleo con la lista permitida `engine-operations.json` (pospuesto en F0).
4. **Plugin de WordPress** (`packages/wp-plugin`, GPL-2.0-or-later, ADR 0008): vinculación con código de 6 dígitos, firma HMAC, rutas `/pair`, `/status`, `/pages`, `/posts`, `/products`, `DELETE /connection`, pantalla en wp-admin, compatibilidad con HPOS, desinstalación limpia.
5. **Cliente WordPress en el motor** con protección SSRF, tiempos y reintentos (ADR 0011 y 0012).
6. **Interfaz**: Configuración → "Sitios conectados" (conectar, comprobar, ver contenido, volver a conectar, quitar), asistente de conexión con descarga del plugin, Inicio → "Qué hacer ahora" actualizado.
7. **Auditoría local** de toda operación con secretos (Bóveda incluida) y de conexión/desconexión de sitios.
8. **Logs**: filtro por valor en ambas capas (ADR 0013).
9. **CI**: PHPCS, PHPStan y PHPUnit del plugin; trabajo con wp-env y WooCommerce; motor también en `macos-latest`; `composer audit`; cobertura de Rust.
10. **Pendientes de F0**: sustituir `core:default` por permisos de eventos concretos; medir y exigir cobertura de Rust.

### 2.2 Decisiones de alcance

| Tema | Decisión | Por qué |
| --- | --- | --- |
| **"Solo quiero analizar una URL"** (sin plugin, solo lectura pública) | **Se pospone a F2 (Auditoría).** | Sin plugin lo único que se puede leer es lo público, y leer lo público *es* el rastreador de F2 (robots.txt, límites, renderizado). En F1a esos datos no tendrían ningún uso (no hay análisis) y habría que diseñar un estado "sitio sin conexión" en la interfaz. El modelo de datos queda preparado: `sites` es independiente de `site_connections`, así que F2 puede crear sitios solo con URL sin migración incompatible. |
| **Un sitio o varios** | **Varios** desde F1a, en lista simple. | El modelo lo exige (Diego maneja 5–20) y una lista no cuesta más que un caso único. Sin selector de sitio en la barra superior (llega con los agentes). |
| **Botón "Abrir Faro en el escritorio"** en wp-admin | **Se pospone a la fase de release.** | Un enlace profundo (`faro://`) necesita `tauri-plugin-deep-link` + instancia única, registrar el esquema en el instalador (que aún no existe) y tratar la URL recibida como entrada no confiable. En F1a wp-admin muestra en su lugar: "Abre Faro en tu computadora y ve a Configuración → Sitios conectados." |
| **Distribución del plugin** | Zip generado por el build (`npm run build:wp-plugin`); la app lo guarda en Descargas. WordPress.org en la fase de lanzamiento. | Pedido por el usuario. |
| **Contenido leído** | Se lee en vivo, **no se guarda** en la base. La base solo guarda conteos y metadatos del sitio. | Qué inventario guardar lo decide F2 según lo que necesite la auditoría. Guardarlo ahora obligaría a mantener una tabla que nadie usa. |
| **Qué contenido** | Solo publicado (`publish`): páginas, entradas y productos. | Suficiente para demostrar la lectura y para la auditoría; borradores y privados cuando haga falta. |

### 2.3 Queda fuera de F1a (explícito)

- Publicar borradores y escribir metas de Yoast/Rank Math (F4). El diseño queda preparado: cliente único en `faro_engine/wordpress/` (ADR 0011), detección del plugin SEO en `/status` y carpeta `includes/seo/` en el plugin.
- Leer pedidos (`wc_get_orders`), clientes o datos personales.
- WordPress multisitio; varias computadoras conectadas al mismo sitio a la vez (una conexión nueva reemplaza la anterior, ADR 0011 §5).
- Sitios solo `http`, en puertos distintos de 443, detrás de autenticación HTTP básica o en la red local (ADR 0012). Proxy del sistema.
- Varios perfiles, interfaz de perfiles, restaurar copias de seguridad o la llave de la base (`db.key_missing` solo se informa).
- Visor del registro de auditoría.
- `sqlite-vec` (solo se verifica que carga, ADR 0009).
- Enlace profundo, repositorio WordPress.org, instalador y firma (release).
- Iconos del sitio (favicons): no se cargan imágenes remotas en F1a.

### 2.4 Pendientes de F0 que siguen pospuestos

- **Pruebas extremo a extremo automáticas** (WebdriverIO + tauri-driver): se posponen a **F1b** (la siguiente subfase de F1). Motivo: F1a ya abre cinco frentes nuevos (base, protocolo, plugin, CI con Docker e interfaz); el flujo extremo a extremo se cubre con la prueba de integración motor ↔ wp-env (§9.4) y la verificación manual (§10).

### 2.5 Prerrequisitos

- F0 §14 puntos 1–2 hechos: primer PR y protección de `main` con `ci-ok` requerido (todo el trabajo de F1a entra por PR con `ci-ok` en verde).
- Docker Desktop con WSL 2 instalado (hecho por el usuario). **El motor de Docker todavía no arranca**: debe resolverse antes de que empiece la tarea T3 (plugin), porque wp-env es la vía de pruebas del plugin.

---

## 3. Experiencia

Textos en español, tuteo. Colores y componentes según `sistema-diseno-faro` (ADR 0007). Los mensajes de error se eligen por `code` (§5.6).

### 3.1 Configuración

La página pasa a tener **pestañas** (componente Tabs), estado en la URL (`#/settings?tab=sites` / `?tab=ai-keys`):

1. **Sitios conectados** (pestaña por defecto)
2. **Claves de IA** (lo de F0, sin cambios)

### 3.2 Configuración → Sitios conectados

Tooltip del título: "Faro lee tu sitio con un plugin. Nunca te pedimos tu contraseña."

| Estado | Qué ve el usuario |
| --- | --- |
| **Cargando** | Dos tarjetas esqueleto. |
| **Motor no listo** (`engine.not_ready`) | "El motor de Faro todavía no está listo. Espera unos segundos e intenta de nuevo." + **Intentar de nuevo**. |
| **Base no disponible** (`db.*`) | Mensaje del código (§5.6) sin botón de acción (reiniciar no lo arregla), salvo `db.migration_failed`: **Intentar de nuevo**. |
| **Vacío** | Estado vacío: título "Conecta tu sitio de WordPress", frase "Faro leerá tus páginas, entradas y productos para ayudarte a atraer más clientes. Nunca te pedimos tu contraseña.", acción **Conectar tu sitio**. |
| **Con sitios** | Botón **Conectar otro sitio** arriba a la derecha y una tarjeta por sitio (orden: fecha de conexión). |

**Tarjeta de sitio**: icono `globe`, nombre del sitio (o el dominio si no hay nombre), URL en JetBrains Mono, chip de conexión (`ConnectionChip`, texto + icono) y una línea de datos.

| Chip | Cuándo | Línea de datos | Acciones |
| --- | --- | --- | --- |
| "Comprobando…" (neutral, con indicador de carga) | Durante la comprobación | la anterior | todas deshabilitadas |
| "Conectado" (`success`) | `status = active` y comprobación correcta en esta sesión | "12 páginas · 34 entradas · 56 productos" (con `Intl.NumberFormat` y plurales); si WooCommerce no está activo: "12 páginas · 34 entradas · WooCommerce no está activo". Debajo: "Comprobado hace 5 minutos" (`Intl.RelativeTimeFormat`) | **Ver contenido**, **Comprobar conexión**, menú ⋯ → **Desconectar sitio** |
| "Sin comprobar" (neutral) | `status = active` pero la comprobación automática no pudo hacerse | la guardada + mensaje del error debajo | igual que "Conectado" |
| "Desconectado" (`warning`) | `status = revoked` | Mensaje del `last_error_code` (`site.revoked`, `site.connection_broken`, `site.auth_failed`, `site.secret_missing`) | **Volver a conectar**, **Quitar de Faro** |

**Comprobación automática** (mismo patrón que la Bóveda de F0, ADR 0003): al abrir la pestaña, la interfaz llama a `checkSiteConnection` **una vez por sesión y por sitio** para cada sitio `active`. Un error que impide comprobar (red, tiempo agotado, error del servidor) deja la tarjeta en "Sin comprobar" con su mensaje y no se reintenta sola. Un veredicto (`revoked`) cambia la tarjeta a "Desconectado". Sin toasts en la comprobación automática; **Comprobar conexión** manual sí muestra un toast ("Tu sitio responde bien." o el error).

### 3.3 Asistente "Conectar tu sitio"

Diálogo de tres pasos con indicador "Paso 1 de 3". Siempre visible al pie: "Nunca te pediremos tu contraseña de WordPress." Botones: **Atrás** / **Siguiente**, **Cancelar**.

**Paso 1 — "Instala el plugin de Faro en tu sitio"**
- Botón **Guardar el plugin en Descargas** → llama `wp_plugin_export`. Con éxito: texto "Guardamos faro-wordpress.zip en tu carpeta Descargas." y se abre la carpeta con el archivo seleccionado. Error: mensaje (`plugin.package_missing`, `plugin.export_failed`).
- Instrucciones numeradas: "1. Entra al panel de WordPress de tu sitio. 2. Ve a Plugins → Añadir nuevo → Subir plugin. 3. Elige faro-wordpress.zip, pulsa Instalar ahora y después Activar."
- Enlace-botón "Ya tengo el plugin instalado" → paso 2.

**Paso 2 — "Genera un código en WordPress"**
- "En WordPress, ve a Ajustes → Faro y pulsa Generar código de conexión. El código dura 10 minutos y solo sirve una vez."

**Paso 3 — "Escribe la dirección y el código"**
- Campo **Dirección de tu sitio** (JetBrains Mono, `type="url"`, `autocomplete="off"`, `spellcheck=false`, marcador `https://tutienda.com`). Ayuda: "La misma dirección que escriben tus clientes."
- Campo **Código de conexión** (`inputMode="numeric"`, `autocomplete="one-time-code"`, acepta "482913" o "482 913"; se validan 6 números en la interfaz antes de enviar: "El código tiene 6 números.").
- Botón principal **Conectar sitio** (carga: "Conectando con tu sitio…"; puede tardar hasta un minuto).
- Éxito: el diálogo se cierra, los campos se vacían, toast "Tu sitio {{name}} está conectado." y la lista se refresca con la tarjeta nueva en "Conectado".
- Error: el mensaje va debajo del campo que corresponde (URL: `site.invalid_url`, `site.https_required`, `site.address_not_allowed`, `site.unreachable`, `site.tls_error`, `site.plugin_not_found`, `site.moved`, `site.blocked`; código: `site.pairing_code_invalid` con intentos restantes, `site.pairing_code_expired`, `site.rate_limited`), o arriba del formulario para el resto. El diálogo sigue abierto.
- `site.already_connected`: "Este sitio ya está en Faro." + botón **Ir al sitio** (cierra el diálogo y enfoca su tarjeta).
- El código vive solo en el estado del campo: no va a `useMutation`, caché de Query, `localStorage` ni `console`.

**Volver a conectar** abre el mismo diálogo en el paso 2, con la URL fija (solo lectura) y botón **Volver a conectar** → `reconnectSite`.

### 3.4 Ver contenido

Panel lateral (Sheet) "Contenido de {{name}}", con pestañas **Páginas**, **Entradas**, **Productos** (la de Productos, si WooCommerce no está activo, muestra "Este sitio no tiene WooCommerce activo.").

- Tabla (14 px, 3 columnas): **Título**, **Dirección** (JetBrains Mono, truncada con tooltip; texto, no enlace), **Última modificación** (`Intl.DateTimeFormat`, zona del usuario).
- Paginación de 50 elementos: **Anteriores** / **Siguientes** y "Página 2 de 5".
- Cargando: filas esqueleto. Vacío: "Tu sitio no tiene páginas publicadas." (y el equivalente para entradas y productos). Error: mensaje + **Intentar de nuevo**; si el error es `site.revoked` o `site.connection_broken`, el panel lo muestra y la lista de sitios se refresca.
- Los títulos llegan como **texto plano** y se muestran como texto (nunca HTML).

### 3.5 Desconectar y quitar

- **Desconectar sitio** (sitio activo): confirmación con botón `destructive` **Desconectar {{name}}** y texto "Faro dejará de leer este sitio. Para volver a conectarlo necesitarás un código nuevo."
- **Quitar de Faro** (sitio desconectado): confirmación "Quitaremos {{name}} de tu lista." con botón `destructive` **Quitar de Faro**.
- Ambos llaman `removeSite`. Resultado `remote_revoked: true` → toast "Desconectamos {{name}}."; `remote_revoked: false` en un sitio que estaba activo → toast de aviso "Quitamos {{name}} de Faro, pero no pudimos avisar a tu sitio. Para terminar, entra a WordPress → Ajustes → Faro y pulsa Desconectar." (en un sitio ya desconectado no se avisa).

### 3.6 Inicio → "Qué hacer ahora"

Lista de pasos con estado (icono + texto, nunca solo color):

1. **Conecta tu sitio** — hecho si hay al menos un sitio `active`. Acción **Conectar tu sitio** → Configuración, pestaña Sitios conectados.
2. **Agrega una clave de IA** — hecho si la Bóveda tiene al menos una clave. Acción **Agregar clave de IA** → pestaña Claves de IA.

- Sin nada hecho: se mantiene el estado vacío de bienvenida de F0 con **Conectar tu sitio** como acción principal y el paso 2 debajo.
- Todo hecho: "Todo listo por ahora. Pronto los agentes empezarán a trabajar en tu sitio." (sustituye el texto de F0).
- Mientras carga la lista de sitios: esqueleto en el paso 1. Si el motor no está listo o la base no está disponible: el paso 1 muestra "Aún no podemos ver tus sitios." sin bloquear el paso 2.

**Tarjeta del motor**: si `EngineStatus.database_error` no es `null`, debajo de "Motor conectado" aparece un aviso `critical` con el mensaje de su código (§5.6).

### 3.7 Pantalla del plugin en wp-admin (Ajustes → Faro)

Solo usuarios con `manage_options`. Textos del plugin en español (ver §4.1).

| Estado | Contenido |
| --- | --- |
| Sitio sin HTTPS (y entorno no `local`) | Aviso: "Tu sitio no usa HTTPS. Faro solo se conecta a sitios con HTTPS." Botón de código deshabilitado. |
| Sin conexión | "Conecta tu sitio con Faro" + texto "Faro leerá tus páginas, entradas y productos. No necesita tu contraseña." + botón **Generar código de conexión**. |
| Código generado (solo en la respuesta a esa acción; nunca se vuelve a mostrar) | Código grande "482 913", "Escríbelo en Faro junto con esta dirección: https://tutienda.com", "Caduca en 10 minutos y solo sirve una vez." + botón **Generar otro código**. Si ya había conexión: "Si usas este código, la conexión actual se reemplazará." |
| Conectado | "Tu sitio está conectado con Faro desde el {{fecha}}." "Última lectura de Faro: {{fecha}}." Botón **Desconectar de Faro** (confirmación del navegador). Botón **Generar código de conexión** secundario para reemplazar. |
| Conexión rota (salts cambiadas) | "La conexión con Faro dejó de funcionar porque cambiaron las claves de seguridad de WordPress. Genera un código nuevo y escríbelo en Faro." |
| Siempre | "Abre Faro en tu computadora y ve a Configuración → Sitios conectados." |

Enlace "Ajustes" en la fila del plugin en la lista de Plugins. Nada de configuración de agentes en wp-admin.

---

## 4. Diseño técnico por capa

### 4.1 Plugin WordPress (`wordpress-php`) — `packages/wp-plugin`

Sigue la skill `wordpress-plugin` con las precisiones de ADR 0011.

**Estructura**
```
packages/wp-plugin/
  faro.php  uninstall.php  readme.txt  LICENSE (GPL-2.0, ADR 0008)
  includes/ class-faro-plugin.php  class-faro-pairing.php  class-faro-crypto.php
            class-faro-signature.php  class-faro-rest.php  class-faro-content.php
            class-faro-status.php  woocommerce/class-faro-woocommerce.php
            seo/class-faro-seo-detector.php
  admin/    class-faro-admin.php  views/settings-page.php
  languages/ faro.pot
  tests/    bootstrap.php  test-*.php
  composer.json  composer.lock  phpcs.xml.dist  phpstan.neon.dist  phpunit.xml.dist  .wp-env.json
```
- Prefijos `faro_`, clases `Faro_*`, constantes `FARO_` (`FARO_VERSION = '0.1.0'`, `FARO_API_VERSION = 1`, `FARO_PLUGIN_FILE`). Todo archivo empieza con `defined( 'ABSPATH' ) || exit;`.
- Cabecera: `Plugin Name: Faro`, `Requires at least: 6.0`, `Requires PHP: 8.1`, `Text Domain: faro`, `License: GPL-2.0-or-later`, `License URI`, `WC requires at least` / `WC tested up to` (valores de la versión fijada en `.wp-env.json`).
- **Sin dependencias de Composer en tiempo de ejecución** (Composer solo para herramientas de desarrollo; el zip no lleva `vendor/`).
- **No incluye código de la app** (ADR 0008).
- Textos: fuente en **español neutro** con `__()` / `esc_html__()` y dominio `faro`; `faro.pot` generado con `wp i18n make-pot`. (Traducción al inglés antes de WordPress.org, §11.)

**Vinculación** (`Faro_Pairing`): como la skill, con: código `random_int(0, 999999)` con 6 dígitos (ceros a la izquierda), opción `faro_pairing` (`autoload = no`) con `hash_hmac('sha256', $code, wp_salt('auth'))`, `expires_at` (+10 min) y `attempts_left` (5). Método público `Faro_Pairing::create_code(): string` (lo usa la pantalla y la prueba de integración vía `wp eval`). Generar código exige `manage_options` + `check_admin_referer`. Solo si `wp_is_using_https()` o `wp_get_environment_type() === 'local'`.

**`POST /faro/v1/pair`** (`permission_callback` verifica que haya código pendiente y el límite por IP: 10 fallos cada 15 min por `REMOTE_ADDR`, transient `faro_pair_ip_<sha256>`; no se confía en `X-Forwarded-For`). Compara con `hash_equals`; fallo resta intento (a 0 borra el código); éxito borra el código, genera `connection_id` (UUID v4 de `wp_generate_uuid4`), `token` y `hmac_secret` (32 bytes de `random_bytes`, base64url sin relleno), guarda `faro_connection` (`autoload = no`) con `connection_id`, `token_sha256`, `hmac_secret` cifrado (`Faro_Crypto`, ADR 0011 §4), `app_instance_id`, `app_version`, `created_at`, `last_seen_at`. Una conexión anterior se reemplaza.

**Firma** (`Faro_Signature::verify( WP_REST_Request )`): canónica y orden de verificación de ADR 0011 §2. Nonces en transients `faro_nonce_<sha256(nonce)>` de 10 min. `last_seen_at` se actualiza como mucho cada 5 min.

**Contenido** (`Faro_Content`): páginas y entradas con `WP_Query` (`post_status = publish`, `orderby = ID`, `order = ASC`, `posts_per_page`, `paged`); productos solo con `wc_get_products( [ 'status' => 'publish', 'limit' => $per_page, 'page' => $page, 'paginate' => true, 'orderby' => 'ID', 'order' => 'ASC' ] )`. Título: `html_entity_decode( wp_strip_all_tags( get_the_title( $post ) ), ENT_QUOTES, 'UTF-8' )`. URL: `get_permalink()`. Fechas en UTC ISO-8601 con `Z` (`post_modified_gmt`; productos `get_date_modified()` en UTC).

**Estado** (`Faro_Status`): versiones, conteos (`wp_count_posts()->publish` para páginas y entradas; productos con `wc_get_products( [ 'status' => 'publish', 'limit' => 1, 'paginate' => true, 'return' => 'ids' ] )->total`), WooCommerce activo, versión y HPOS (`OrderUtil::custom_orders_table_usage_is_enabled()` si existe la clase), plugin SEO detectado (`Faro_Seo_Detector`: `yoast` si `defined('WPSEO_VERSION')`, `rank_math` si `defined('RANK_MATH_VERSION')`, si no `none`). Los adaptadores de escritura `Faro_Seo_*` se crean en F4.

**Cabeceras de respuesta**: todas las rutas `faro/v1` envían `Cache-Control: no-store, private` (evita que un CDN guarde respuestas; en especial la de `/pair`).

**HPOS**: declaración en `before_woocommerce_init` como la skill. Sin WooCommerce: `/products` responde `{items: [], ..., woocommerce_active: false}`.

**Revocación**: formulario "Desconectar de Faro" (nonce + `manage_options`) y `DELETE /faro/v1/connection` firmado borran `faro_connection`. Desactivar el plugin no borra datos; `uninstall.php` borra las opciones `faro_*` y los transients `faro_*` (incluidos los de nonces e IP).

**`.wp-env.json`** (versiones fijas, no `latest-stable`):
```json
{
  "core": "WordPress/WordPress#<6.x fijada>",
  "phpVersion": "8.3",
  "plugins": ["https://downloads.wordpress.org/plugin/woocommerce.<versión fijada>.zip"],
  "mappings": { "wp-content/plugins/faro": "." },
  "config": { "WP_ENVIRONMENT_TYPE": "local" }
}
```
Más un `.wp-env.min.json` (o variable de entorno de wp-env) para la combinación mínima: PHP 8.1 + WordPress 6.0 sin WooCommerce. Si esa combinación tiene incompatibilidades que bloquean, se sube `Requires at least` a la versión mínima que pase y se documenta en el PR (§11 pregunta 2).

### 4.2 Motor Python (`motor-python`)

**Dependencias nuevas**: la librería SQLCipher elegida en T1 (ADR 0009), `httpx` pasa a dependencia de ejecución; `respx` y `freezegun` en dev. Todo con `uv add`.

**Módulos nuevos**
```
faro_engine/core/db/        connection.py  migrations.py  database.py  migrations/0001_initial.sql
faro_engine/core/secrets.py        # cliente del canal secret_request (futuros por id, 10 s)
faro_engine/core/audit.py          # inserta en audit_log; recibe eventos "audit" del núcleo
faro_engine/core/protocol.py       # + db_key, secret_response, audit, secret_request
faro_engine/core/routes/sites.py   faro_engine/core/schemas/sites.py
faro_engine/net/            urls.py (normalización)  guard.py (SSRF, IP fijada)  client.py (httpx, tiempos, reintentos)
faro_engine/wordpress/      signing.py  client.py (discover, pair, status, list, revoke)  errors.py (mapeo wp.* → site.*)
faro_engine/sites/          service.py (casos de uso: connect, reconnect, check, list_content, remove)
```
`faro_engine/net` y `faro_engine/wordpress` son de `motor-python` en F1a; en el cierre se añaden al mapa de `faro-arquitectura` (F2 los reutiliza `seo-datos` e `ingeniero-ia`).

**Arranque** (`__main__.py`):
1. Token (sin cambios). 2. Espera la línea `db_key` (10 s). 3. Abre `<data-dir>/profiles/<profile>.db`, aplica migraciones (ADR 0009 §4–5), sobrescribe el `bytearray` de la llave. 4. Si falla, `Database.state = unavailable(code)` y sigue. 5. `ready`.
- `--allow-local-sites` (rechazado con `sys.frozen`, código 2) y, en `--dev`, `FARO_ENGINE_DEV_DB_KEY`, `FARO_ENGINE_DEV_PROFILE_ID` y `FARO_ALLOW_LOCAL_SITES` desde `.env.local` (ADR 0009, 0012). En `--dev` el `--data-dir` por defecto es `apps/engine/.devdata/` (en `.gitignore`).
- *(Cierre T14)* En `--dev` el modo de sitios locales se activa con el argumento `--allow-local-sites` **o** con `FARO_ALLOW_LOCAL_SITES=1` en `.env.local` (cualquiera de los dos basta); ADR 0012 se alineó con el código (§13, fila 9).

**Base**: `Database` con una conexión y un candado; las consultas se ejecutan en un hilo (`anyio.to_thread`), transacciones explícitas. Dependencia FastAPI `get_db()` → `503` con el código de `Database.state` si no está disponible.

**Protocolo**: el lector de stdin (hilo) reparte `shutdown`, `secret_response` (resuelve el futuro por `id` con `loop.call_soon_threadsafe`) y `audit`. La escritura en stdout pasa por un candado (una línea JSON por evento, `flush`). El `run_id` llega en la cabecera `X-Faro-Run-Id`; sin ella, cualquier `secret_request` se rechaza localmente.

**Cliente WordPress** (`wordpress/client.py`, ADR 0011–0012):
- **Descubrimiento**: `GET {url}/wp-json/faro/v1` (índice del espacio de nombres); 200 con `"namespace": "faro/v1"` → `api_root = {url}/wp-json/`. Si 404 o no JSON: `GET {url}/?rest_route=/faro/v1` → `api_root = {url}/?rest_route=`. Si nada → `site.plugin_not_found`. Sigue hasta 3 redirecciones validadas y guarda la URL final como URL del sitio.
- `pair(code)`: `POST /faro/v1/pair` `{code, app_instance_id, app_version}`, sin reintentos; valida la respuesta (`connection_id`, `token` y `hmac_secret` de 43 base64url, `api_version == 1`).
- `status()`, `list(kind, page, per_page)`, `revoke()`: firmados con nonce y hora nuevos en cada intento.
- Mapeo de errores en §5.6.

**Casos de uso** (`sites/service.py`):

*connect(url, pairing_code)* (`connectSite`):
1. Validar código (`^\d{6}$` tras quitar espacios) → `site.invalid_code_format`. Normalizar URL y guardia SSRF.
2. Si existe un sitio con esa URL → `409 site.already_connected` (`details.site_id`).
3. Descubrir, `pair`.
4. `site_id` = UUID v7. `secret_request op=create ref=wp/<site_id>/token`.
5. `status()` firmado para comprobar la credencial y obtener metadatos. Si falla → `delete` del secreto creado, `revoke()` de buena fe, devolver el error.
6. Insertar `sites` + `site_connections` en una transacción. Si falla → `delete` del secreto + `revoke()` de buena fe.
7. Auditoría `site.connected`. `201 SiteOut`.

*reconnect(site_id, pairing_code)* (`reconnectSite`): sitio existente (si no, 404) → descubrir con su URL → `pair` → `set` del secreto → `status()` → actualizar la conexión a `active` → auditoría `site.reconnected`.

*check(site_id)* (`checkSiteConnection`): `get` del secreto (si falta → marcar `revoked` con `site.secret_missing`) → comparar `sha256(token)` con `token_sha256` (distinto → igual que falta) → `status()`. Correcto: actualiza metadatos, conteos, `last_checked_at`, `status = active`, `last_error_code = null`. Veredicto `wp.revoked` / `wp.connection_broken` / `wp.invalid_signature` → `status = revoked`, `revoked_at`, `last_error_code`, auditoría `site.revoked_detected`, y **devuelve `SiteOut`** (no es error). Si no se pudo comprobar (red, tiempo, 5xx, 429, `site.clock_skew`, `site.blocked`, `site.moved`, `site.bad_response`) → devuelve el error y **no cambia** el estado guardado.

*list_content(site_id, kind, cursor, limit)* (`listSiteContent`): como `check` para el secreto; lee una página remota (`page` = cursor, `per_page` = limit ≤ 100). Veredicto de revocación → marca `revoked` y devuelve el error `site.revoked`/`site.connection_broken`.

*remove(site_id)* (`removeSite`): si hay secreto, `revoke()` firmado (un reintento; `401 wp.revoked` cuenta como hecho) → `delete` del secreto → borrar el sitio (en cascada su conexión) → auditoría `site.removed` → `{remote_revoked}`.

**Logs**: `SENSITIVE_KEYS` ampliado y filtro por valor (ADR 0013). Nunca se registran cuerpos, cabeceras ni el código de vinculación.

**Cobertura estricta (95 %)**: añadir a `strict_modules` `core/secrets.py`, `core/db/migrations.py`, `net/guard.py`, `wordpress/signing.py`.

### 4.3 Núcleo Rust (`tauri-rust`)

**Perfil y llave** (`src/profile/`): `profiles.json`, generación de la llave con `getrandom` (64 hex en `SecretString`), reglas de ADR 0009 §3, uso de `SecretStore`. Se ejecuta antes de lanzar el motor en cada arranque (y reinicio).

**Supervisor** (`engine/`):
- Escritor de stdin único (tarea + `mpsc`): token → línea `db_key` → eventos (`shutdown`, `secret_response`, `audit`). La línea con la llave se construye en un `Zeroizing<String>`.
- Despachador de stdout: `ready`, `secret_request` → `SecretBroker`; otras líneas como hoy (sin registrar contenido).
- `EngineStatus` gana `database_error: FaroErrorData | null`, leído de `/health` en cada consulta de salud. El estado sigue siendo `ready` aunque la base no esté disponible.
- Lanzamiento con `--allow-local-sites` solo si `cfg!(debug_assertions)` **y** `FARO_ALLOW_LOCAL_SITES` vale exactamente `1` (sin contar espacios). *(Cierre T14)* La variable se lee primero del entorno del proceso y, si no está, de `.env.local` en la raíz del repositorio (`engine/mod.rs`, `local_sites_allowed` y `read_dev_env`). En release el lanzador nunca pasa el argumento, sea cual sea el entorno, y el motor empaquetado lo rechazaría con código 2.

**`SecretBroker`** (`src/secrets/`): gramática de referencias de `llavero-y-cifrado`, concesiones (ADR 0010 §3), validación de valores de `wp/*/token` (ADR 0011 §3), operaciones del llavero en `spawn_blocking`, respuesta por stdin, evento de auditoría por cada solicitud (`secret.used`, `secret.added`, `secret.replaced`, `secret.deleted` o `secret.denied`). Las concesiones se borran al reiniciar el motor. *(Cierre T14)* Además: concesiones atadas a generación del motor y perfil, revalidadas tras el candado del llavero; índice de sitios por perfil en `<app_data_dir>/profile-sites/<perfil>.json`; regla del `delete` de `{new}` intentado (§13, filas 1–4; ADR 0010, actualización 2026-10-01).

**`engine_call`** (`src/commands/engine.rs`):
- `engine-operations.json` incrustado (`include_str!`) y validado en una prueba (métodos, rutas, plantillas de `secrets` con la gramática).
- Solo `operationId` de la lista (`engine.operation_not_allowed`). Parámetros de ruta: `^[A-Za-z0-9_-]{1,64}$` y, si aparecen en `secrets`, UUID válido (`engine.invalid_request`). Cuerpo JSON ≤ 256 KB.
- Motor no `ready` → `engine.not_ready`. Tiempo: `timeout_seconds` de la operación (por defecto 30 s) → `engine.timeout`.
- Crea la concesión si hay `secrets`, añade `Authorization` y `X-Faro-Run-Id`, reenvía; devuelve el JSON de 2xx y reenvía **sin cambios** los errores `{code, message, details}` del motor (ADR 0002). Nunca registra cuerpos, parámetros de consulta ni respuestas.

**Auditoría de la Bóveda**: `vault_add_key`, `vault_test_key`, `vault_delete_key` emiten `secret.added`/`secret.replaced`, `secret.tested`, `secret.deleted` (actor `user`, `details.provider`, `result`) por el canal de auditoría con búfer de 500 (ADR 0010 §4).

**`wp_plugin_export`**: en builds de depuración lee `packages/wp-plugin/dist/faro-wordpress.zip` (ruta desde `CARGO_MANIFEST_DIR`); en release, el recurso empaquetado (se configura en la fase de release, §12). Copia a `app.path().download_dir()` como `faro-wordpress.zip` (reemplaza si existe; copia a un temporal y renombra) y muestra el archivo en el explorador con `tauri-plugin-opener` **desde Rust** (`reveal_item_in_dir`), **sin** conceder permisos `opener:*` a la interfaz. Devuelve solo `{file_name}`. Errores: `plugin.package_missing`, `plugin.export_failed`.

**Logs**: `MakeWriter` con filtro por valor (ADR 0013).

**Capabilities**: `core:default` se sustituye por los permisos de eventos que la interfaz usa (`core:event:allow-listen`, `core:event:allow-unlisten` y los que la prueba de `tests/acl.rs` demuestre necesarios). Nuevos: `allow-engine-call`, `allow-wp-plugin-export`.

### 4.4 Interfaz (`frontend-react`)

- `lib/api/engineCall.ts`: `api.call(operationId, {path, query, body})` tipado con `packages/shared/engine.d.ts` sobre `invoke("engine_call", { request })`.
- `lib/api/sites.ts` (`listSites`, `connectSite`, `reconnectSite`, `checkSiteConnection`, `listSiteContent`, `removeSite`) y `lib/api/wpPlugin.ts` (`exportWpPlugin`).
- `lib/api/types.ts`: `EngineStatus.database_error`.
- `features/sites/`: `SitesSection.tsx`, `SiteCard.tsx`, `ConnectSiteDialog.tsx` (3 pasos, también para volver a conectar), `SiteContentSheet.tsx`, `RemoveSiteDialog.tsx`, `useSites.ts` (`queryKey: ["sites"]`), `useAutoCheckSites.ts` (`Set<site_id>` a nivel de módulo, como `useAutoTestKeys`).
- `pages/SettingsPage.tsx` con pestañas; `features/home/NextSteps.tsx` con la lista de pasos; `EngineStatusCard` con el aviso de base.
- **`connectSite` y `reconnectSite` no usan `useMutation`** con el código como variable (mismo motivo que la clave en F0): se llaman desde el manejador del formulario y luego se invalida `["sites"]`.
- `listSiteContent` con `useQuery` (`["siteContent", siteId, kind, cursor]`, `staleTime` 60 s); pila de cursores en el estado del panel para **Anteriores**.
- i18n: claves nuevas en `settings` (`sites.*`), `home` y `errors` en `es`; `en` y `pt-BR` con `[TODO] `.

### 4.5 Contratos (`devops-release` + `motor-python`)

- `scripts/generate-contracts.mjs` copia `x-faro-timeout-seconds` → `timeout_seconds` y `x-faro-secrets` → `secrets` a `engine-operations.json`. Cualquier diferencia en `secrets` entre dos versiones requiere revisión de `revisor-seguridad` (se indica en la plantilla de PR).

### 4.6 Build, CI y repositorio (`devops-release`)

- **`npm run build:wp-plugin`** (`scripts/build-wp-plugin.mjs`): zip determinista (entradas ordenadas, fecha fija) en `packages/wp-plugin/dist/faro-wordpress.zip` con carpeta raíz `faro/`, que incluye `faro.php`, `uninstall.php`, `readme.txt`, `LICENSE`, `includes/`, `admin/`, `languages/` y **excluye** `tests/`, `vendor/`, `dist/`, `composer.*`, configuraciones y `.wp-env*.json`. Usa una librería zip de Node mantenida y fijada. `npm run setup` lo ejecuta. `dist/` en `.gitignore`.
- **Licencias** (ADR 0008): línea de excepción en el `LICENSE` y el `README.md` de la raíz.
- **`.env.local.example`**: `FARO_ALLOW_LOCAL_SITES=0`, `FARO_ENGINE_DEV_DB_KEY=` y `FARO_ENGINE_DEV_PROFILE_ID=` vacíos con instrucciones para generarlos (`python -c "import secrets; print(secrets.token_hex(32))"`), nunca valores reales.
- **`.gitignore`**: `apps/engine/.devdata/`, `packages/wp-plugin/vendor/`, `packages/wp-plugin/dist/`.
- **gitleaks**: excepción por ruta **y** valor para `packages/shared/fixtures/wp-signature-v1.json`.
- **Dependabot**: ecosistema `composer` (`packages/wp-plugin`).

**Trabajos de CI nuevos o cambiados** (todos con acciones fijadas por SHA, `permissions: contents: read`, `timeout-minutes`, sin secretos, sin `pull_request_target`; todos en `needs` de `ci-ok`):

| Trabajo | Runner | Timeout | Pasos |
| --- | --- | --- | --- |
| `engine` (cambia) | matriz `ubuntu-latest`, `windows-latest`, **`macos-latest`** | 20 min | igual que F0 (valida la librería SQLCipher en las tres plataformas) |
| `core` (cambia) | `windows-latest` | 45 min | + `cargo llvm-cov` con umbral 80 % global y 95 % en `vault/`, `secrets/`, `profile/`, `engine/protocol.rs` (reemplaza `cargo test`, mismas pruebas) |
| `wp-plugin` (nuevo) | `ubuntu-latest` | 10 min | `setup-php` (8.1) + caché de Composer → `composer install` → `composer phpcs` (WordPress Coding Standards + PHPCompatibilityWP 8.1+) → `composer phpstan` (nivel 6+, `szepeviktor/phpstan-wordpress`, stubs de WooCommerce) → `npm run build:wp-plugin` y comprobar el contenido del zip (sin `tests/`, `vendor/`; con `LICENSE`) |
| `wp-plugin-integration` (nuevo) | matriz `ubuntu-latest` × {`actual`: PHP 8.3 + WP 6.x fijada + WooCommerce fijada; `minimo`: PHP 8.1 + WP 6.0 sin WooCommerce} | 25 min | `setup-node` + `npm ci` → `setup-php` + `composer install` → caché de `~/.wp-env` (clave: hash de `.wp-env*.json`) → `npx @wordpress/env start` (un reintento si falla) → PHPUnit dentro de `tests-cli` (en `actual`, con HPOS activado y desactivado) → en `actual`: `setup-uv` + `uv sync` y `pytest -m wp_env` (prueba de integración motor ↔ plugin, §9.4) → `wp-env stop` |
| `audit` (cambia) | `ubuntu-latest` | 15 min | + `composer audit` en `packages/wp-plugin` |

**Por qué wp-env en la CI** (costo/tiempo): el repositorio es público y los minutos en runners estándar son gratis (ADR 0005); los trabajos de wp-env tardan unos 6–10 min cada uno (descarga de imágenes, WordPress y WooCommerce) y corren **en paralelo** con `core` en Windows (≈20–30 min), que sigue siendo el más largo, así que no alargan la CI. A cambio, es la única verificación automática del código más sensible del plugin (vinculación y firma) contra WordPress y WooCommerce reales. Riesgos: descargas inestables y límites de Docker Hub para descargas anónimas → versiones fijadas, caché de `~/.wp-env` y un reintento; si se vuelve inestable, se valora un espejo de imágenes (ADR nuevo si cambia la política de CI). Nunca runners propios.

---

## 5. Contratos

### 5.1 Protocolo por stdin/stdout

Definido en ADR 0010 §1–2. Resumen:

| Dirección | Evento | Campos |
| --- | --- | --- |
| núcleo → motor | 1.ª línea | token (43 base64url) |
| núcleo → motor | `db_key` | `profile` (UUID) + `key` (64 hex) **o** `error` (`db.key_missing` \| `vault.keyring_unavailable`) |
| núcleo → motor | `shutdown` | — |
| motor → núcleo | `ready` | `port`, `version`, `pid` |
| motor → núcleo | `secret_request` | `id`, `run_id`, `op` (`get`\|`create`\|`set`\|`delete`), `ref`, `value` (solo `create`/`set`) |
| núcleo → motor | `secret_response` | `id` + `value` \| `ok: true` \| `error` (`vault.invalid_ref`, `vault.secret_not_allowed`, `vault.not_found`, `vault.already_exists`, `vault.invalid_input`, `vault.keyring_unavailable`) |
| núcleo → motor | `audit` | `occurred_at`, `actor`, `action`, `secret_ref`, `run_id`, `result`, `details` |

### 5.2 Endpoints del motor

Todos con Bearer + `Host` (F0). Tipos:

```ts
type SiteConnectionStatus = "active" | "revoked";
interface SiteOut {
  id: string; url: string; name: string | null; created_at: string;
  connection: {
    status: SiteConnectionStatus;
    last_error_code: string | null;
    connected_at: string; last_checked_at: string | null; revoked_at: string | null;
    plugin_version: string | null; wp_version: string | null;
    woocommerce: { active: boolean; version: string | null; hpos_enabled: boolean | null } | null;
    seo_plugin: "yoast" | "rank_math" | "none" | null;
    counts: { pages: number; posts: number; products: number | null } | null;
  } | null; // null reservado para sitios solo con URL (F2)
}
type ContentKind = "page" | "post" | "product";
interface SiteContentItem { remote_id: number; kind: ContentKind; title: string; url: string; slug: string; modified_at: string; }
interface SiteContentPage { items: SiteContentItem[]; next_cursor: string | null; total: number; total_pages: number; woocommerce_active: boolean; }
interface HealthOut { status: "ok"; version: string; database: { state: "ready" | "unavailable"; error_code: string | null; newer_schema: boolean } }
```

| Método | Ruta | `operationId` | Entrada | Salida | `x-faro-secrets` | Timeout | Errores |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/health` | `getHealth` | — | `HealthOut` (**campo `database` nuevo, aditivo**) | — | — | F0 |
| GET | `/sites` | `listSites` | — | `{items: SiteOut[], next_cursor: null}` | — | 10 s | `db.*` (503) |
| POST | `/sites` | `connectSite` | `{url: string, pairing_code: string}` | `201 SiteOut` | `wp/{new}/token`: `create` (+`delete` de lo creado) | 60 s | `site.invalid_url`, `site.https_required`, `site.address_not_allowed`, `site.invalid_code_format`, `site.already_connected` (409), errores de red y del sitio (§5.6), `vault.*`, `db.*` |
| PUT | `/sites/{site_id}/connection` | `reconnectSite` | `{pairing_code: string}` | `SiteOut` | `wp/{site_id}/token`: `set` | 60 s | `site.not_found` (404) y los de `connectSite` salvo `already_connected` |
| POST | `/sites/{site_id}/check` | `checkSiteConnection` | — | `SiteOut` (revocación = veredicto, no error) | `wp/{site_id}/token`: `get` | 45 s | `site.not_found`, red y sitio (§5.6) |
| GET | `/sites/{site_id}/content` | `listSiteContent` | query `kind` (obligatorio), `cursor` (opcional, número de página como texto), `limit` (1–100, 50 por defecto) | `SiteContentPage` | `wp/{site_id}/token`: `get` | 45 s | `site.not_found`, `site.revoked`, `site.connection_broken`, red y sitio |
| DELETE | `/sites/{site_id}` | `removeSite` | — | `{remote_revoked: boolean}` | `wp/{site_id}/token`: `get`, `delete` | 45 s | `site.not_found`, `vault.keyring_unavailable`, `db.*` |

El motor aplica su plazo total 5 s por debajo del timeout del núcleo (ADR 0012). Rutas nuevas con al menos una prueba en `tests/routes/`.

### 5.3 Rutas REST del plugin (`/wp-json/faro/v1/`)

| Método | Ruta | Autorización | Entrada | Salida 200 | Errores |
| --- | --- | --- | --- | --- | --- |
| POST | `/pair` | código pendiente + límite por IP | `{code: "123456", app_instance_id: uuid, app_version: string}` | `{api_version: 1, connection_id, token, hmac_secret, site: {name, home_url, wp_version, plugin_version}}` | 400 `wp.invalid_input`, 403 `wp.pairing_invalid` (`data.attempts_left`), 410 `wp.pairing_expired`, 429 `wp.rate_limited`, 403 `wp.insecure_site` |
| GET | `/status` | firma | — | `{api_version: 1, plugin_version, wp_version, site_name, home_url, woocommerce: {active, version, hpos_enabled}, seo_plugin, counts: {pages, posts, products}, connection: {connection_id, created_at}}` | 401 `wp.invalid_signature` \| `wp.revoked` \| `wp.connection_broken` \| `wp.stale_request` |
| GET | `/pages`, `/posts`, `/products` | firma | `page` (≥ 1), `per_page` (1–100, 50) con `sanitize_callback`/`validate_callback` | `{items: [{id, title, url, slug, modified_at}], page, per_page, total, total_pages, woocommerce_active}` | 400 `wp.invalid_input`, 401 como `/status` |
| DELETE | `/connection` | firma | — | `{revoked: true}` | 401 como `/status` |

Errores como `WP_Error( '<code>', __( '<mensaje>', 'faro' ), [ 'status' => N ] )`, sin detalles de la firma. `permission_callback` nunca `__return_true`.

### 5.4 Comandos Tauri

| Comando | Argumentos (`invoke`) | Devuelve | Errores |
| --- | --- | --- | --- |
| `engine_call` | `{ request: { operation: string, path?: Record<string,string>, query?: Record<string,string\|number\|boolean>, body?: unknown } }` | JSON de la respuesta del motor | `engine.operation_not_allowed`, `engine.invalid_request`, `engine.not_ready`, `engine.timeout`, y los del motor sin cambios |
| `wp_plugin_export` | — | `{ file_name: string }` | `plugin.package_missing`, `plugin.export_failed` |
| `engine_status` (cambia) | — | `EngineStatus` con `database_error: FaroErrorData \| null` (aditivo) | — |

### 5.5 Permisos

- `permissions/engine.toml`: + `allow-engine-call`. `permissions/wp_plugin.toml`: `allow-wp-plugin-export`. Ambos en `COMMANDS` de `build.rs`, `generate_handler!`, `capabilities/main.json` y `tests/acl.rs`.
- `capabilities/main.json`: sin `core:default`; permisos de eventos concretos (§4.3).
- Plugin de Tauri nuevo: `tauri-plugin-opener`, **solo desde Rust**; ninguna capability `opener:*`. CSP sin cambios (ADR 0006).

### 5.6 Catálogo de errores nuevos (`locales/es/errors.json`)

| `code` | Mensaje |
| --- | --- |
| `engine.not_ready` | El motor de Faro todavía no está listo. Espera unos segundos e intenta de nuevo. |
| `engine.operation_not_allowed` | Esta acción no está permitida. Reinicia Faro; si se repite, escríbenos. |
| `engine.timeout` | Faro tardó demasiado en responder. Intenta de nuevo. |
| `engine.secrets_unavailable` | Esta acción no está disponible en el modo de desarrollo externo. |
| `db.key_missing` | No encontramos la llave de tus datos en el llavero de tu computadora. Tus datos siguen guardados, pero Faro no puede abrirlos. |
| `db.wrong_key` | No pudimos abrir tus datos de Faro con la llave guardada en tu computadora. |
| `db.migration_failed` | No pudimos actualizar tus datos de Faro. Tus datos anteriores están a salvo en una copia. Intenta de nuevo. |
| `db.migration_tampered` | Los datos de Faro se modificaron fuera de la app y no es seguro abrirlos. |
| `db.too_new` | Tus datos son de una versión más nueva de Faro. Actualiza Faro para abrirlos. |
| `db.unavailable` | Tus datos de Faro no están disponibles ahora. Reinicia Faro. |
| `site.invalid_url` | Esa dirección no parece válida. Escríbela así: https://tutienda.com |
| `site.https_required` | Faro solo se conecta a sitios con HTTPS (el candado del navegador). |
| `site.address_not_allowed` | Esa dirección apunta a esta computadora o a tu red local. Faro solo se conecta a sitios publicados en internet. |
| `site.unreachable` | No pudimos conectar con tu sitio. Revisa la dirección y que el sitio esté en línea. |
| `site.tls_error` | El certificado de seguridad de tu sitio no es válido. Pide a tu proveedor de hosting que lo revise. |
| `site.timeout` | Tu sitio tardó demasiado en responder. Intenta de nuevo en unos minutos. |
| `site.server_error` | Tu sitio tuvo un problema al responder. Intenta de nuevo en unos minutos. |
| `site.rate_limited` | Tu sitio recibió demasiados intentos. Espera 15 minutos e intenta de nuevo. |
| `site.plugin_not_found` | No encontramos el plugin de Faro en ese sitio. Revisa que esté instalado y activado. |
| `site.plugin_outdated` | El plugin de Faro de tu sitio no es compatible con esta versión. Instala la versión más reciente. |
| `site.blocked` | Algo en tu sitio está bloqueando la conexión, como un plugin de seguridad. Permite el acceso a la API de WordPress e intenta de nuevo. |
| `site.moved` | Tu sitio respondió desde otra dirección. Vuelve a conectarlo con la dirección nueva. |
| `site.bad_response` | Tu sitio respondió algo que no esperábamos. Revisa que el plugin de Faro esté actualizado. |
| `site.response_too_large` | Tu sitio envió una respuesta demasiado grande. Intenta de nuevo. |
| `site.invalid_code_format` | El código tiene 6 números. |
| `site.pairing_code_invalid` | El código no coincide. Revísalo en WordPress (Ajustes → Faro). Te quedan {{attempts_left}} intentos. |
| `site.pairing_code_expired` | Este código ya no sirve: caducó o ya se usó. Genera uno nuevo en WordPress. |
| `site.already_connected` | Este sitio ya está en Faro. |
| `site.not_found` | No encontramos ese sitio en Faro. Puede que ya lo hayas quitado. |
| `site.revoked` | Tu sitio se desconectó de Faro desde WordPress. Vuelve a conectarlo con un código nuevo. |
| `site.connection_broken` | La conexión dejó de funcionar porque cambiaron las claves de seguridad de WordPress. Vuelve a conectarlo con un código nuevo. |
| `site.auth_failed` | Tu sitio rechazó la conexión. Vuelve a conectarlo con un código nuevo. |
| `site.secret_missing` | Falta la conexión de este sitio en el llavero de tu computadora. Vuelve a conectarlo con un código nuevo. |
| `site.clock_skew` | La hora de tu computadora no coincide con la de tu sitio. Activa la fecha y hora automáticas e intenta de nuevo. |
| `vault.invalid_ref`, `vault.secret_not_allowed`, `vault.secret_timeout` | Faro no pudo usar una credencial guardada. Reinicia Faro e intenta de nuevo. |
| `plugin.package_missing` | Esta versión de Faro no incluye el plugin de WordPress. |
| `plugin.export_failed` | No pudimos guardar el plugin en tu carpeta Descargas. Revisa que haya espacio e intenta de nuevo. |

**Mapeo de respuestas del sitio → código del motor**

| Respuesta del sitio | Código |
| --- | --- |
| `403 wp.pairing_invalid` | `site.pairing_code_invalid` (`details.attempts_left`) |
| `410 wp.pairing_expired` | `site.pairing_code_expired` |
| `429` (cualquiera, tras el reintento) | `site.rate_limited` |
| `403 wp.insecure_site` | `site.https_required` |
| `401 wp.revoked` / `wp.connection_broken` / `wp.invalid_signature` | `site.revoked` / `site.connection_broken` / `site.auth_failed` (veredictos) |
| `401 wp.stale_request` | `site.clock_skew` |
| Otro 401/403 (p. ej. `rest_forbidden` de un plugin de seguridad, HTML de un WAF) | `site.blocked` |
| 404 de la ruta | `site.plugin_not_found` |
| 3xx en una petición firmada | `site.moved` |
| 5xx (tras reintentos) | `site.server_error` |
| JSON inválido o con otra forma | `site.bad_response` |
| `api_version` ≠ 1 | `site.plugin_outdated` |
| error de red / DNS | `site.unreachable`; TLS → `site.tls_error`; tiempo → `site.timeout` |

---

## 6. Datos

Base `<data-dir>/profiles/<profile_id>.db` (ADR 0009). `profiles.json` en `<app_data_dir>` (no secreto). Migración `0001_initial.sql` (además de `schema_migrations` de la skill), y `PRAGMA user_version = 1` como piso de compatibilidad (lo fija el ejecutor, no el `.sql`):

```sql
CREATE TABLE IF NOT EXISTS sites (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,               -- normalizada (ADR 0012), sin barra final
  name TEXT,                       -- nombre del sitio desde /status
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS sites_url_uq ON sites(url);

CREATE TABLE IF NOT EXISTS site_connections (
  id TEXT PRIMARY KEY,             -- se envía como app_instance_id
  site_id TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('wp_plugin')),
  api_root TEXT NOT NULL,          -- https://…/wp-json/ o https://…/?rest_route=
  remote_connection_id TEXT NOT NULL,
  token_sha256 TEXT NOT NULL,      -- hex; el token vive solo en el llavero
  secret_ref TEXT NOT NULL,        -- wp/<site_id>/token
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
  last_error_code TEXT,
  plugin_version TEXT,
  wp_version TEXT,
  woocommerce_active INTEGER CHECK (woocommerce_active IN (0, 1)),
  woocommerce_version TEXT,
  hpos_enabled INTEGER CHECK (hpos_enabled IN (0, 1)),
  seo_plugin TEXT CHECK (seo_plugin IN ('yoast', 'rank_math', 'none')),
  pages_count INTEGER,
  posts_count INTEGER,
  products_count INTEGER,
  connected_at TEXT NOT NULL,
  last_checked_at TEXT,
  revoked_at TEXT
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS site_connections_site_kind_uq ON site_connections(site_id, kind);

CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('user', 'agent', 'system')),
  action TEXT NOT NULL,
  secret_ref TEXT,
  run_id TEXT,
  result TEXT NOT NULL CHECK (result IN ('ok', 'denied', 'error')),
  details TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE INDEX IF NOT EXISTS audit_log_occurred_at_idx ON audit_log(occurred_at);
```

- `audit_log` no tiene clave foránea a `sites`: el registro sobrevive a quitar un sitio. Solo se inserta. Sin retención en F1a.
- Acciones de auditoría: `secret.added`, `secret.replaced`, `secret.tested`, `secret.used`, `secret.denied`, `secret.deleted`, `site.connected`, `site.reconnected`, `site.revoked_detected`, `site.removed`.
- No se crea `organizations` (el ejemplo de la skill es ilustrativo; se añadirá cuando se use, como columna `NULL`).
- Fixture `tests/fixtures/db/v0001.sql` (sin datos personales) para probar la migración 0002 cuando exista.

---

## 7. Seguridad

**Secretos que toca**
| Secreto | Dónde vive | Recorrido |
| --- | --- | --- |
| Llave SQLCipher | llavero `db/<profile>/key` | núcleo → stdin (2.ª línea) → `PRAGMA key` → se sobrescribe. Nunca por `secret_request`. |
| Token + secreto HMAC del sitio | llavero `wp/<site_id>/token` (JSON v1) | sitio → motor (respuesta de `/pair`) → stdout `create` → núcleo → llavero. Uso: llavero → núcleo → stdin `get` → motor (memoria de la operación) → cabeceras firmadas por HTTPS. Nunca en SQLite (solo `sha256(token)`), la interfaz, logs ni respuestas del motor. |
| Código de vinculación | de un solo uso, 10 min | wp-admin → usuario → campo de la interfaz → `engine_call` (cuerpo) → motor → `/pair`. No se registra ni se guarda. |
| Token de sesión del motor | memoria | sin cambios |
| En WordPress | opción `faro_connection` | `sha256(token)` + secreto HMAC cifrado con clave derivada de `wp_salt('auth')` |

**Permisos Tauri**: `allow-engine-call`, `allow-wp-plugin-export`; fuera `core:default`; `tauri-plugin-opener` sin permisos para la interfaz; CSP sin cambios.

**Aprobación y autonomía**: F1a **no publica ni gasta**: solo lee y crea/revoca la propia conexión, que el usuario inicia de forma explícita. No pasa por la Bandeja. Los endpoints de escritura (`/drafts`, `/seo-meta`) no existen todavía.

**Controles clave**
- Concesiones por operación compiladas en el núcleo; `db/*` nunca sale por el canal; `{new}` solo crea referencias inexistentes (ADR 0010).
- SSRF, IP fijada, sin redirecciones en peticiones firmadas, TLS verificado, límites de tamaño y tiempo; sitios locales solo en depuración con doble llave (ADR 0012).
- Plugin: `permission_callback` en todas las rutas; código de 6 dígitos de un solo uso, 10 min, 5 intentos, límite por IP; `hash_equals` en todas las comparaciones; nonces guardados solo tras una firma válida; `Cache-Control: no-store`; nonces y `manage_options` en wp-admin; `esc_*` en salidas y `sanitize_*` en entradas; el plugin no hace peticiones salientes.
- Contenido remoto (títulos, URLs) siempre como texto en React; sin `dangerouslySetInnerHTML`.
- Separación de licencias y de código app ↔ plugin (ADR 0008).
- Filtro de logs por valor en ambas capas (ADR 0013).

**Revisión obligatoria de `revisor-seguridad`**: T3 (plugin), T5/T6 (base, llave, protocolo), T7/T8 (secretos, concesiones, `engine_call`, auditoría, logs, `wp_plugin_export`, capabilities), T9 (cliente WordPress, SSRF), T10 (workflows y zip). Secciones 1, 2, 3, 5, 7, 9 y 10 de `revision-seguridad`.

---

## 8. Tareas

**Integración**: cada tarea entra en `main` por PR con `ci-ok` en verde. Los cambios de protocolo tienen que llegar a la vez a ambos lados para que `engine_real` siga en verde: **T5 + T6** comparten rama y PR (`f1a/base-cifrada`), y **T7 + T8** también (`f1a/secretos`). Los PR que cambian endpoints incluyen `npm run contracts`. El PR del plugin (T3) incluye los trabajos de CI del plugin (T4) para que el propio PR se valide.

**Orden y paralelismo**
```
T0 ─┬─ T1 ──► T5+T6 ──► T7+T8 ──► T9 ──► T11 ──┐
    ├─ T2 ─────────────────────────────────────┤
    ├─ T3+T4 (tras resolver Docker) ────────────┼─► T10 ─► T12 ─► T13 ─► T14
    └─ T4b (contratos) ─────────► (antes de T8) │
T11 puede empezar con mockIPC al aprobarse la spec; se integra tras T9.
```

| # | Agente | Tarea | Depende de | Terminado cuando |
| --- | --- | --- | --- | --- |
| T0 | usuario | Aprobar esta spec y los ADR 0008–0013; resolver el arranque del motor de Docker Desktop (WSL 2). | — | Spec en estado "aprobada"; `docker run hello-world` funciona. |
| T1 | motor-python | Verificación de la librería SQLCipher con los 7 criterios de ADR 0009 (primero `sqlcipher3-wheels`, luego `apsw-sqlite3mc`). Rama de prueba con el trabajo `engine` en las tres plataformas. | T0 (spec) | Informe con cada criterio cumplido/no cumplido y evidencia (entradas del lock, ejecución de CI, script PyInstaller en Windows); `arquitecto` actualiza ADR 0009 con la librería elegida. |
| T2 | tauri-rust | Filtro de logs por valor (ADR 0013) + sustituir `core:default` por permisos de eventos con prueba en `tests/acl.rs`. | T0 | Prueba que registra secretos falsos de cada patrón y no aparecen en el archivo; la interfaz sigue recibiendo `engine://status`; `acl.rs` demuestra que un comando o evento no concedido se rechaza; `clippy` y pruebas en verde. |
| T3 | wordpress-php | Plugin completo (§4.1, §5.3): estructura, `LICENSE` GPL y cabeceras (ADR 0008), vinculación, cripto, firma, rutas, contenido, estado, HPOS, pantalla de wp-admin, `uninstall.php`, `readme.txt`, `faro.pot`, `.wp-env.json` y variante mínima. Genera `packages/shared/fixtures/wp-signature-v1.json` (≥ 8 casos: sin cuerpo, con cuerpo, consulta con varios parámetros y caracteres especiales, modo `rest_route`). | T0 (Docker resuelto) | PHPCS, PHPStan (nivel ≥ 6) y PHPUnit en verde en wp-env con WooCommerce (HPOS activado y desactivado) y en la combinación mínima; todas las pruebas de §9.3; ningún archivo de `apps/` copiado o importado; firma indicando revisión de `revisor-seguridad`. |
| T4 | devops-release | En el PR de T3: trabajos `wp-plugin` y `wp-plugin-integration` (sin el paso de pytest hasta T9), `composer audit`, Dependabot `composer`, `build:wp-plugin` (zip determinista), `.gitignore`, excepción de gitleaks, línea de excepción de licencia en `LICENSE` y `README.md` de la raíz (ADR 0008). | T3 (en paralelo, mismo PR) | El PR pasa `ci-ok` con los trabajos nuevos; un error de PHPCS, PHPStan o PHPUnit hace fallar `ci-ok`; el zip tiene carpeta `faro/`, incluye `LICENSE` y no incluye `tests/` ni `vendor/`; dos builds seguidos producen el mismo hash. |
| T4b | devops-release + motor-python | Ampliar `generate-contracts.mjs` con `timeout_seconds` y `secrets` (§4.5). | T0 | `npm run contracts` es determinista; con una ruta de ejemplo en una prueba del generador, el JSON contiene los campos. |
| T5 | motor-python | Base cifrada (§4.2, ADR 0009): `core/db/`, `0001_initial.sql`, piso `user_version`, copias, `db_key` en el arranque, estado de la base en `/health`, `get_db()` con 503, `--dev` con base de desarrollo; trabajo `engine` con `macos-latest` (con devops-release). | T1 | Pruebas obligatorias de `migraciones-sqlite` + `db.too_new` y `db.newer_schema`; `/health` informa `database`; `ruff`, `mypy`, cobertura en verde en las tres plataformas; contratos regenerados. |
| T6 | tauri-rust | Perfil y llave (§4.3, ADR 0009 §3), escritor de stdin único y línea `db_key`, `EngineStatus.database_error`. Mismo PR que T5. | T2, T5 (misma rama) | Pruebas con `MemoryStore`: primer arranque crea llave antes de `profiles.json`; `.db` existente sin llave → `db.key_missing` y **no** genera llave; llavero caído → error en la línea; la llave no aparece en logs capturados; `engine_real` arranca el motor real con una base temporal y `/health` informa `ready`. |
| T7 | motor-python | Canal de secretos (`core/secrets.py`), `X-Faro-Run-Id`, auditoría (`core/audit.py` + eventos `audit` por stdin), logs por nombre y valor (ADR 0013). | T5 | Pruebas con canal simulado: respuesta correcta, error, tiempo agotado (10 s simulados), respuesta con `id` desconocido ignorada; evento `audit` inválido descartado sin registrar contenido; logs sin secretos falsos; cobertura 95 % en `core/secrets.py`. |
| T8 | tauri-rust | `SecretBroker` y concesiones (ADR 0010), `engine_call`, auditoría de la Bóveda con búfer, `wp_plugin_export` con `tauri-plugin-opener` solo desde Rust, permisos y ACL. Mismo PR que T7. | T6, T7, T4b | Pruebas: cada regla de ADR 0010 §3 (gramática, `run_id` caducado, ref no concedida, `db/*` siempre rechazada, `{new}` una vez y sin reemplazar, `delete` solo de lo creado, valor con forma inválida); `engine_call` rechaza operaciones fuera de la lista y parámetros inválidos, reenvía errores del motor sin cambios, aplica timeout; `wp_plugin_export` con carpeta de descargas temporal; ningún valor de retorno ni `Debug` contiene secretos; cobertura ≥ 95 % en `secrets/` y `profile/`. |
| T9 | motor-python | `faro_engine/net`, `faro_engine/wordpress`, `faro_engine/sites`, rutas `/sites*` (§4.2, §5.2, §5.6), `--allow-local-sites`, contratos. Prueba de integración `-m wp_env` y su paso en el trabajo `wp-plugin-integration` (con devops-release). | T8, T3 | Pruebas de §9.2 en verde; vectores de firma compartidos coinciden; `pytest -m wp_env` pasa contra wp-env en CI; cobertura 95 % en `net/guard.py` y `wordpress/signing.py`; contratos regenerados. |
| T10 | devops-release | Cobertura de Rust (`cargo llvm-cov`, umbrales de §4.6), `.env.local.example`, `.gitignore` de `.devdata`, plantilla de PR con la casilla "cambia `secrets` en engine-operations.json → revisor-seguridad", README (wp-env, `FARO_ALLOW_LOCAL_SITES`, `build:wp-plugin`). | T8, T9 | `core` falla si la cobertura baja del umbral; README permite montar wp-env y conectar un sitio local siguiendo solo sus pasos. |
| T11 | frontend-react | Interfaz de §3 y §4.4: pestañas de Configuración, Sitios conectados (4 estados), asistente, volver a conectar, ver contenido, quitar, comprobación automática, Inicio, aviso de base, `errors.json`. | spec aprobada (mockIPC); integración tras T9 | Estados vacío/cargando/error/éxito de cada pantalla probados con `mockIPC`; comprobación automática una vez por sitio y sesión; el código no está en caché de Query, `localStorage` ni `console`; cero textos en duro; `lint`, `typecheck`, `test` en verde; flujo real contra wp-env funciona en `npm run dev`. |
| T12 | qa-pruebas | Pruebas de §9 que falten en cada capa, prueba de integración y lista de verificación manual `docs/qa/2026-xx-xx-f1a-verificacion-manual.md` (§10), ejecutada en Windows 11. | T3–T11 | Tabla de resultados de `qa-pruebas`; umbrales de cobertura cumplidos; lista manual completa. |
| T13 | revisor-seguridad | Revisión de T2, T3, T5–T10 con las secciones de §7. | T12 | `APROBADO` o `APROBADO CON CAMBIOS` con los cambios aplicados y revisados de nuevo. |
| T14 | arquitecto | Cierre: marcar la spec como implementada, sección de diferencias, actualizar ADR 0009 (librería) y los que cambien; pedir al agente principal que actualice las skills `llavero-y-cifrado`, `wordpress-plugin`, `migraciones-sqlite`, `tauri-sidecar-python`, `contratos-api-local` y `faro-arquitectura` (módulos `net`, `wordpress`, `sites`). | T13 | Spec, ADR y skills reflejan lo construido. |

---

## 9. Pruebas (para `qa-pruebas`)

Sin servicios reales salvo wp-env local. `TZ=UTC`, relojes inyectados, sin `sleep`. Secretos de prueba con forma evidente (`test-…`) y en la lista de excepciones de gitleaks.

### 9.1 Núcleo Rust
- Perfil y llave: casos de T6; `profiles.json` atómico (fallo a mitad no deja archivo corrupto).
- Protocolo: la 2.ª línea es `db_key` válida o con `error`; la llave no está en logs; `secret_request` malformada se ignora sin registrar contenido.
- `SecretBroker`: todos los casos de T8; cada solicitud emite un evento `audit` sin valor; las concesiones desaparecen tras el reinicio del motor.
- `engine_call`: lista permitida, parámetros de ruta, límite de cuerpo, `engine.not_ready`, timeout, reenvío de errores, cabecera `X-Faro-Run-Id` solo en operaciones con `secrets`.
- Búfer de auditoría: se envía al quedar listo el motor; con más de 500 descarta los más viejos.
- Filtro de logs por valor (cada patrón de ADR 0013) y ACL sin `core:default`.
- `wp_plugin_export`: zip ausente → `plugin.package_missing`; destino no escribible → `plugin.export_failed`; reemplaza un archivo existente.

### 9.2 Motor
- Base: todas las obligatorias de `migraciones-sqlite` + piso `user_version` (`db.too_new`, `db.newer_schema`), `db_key` con `error`, sin 2.ª línea → base no disponible, rutas con base caída → 503 con el código.
- `net/guard.py`: cada rango prohibido (IPv4 e IPv6, incluidas IPv4 mapeadas), nombre que resuelve a una pública y una privada → rechazado, IP fijada (una segunda resolución distinta no se usa), usuario/contraseña en URL, puerto distinto de 443, `http` sin modo local, modo local permite `http://localhost:8888` pero **no** el puerto del motor ni `192.168.x.x`; `--allow-local-sites` con `sys.frozen` → código 2.
- Normalización de URL (tabla de casos: sin esquema, mayúsculas, IDN, barra final, subcarpeta, consulta y fragmento).
- Firma: los vectores compartidos; nonce y hora nuevos en cada reintento.
- Cliente (con `respx`): descubrimiento por `/wp-json/` y por `?rest_route=`, redirecciones (≤ 3 válidas, 4.ª o a IP privada rechazada), cada fila del mapeo de §5.6, reintentos solo en GET/DELETE, `POST /pair` nunca reintentado, respuesta > 5 MB, `api_version` distinto.
- Casos de uso: `connect` feliz (orden: `create` → `status` → inserción), fallo de `status` → `delete` + revocación de buena fe, fallo de inserción → `delete`, `already_connected`; `reconnect`; `check` con veredicto (devuelve `SiteOut` revocado) y sin veredicto (error, estado sin cambios); `secret_missing` y hash distinto; `list_content` paginado; `remove` con sitio caído (`remote_revoked: false`) y con `wp.revoked` (cuenta como hecho).
- Rutas: 200/201/404/409/503, 401 sin token, 403 con Host incorrecto; `X-Faro-Run-Id` ausente → los secretos se rechazan.
- Logs: ni el código de vinculación, ni token, ni secreto HMAC en stderr capturado.

### 9.3 Plugin (PHPUnit en wp-env)
- Código caducado, reutilizado, con intentos agotados, límite por IP, código de 6 dígitos con ceros a la izquierda, sitio sin HTTPS fuera de `local`.
- Firma válida, alterada (cada campo de la canónica), fuera de ventana (`wp.stale_request`), nonce repetido, sin cabeceras, token incorrecto, conexión reemplazada (`wp.revoked`), salts cambiadas (`wp.connection_broken`), vectores compartidos.
- Todas las rutas sin firma → 401; ninguna registrada con `__return_true` (prueba que recorre `rest_get_server()->get_routes()` del espacio de nombres).
- Revocación desde wp-admin (con nonce; sin nonce o sin `manage_options` → rechazo) y desde `DELETE /connection`.
- Contenido: paginación, `per_page` > 100 rechazado, solo publicados, títulos sin HTML, fechas UTC con `Z`; productos con HPOS activado y desactivado; sin WooCommerce → `woocommerce_active: false`.
- `Cache-Control: no-store` en todas las respuestas `faro/v1`.
- `uninstall.php` borra todas las opciones y transients `faro_*`.

### 9.4 Integración motor ↔ plugin (`pytest -m wp_env`, CI `wp-plugin-integration`)
Con wp-env arriba y `--allow-local-sites`: generar código con `wp eval 'echo Faro_Pairing::create_code();'` → `connectSite` → `checkSiteConnection` → `listSiteContent` de páginas, entradas y productos → desconectar desde WordPress (`wp option delete faro_connection`) → `checkSiteConnection` devuelve `revoked` → `reconnectSite` con código nuevo → `removeSite` con `remote_revoked: true`. Canal de secretos simulado en memoria (no llavero real).

### 9.5 Interfaz
- Sitios conectados: los 4 estados, `engine.not_ready`, `db.key_missing`; tarjeta en cada chip; comprobación automática (una vez por sitio y sesión, no en sitios `revoked`, sin toasts, error → "Sin comprobar").
- Asistente: pasos, exportar plugin (éxito y errores), validación del código en la interfaz, cada error colocado bajo su campo, `already_connected` → **Ir al sitio**, éxito cierra y vacía; volver a conectar con URL fija.
- Ver contenido: pestañas, paginación con pila de cursores, vacío, error, revocado refresca la lista, sin WooCommerce.
- Quitar: textos según `remote_revoked` y estado previo.
- Inicio: combinaciones de pasos hechos; aviso de base en la tarjeta del motor.
- `api.call` invoca `engine_call` con la operación y parámetros correctos; el código de vinculación no aparece en la caché de Query ni en `localStorage`.

### 9.6 Contratos
`npm run contracts` determinista; `engine-operations.json` contiene las 7 operaciones con sus `secrets` y `timeout_seconds` de §5.2.

---

## 10. Verificación manual (Windows 11, con wp-env)

1. `npm run setup` genera el zip; `npm run dev` arranca; en `%APPDATA%\app.faro.desktop\profiles\` hay un `.db` que no empieza por `SQLite format 3` y en el Administrador de credenciales aparece `db/<uuid>/key.app.faro.desktop`.
2. `npx @wordpress/env start` en `packages/wp-plugin` con `FARO_ALLOW_LOCAL_SITES=1`; Configuración → Sitios conectados muestra el estado vacío.
3. **Guardar el plugin en Descargas** abre la carpeta con `faro-wordpress.zip`. Instalarlo en un WordPress limpio de wp-env con **Subir plugin** funciona.
4. En wp-admin → Ajustes → Faro, generar código; conectarse con `http://localhost:8888` y el código → tarjeta "Conectado" con conteos correctos (comparar con wp-admin). En el Administrador de credenciales aparece `wp/<uuid>/token.app.faro.desktop`.
5. *(Reescrito en el cierre T14: con el sitio conectado no se alcanza, porque el motor responde antes `site.already_connected`.)* Hacerlo en **Volver a conectar** (paso 7), antes de generar el código nuevo: el código ya usado del paso 4 → "Este código ya no sirve…"; después generar un código y escribir otro de 6 números → mensaje con intentos restantes, y el código correcto sigue sirviendo.
6. Ver contenido: páginas, entradas y productos coinciden con wp-admin; activar y desactivar HPOS en WooCommerce → sigue funcionando.
7. Desconectar desde wp-admin → al volver a abrir la pestaña (nueva sesión) la tarjeta queda "Desconectado"; **Volver a conectar** con código nuevo → "Conectado".
8. Cambiar una salt en `wp-config.php` del contenedor → **Comprobar conexión** → "Desconectado" con el mensaje de claves de seguridad; wp-admin muestra la conexión rota.
9. Detener wp-env → **Comprobar conexión** → mensaje de conexión y la tarjeta sigue como estaba ("Sin comprobar" en la comprobación automática).
10. **Desconectar sitio** con wp-env detenido → aviso de que no se pudo avisar al sitio; la credencial `wp/…` desaparece del Administrador de credenciales.
11. Sin `FARO_ALLOW_LOCAL_SITES`, conectar `http://localhost:8888` → mensaje de dirección no permitida.
12. `faro.AAAA-MM-DD.log` no contiene el código, el token, el secreto HMAC ni la llave (buscar los valores conocidos de la prueba).
13. Inicio → "Qué hacer ahora" marca "Conecta tu sitio" como hecho.
14. Borrar la credencial `db/…/key` del Administrador de credenciales con el `.db` presente y reabrir Faro → aviso `db.key_missing` en Inicio y en Sitios conectados; **no** se crea una llave nueva (volver a poner la llave desde una copia hecha antes del paso).

---

## 11. Decisiones del usuario (2026-09-29)

1. **Slug del plugin en WordPress.org:** se comprueba la disponibilidad de `faro` antes del lanzamiento; si está ocupado, se usa `faro-marketing`. No bloquea F1a (el zip se instala a mano).
2. **Versión mínima de WordPress:** se declara 6.0 y se prueba PHP 8.1 + WP 6.0 en CI; si falla en wp-env, se sube `Requires at least` a la primera versión que pase en lugar de añadir compatibilidad.
3. **Idioma de los textos del plugin:** español neutro en F1a; antes de publicar en WordPress.org la fuente pasa a inglés con traducciones a `es_*`.
4. **Licencia del plugin:** GPL-2.0-or-later (ADR 0008).

## 12. Pendientes que deja F1a

Actualizado en el cierre T14 (2026-10-01). Cada punto indica su origen. Ninguno bloquea F1a: los hallazgos de seguridad abiertos son de severidad **baja**.

### 12.1 Antes de dar F1a por cerrada del todo (usuario)

1. **Verificación manual** de [docs/qa/2026-10-01-f1a-verificacion-manual.md](../qa/2026-10-01-f1a-verificacion-manual.md). El usuario verificó el flujo real con wp-env y `npm run dev` el 2026-10-01 (pasos 1b, 3a, 4a, 6a, 10b y 15). El resto de §10 sigue pendiente (1a, 1c, 1d, 2a, 2b, 3b, 4b, 4c, 5a, 5b, 6b, 6c, 7a, 7b, 8, 9a, 9b, 10, 11, 12, 13 y 14). — Origen: T12.

### 12.2 Hallazgos de las revisiones de seguridad de F1a (T13, severidad baja)

2. **`set` en `wp/*` puede volver a crear el token de un sitio quitado.** El índice `profile-sites/` nunca borra; si un motor comprometido llama a `reconnectSite` con el id de un sitio ya quitado, la concesión de `set` sigue siendo válida para ese id. Valorar marcar los sitios quitados en el índice o exigir que el secreto exista para `set`. — `apps/desktop/src-tauri/src/secrets/sites.rs`, `secrets/mod.rs`.
3. **La auditoría síncrona en el hilo de stdin del motor puede retrasar `secret_response`.** `watch_stdin` inserta cada evento `audit` en la base en el mismo hilo que reparte las respuestas de secretos. Pasar la inserción a una cola o a otro hilo. — `apps/engine/faro_engine/__main__.py`, `core/audit.py`.
4. **Un fallo al escribir el índice de sitios se informa como `vault.keyring_unavailable`** (con `reason = site_index` solo en la auditoría). Conviene un código propio para no confundirlo con el llavero. — `secrets/mod.rs` (`run_in_keyring`).
5. **B5: sitio atascado si se borra `profile-sites/`.** Sin índice, el núcleo omite la referencia de ese sitio en las concesiones y no se puede comprobar ni quitar con limpieza. Reconciliar el índice al arrancar (por ejemplo, con la lista de sitios del motor y las credenciales `wp/*` del llavero). — revisión T13.
6. **Huérfano residual en el llavero** si una `create` se cuelga en el llavero más de unos 10 s: el motor deja de esperar y manda el `delete`, que llega cuando la `create` ya pasó el punto de cancelación; el `delete` responde `ok` sin borrar (`Done::Skipped`) y la `create` termina escribiendo. Resolver con un borrado compensatorio o con la reconciliación del punto 5. — `secrets/mod.rs` (`execute`, `prepare`).
7. **`pip-audit` y `composer audit` en la máquina del usuario.** La red del usuario tiene Norton interceptando HTTPS. Para PHP se resolvió exportando la CA de Windows a `%USERPROFILE%\.windows-ca.pem`; para `uv`/`pip`, usar `--native-tls` o `SSL_CERT_FILE` (por verificar en la máquina del usuario). En CI no aplica. — T13.

### 12.3 Arrastrados de fases anteriores (F0 y T6)

Hallazgos menores anotados en las revisiones de F0 y de T6; los informes de esas revisiones no están archivados en el repositorio, así que el detalle de cada uno queda **por verificar** cuando se retome.

8. Redacción de `\"` (comilla escapada) en la salida de consola.
9. Nombre `proxy-authorization` en el filtro de logs por nombre.
10. Nombres `apikey` y `credentials` en el filtro de logs por nombre.
11. Error de `GET_LOCK` en el SQLite del plugin (pruebas del plugin).
12. Detección de salts duplicadas en el plugin.
13. Cabecera de WooCommerce del plugin: hoy `WC requires at least: 11.1` (`faro.php`); revisar el mínimo real.
14. Comprobación de `TAURI_CONFIG`.
15. Código para datos huérfanos.
16. Permisos de archivos (`profiles.json`, `profile-sites/`, base) en macOS y Linux.
17. Adoptar un perfil que solo tiene copias de seguridad.
18. Copias de la llave de la base en memoria de Python (`str` inevitables, ver `core/protocol.py`).

### 12.4 Por fase

**F1b**
19. Pruebas extremo a extremo automáticas (WebdriverIO + tauri-driver). — §2.4.

**F2 (agentes y auditoría)**
20. Concesiones que duran toda una tarea de agente, cerradas con un evento `run_finished` o por caducidad (ADR 0010 §3). — §12 original.
21. Tratar como **datos** el contenido remoto (títulos, URLs, nombre del sitio) en los prompts: nunca como instrucciones. — revisión T13.
22. Antes de habilitar escrituras, revisar si un atacante podría conectar **su** sitio a Faro mediante XSS en la interfaz o inyección de prompts. — revisión T13.
23. Sitios solo con URL ("analizar una URL"), inventario de contenido en la base y reutilizar `faro_engine/net` en el rastreador. — §2.2.

**F4 (publicación)**
24. `/drafts` y `/seo-meta` con `Idempotency-Key` y adaptadores `Faro_Seo_*`. — §2.3.
25. Reservar el nonce de forma **atómica** antes de las escrituras (hoy leer y guardar el transient no es atómico); hay un `TODO F4` en `packages/wp-plugin/includes/class-faro-signature.php`. — revisión T13.

**Release**
26. El lanzador PyInstaller nunca pasa `--allow-local-sites` ni `--dev`, con una prueba que lo demuestre. — revisión T13.
27. Verificación de integridad del binario del sidecar (SHA-256 embebido, skill `tauri-sidecar-python`). — F0.
28. Empaquetar el zip del plugin como recurso (`bundle.resources`) para `wp_plugin_export`; hoy en release responde `plugin.package_missing`. — §4.3.
29. Enlace profundo "Abrir Faro en el escritorio". — §2.2.
30. Empaquetado PyInstaller con la librería SQLCipher (criterio 6 de ADR 0009) y carga de `sqlite-vec` (criterio 5), ambos por verificar. — ADR 0009.

**Más de una conexión por sitio**
31. `faro_connection` como lista por `connection_id`, con escrituras condicionales (comparar e intercambiar) sobre cada entrada (ADR nuevo). — ADR 0011 §5.

**Soporte Linux**
32. Revisar los avisos ignorados en `apps/desktop/src-tauri/.cargo/audit.toml` (solo crates exclusivos de Linux). — F0 §14.

**Observaciones del plugin**
33. Con una caché de objetos persistente (Redis, Memcached), los transients de nonces pueden desalojarse antes de los 10 min y aceptar una repetición dentro de la ventana de ±300 s. — revisión T13.
34. El límite por IP de `/pair` detrás de un CDN o proxy inverso es compartido por todos los visitantes (no se confía en `X-Forwarded-For`). — revisión T13.

**Posterior**
35. Restauración de copias y de la llave (`db.key_missing`), visor de auditoría y retención. — §2.3.

---

## 13. Diferencias con lo implementado (cierre T14, 2026-10-01)

Lo construido cumple la spec salvo estas diferencias. Las filas 1–7 y 9 también se reflejan en los ADR 0010, 0012 y 0013 (actualización del 2026-10-01).

### 13.1 Protocolo y secretos

| # | Tema | Spec / ADR original | Implementado | Código |
| --- | --- | --- | --- | --- |
| 1 | Formato del protocolo de secretos | ADR 0010 §2 | Sin cambios de forma: stdout motor → núcleo `{"event":"secret_request","id","run_id","op":"get\|create\|set\|delete","ref"[,"value"]}`; stdin núcleo → motor `{"event":"secret_response","id"}` con **uno** de `"value"`, `"ok":true` o `"error"`. Más el evento `audit` por stdin. Detalles nuevos: una línea malformada con `id` válido recibe `vault.secret_not_allowed`; sin `id` válido, se ignora. El motor trata un código de error desconocido o una respuesta que no encaja con `op` como `vault.secret_not_allowed` (falla cerrado). | Motor: `apps/engine/faro_engine/core/secrets.py`, `protocol.py`, `audit.py`. Núcleo: `apps/desktop/src-tauri/src/secrets/` (`request.rs`, `mod.rs`, `audit.rs`) |
| 2 | Concesiones | Atadas al `run_id`, caducan con la llamada o a `timeout + 5 s` | Además atadas a la **generación del motor** y al **perfil** activos al concederlas. Tras esperar el candado del llavero, el núcleo **revalida** la concesión (sigue viva, mismo motor en marcha) y usa su perfil, no el actual (corrección de TOCTOU). | `secrets/mod.rs` (`Grant`, `prepare`) |
| 3 | `{new}` | Una sola `create`; `delete` "de lo creado" | Una sola `create` por concesión (aunque falle). El `delete` vale para la referencia **intentada**: si la `create` sigue en cola, la **cancela** (ya no escribirá ni el secreto ni el índice); si no se escribió nada, responde `ok` sin tocar el llavero (`Done::Skipped`, motivo `not_created` en la auditoría). El slot tiene `attempted`, `created` y `cancelled`. | `secrets/mod.rs` (`NewSlot`, `check_grant`, `prepare`) |
| 4 | Índice de sitios por perfil | No existía | `<app_data_dir>/profile-sites/<perfil>.json` = `{"version":1,"site_ids":[…]}` (ordenado, escritura atómica). Un sitio entra cuando el núcleo crea su `{new}`, **antes** de escribir en el llavero, y **nunca sale**. `wp/{site_id}/token` solo se concede si el sitio es del perfil activo; si no, la ref se **omite** de la concesión (con aviso en el log). Índice ilegible → se falla cerrado y no se sobrescribe. | `secrets/sites.rs` |
| 5 | Valor de `wp/*/token` | JSON `{"v":1,"token","hmac_secret"}` | JSON compacto **exacto** `{"v":1,"token":"<43>","hmac_secret":"<43>"}` (base64url), en ese orden, sin espacios, ≤ 1 KB, comparado byte a byte. | `secrets/request.rs` (`is_valid_wp_token_value`) |
| 6 | Espera de cada `secret_request` | 10 s fijos | `min(10 s, plazo restante de la operación)` (`max_wait`); sin tiempo, falla con `vault.secret_timeout` **sin escribir nada** en stdout. | `core/secrets.py` |
| 7 | Auditoría | Búfer de 500 eventos | Sin cambios: búfer de 500 mientras el motor no está listo o la base no está disponible; descarta los más viejos. | `secrets/audit.rs` (`AUDIT_BUFFER`) |

### 13.2 Motor

| # | Tema | Spec / ADR original | Implementado | Código |
| --- | --- | --- | --- | --- |
| 8 | Presupuesto de tiempos de `connectSite` | Núcleo 60 s, motor 55 s | El trabajo (descubrir, `pair`, `create`, `status`, insertar) puede llegar hasta los **45 s**; los últimos **10 s** del plazo del motor (`UNDO_RESERVE_SECONDS`) quedan para deshacer: primero el `delete` del secreto y después el `revoke` remoto, sin reintentos y con **5 s** como mucho (`UNDO_REVOKE_SECONDS`). La inserción en la base también va acotada por el plazo del trabajo (`site.timeout`). Sin tiempo para deshacer, no se envía la `create`. `removeSite` reserva lo mismo para el `delete` final tras el `revoke`. | `core/config.py`, `sites/service.py`, `net/client.py` (`Deadline.ending_before`, `capped`, `SafeHttpClient.limited_to`) |
| 9 | `--allow-local-sites` | Núcleo: debug + `.env.local`. Motor `--dev`: el valor de `.env.local` | Núcleo: build debug **y** `FARO_ALLOW_LOCAL_SITES=1`, leída del entorno del proceso o de `.env.local`. Motor: rechazado con `sys.frozen` (código 2). En `--dev` basta el argumento **o** la variable. **Decisión T14:** ADR 0012 se alinea con el código (motivos en su actualización). | `src-tauri/src/engine/mod.rs`, `apps/engine/faro_engine/__main__.py` |
| 10 | Protección de red (más estricta) | ADR 0012 reglas 1–8 | Se rechaza toda dirección que no sea **global** (además de la lista explícita), incluidas IPv4 mapeadas y **NAT64** hacia una prohibida; el `http` del modo local solo va a `localhost`, `127.0.0.1` y `::1` y esos nombres deben resolver solo a loopback; se pide `Accept-Encoding: identity` y se rechaza cualquier respuesta comprimida (`site.bad_response`); el descubrimiento solo envía el código al mismo host o a su variante con o sin `www.` (mismo esquema y puerto), si no `site.moved`; la `url` de cada elemento de contenido debe empezar por `http://` o `https://`; hosts con última etiqueta numérica o hexadecimal se rechazan. | `net/guard.py`, `net/urls.py`, `net/client.py`, `sites/service.py` (`same_site`), `wordpress/client.py` |
| 11 | Plazo de `getHealth` | "—" en §5.2 | `timeout_seconds = 10` (toda operación declara los dos campos). | `packages/shared/engine-operations.json` |

### 13.3 Interfaz

| # | Tema | Spec original | Implementado | Código |
| --- | --- | --- | --- | --- |
| 12 | Enlaces | URL como texto | Confirmado: ningún `<a href>` con URLs del sitio en la interfaz y ninguna capability `opener:*`. `tauri-plugin-opener` se usa solo desde Rust y ni siquiera se registra como plugin. | `src-tauri/capabilities/main.json`, `src-tauri/src/wp_plugin.rs`, `tests/acl.rs` |
| 13 | Código de vinculación | Sin `useMutation` | Confirmado: el código no pasa por `useMutation`, la caché de Query ni el almacenamiento. | `features/sites/ConnectSiteDialog.tsx` |
| 14 | Quitar un sitio ya desconectado | Sin aviso | Toast extra de éxito "Quitamos {{name}} de tu lista." (clave `sites.toast.removed`). | `features/sites/RemoveSiteDialog.tsx` |
| 15 | Chip de conexión | `ConnectionChip` | `SiteStatusChip.tsx` (mismo comportamiento). | `features/sites/` |

### 13.4 Plugin

| # | Tema | Spec original | Implementado | Código |
| --- | --- | --- | --- | --- |
| 16 | Escrituras de opciones | `update_option` | `Faro_Option_Store`: leer el valor crudo, **comparar e intercambiar** y borrar condicional con `BINARY option_value = %s`, sin recrear nunca la opción. `Faro_Connection::touch()` (actualiza `last_seen_at`) nunca recrea la conexión y solo se ejecuta si coincide el `connection_id` que firmó la petición (hallazgo B1 de T13). `Faro_Pairing` usa el mismo almacén para los intentos. | `includes/class-faro-option-store.php`, `class-faro-connection.php`, `class-faro-signature.php`, `class-faro-pairing.php` |
| 17 | Estructura | §4.1 | Clases añadidas: `Faro_Clock`, `Faro_Errors`, `Faro_Connection`, `Faro_Option_Store`. | `packages/wp-plugin/includes/` |
| 18 | Versiones fijadas | WP 6.x, WooCommerce fijada, PHP 8.3; mínima WP 6.0 + PHP 8.1 | `.wp-env.json`: WordPress 7.1.2 + WooCommerce 11.1.2 + PHP 8.3. `.wp-env.min.json`: WordPress 6.0.16 + PHP 8.1, sin WooCommerce (puertos 8890/8891). `Requires at least: 6.0` se mantiene. PHPStan nivel **8** (spec: ≥ 6). | `packages/wp-plugin/` |

### 13.5 Núcleo, CI y QA

| # | Tema | Spec original | Implementado | Código |
| --- | --- | --- | --- | --- |
| 19 | Cobertura de Rust | 80 % global, 95 % en `vault/`, `secrets/`, `profile/`, `engine/protocol.rs` | Confirmado, por líneas, con `scripts/check-rust-coverage.mjs` (un prefijo sin archivos es error). Las pruebas grandes van en archivos aparte (`*_tests.rs` o `tests.rs`, con `#[path]`) para que `cargo llvm-cov` las excluya del informe. `KeyringStore` acepta un constructor de credenciales mock solo bajo `cfg(test)`. En local: `npm run coverage:core`. | `.github/workflows/ci.yml` (`core`), `package.json`, `src/vault/store.rs` |
| 20 | §10, paso 5 (código ya usado) | Con el sitio conectado | No alcanzable: el motor responde antes `site.already_connected`. Reescrito para hacerlo desde **Volver a conectar**. | §10 |
| 21 | Lista manual | `docs/qa/2026-xx-xx-…` | `docs/qa/2026-10-01-f1a-verificacion-manual.md`. Verificado por el usuario el 2026-10-01 el flujo real con wp-env; el resto, pendiente (§12.1). | `docs/qa/` |
| 22 | Filtro de logs por nombre | Solo Python (ADR 0013 §3) | También en Rust, con las mismas listas (prueba de paridad); patrón `Basic …` añadido; ver ADR 0013 (actualización). | `src-tauri/src/logging/redact.rs`, `faro_engine/core/redact.py` |
| 23 | Librería SQLCipher | A elegir en T1 | `sqlcipher3-wheels` (`>=0.5.7,<0.6`); ver ADR 0009 (actualización). | `apps/engine/pyproject.toml` |
| 24 | T14 | El arquitecto pide al agente principal que actualice las skills | Con autorización del usuario, el arquitecto actualizó directamente las skills `llavero-y-cifrado`, `tauri-sidecar-python`, `contratos-api-local`, `wordpress-plugin`, `migraciones-sqlite`, `faro-arquitectura` y `pruebas-faro`. | `.claude/skills/` |
