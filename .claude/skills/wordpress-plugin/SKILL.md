---
name: wordpress-plugin
description: Estructura, seguridad y pruebas del plugin WordPress/WooCommerce de Faro (packages/wp-plugin) - vinculación con código de 6 dígitos y token HMAC, rutas REST firmadas, escrituras atómicas de opciones (Faro_Option_Store), WooCommerce con HPOS, requisitos del repositorio oficial y verificación con wp-env, PHPUnit, PHPCS y PHPStan (también en local en Windows). Úsala al crear o cambiar cualquier cosa del plugin o al revisarlo.
---

# Plugin WordPress/WooCommerce

Mínimos: **PHP 8.1+**, **WordPress 6.0+**, WooCommerce opcional (con HPOS). El plugin nunca pide la contraseña de administrador, nunca guarda claves de IA y **nunca hace peticiones salientes**. Licencia GPL-2.0-or-later y sin código de la app (ADR 0008). Decisiones de fondo: ADR 0011.

## Estructura (F1a)

```
packages/wp-plugin/
  faro.php  uninstall.php  readme.txt  LICENSE  README.md
  includes/ class-faro-plugin.php  class-faro-clock.php  class-faro-errors.php
            class-faro-crypto.php  class-faro-option-store.php  class-faro-connection.php
            class-faro-pairing.php  class-faro-signature.php  class-faro-rest.php
            class-faro-content.php  class-faro-status.php
            woocommerce/class-faro-woocommerce.php  seo/class-faro-seo-detector.php
  admin/    class-faro-admin.php  views/settings-page.php
  languages/ faro.pot
  tests/    bootstrap.php  class-faro-test-case.php  phpstan-bootstrap.php  test-*.php
  composer.json  composer.lock  phpcs.xml.dist  phpstan.neon.dist  phpunit.xml.dist
  .wp-env.json  .wp-env.min.json  package.json  package-lock.json (wp-env fijado)
```

- Prefijos: funciones, opciones, hooks y transients `faro_`; clases `Faro_*`; constantes `FARO_` (`FARO_VERSION`, `FARO_API_VERSION = 1`, `FARO_PLUGIN_FILE`, `FARO_PLUGIN_DIR`).
- Cabecera de `faro.php`: `Requires at least: 6.0`, `Requires PHP: 8.1`, `Text Domain: faro`, `License: GPL-2.0-or-later`, `License URI`, `WC requires at least` / `WC tested up to`.
- Todo archivo PHP empieza con `defined( 'ABSPATH' ) || exit;`.
- Sin dependencias de Composer en tiempo de ejecución: Composer solo para herramientas de desarrollo. El zip (`npm run build:wp-plugin` desde la raíz → `dist/faro-wordpress.zip`, carpeta `faro/`) no lleva `vendor/`, `tests/`, `dist/`, `composer.*` ni configuración.
- Hora: usa `Faro_Clock::now()` (inyectable en pruebas), no `time()`. Errores: `Faro_Errors::get( 'wp.…' )`.

## Vinculación (sin contraseña)

1. wp-admin → **Ajustes → Faro** (`manage_options` + `check_admin_referer`) → **Generar código de conexión**. Solo si `wp_is_using_https()` o `wp_get_environment_type() === 'local'`. `Faro_Pairing::create_code()` genera 6 dígitos con `random_int` (ceros a la izquierda), guarda en `faro_pairing` (`autoload = no`) solo `hash_hmac( 'sha256', $code, wp_salt( 'auth' ) )`, caducidad (**10 min**) e intentos (**5**). Un código nuevo invalida el anterior. El código se muestra solo en la respuesta a esa acción.
2. El motor llama `POST /faro/v1/pair` con `{code, app_instance_id, app_version}`. `permission_callback` = `Faro_Pairing::permission`: petición segura (`wp_is_using_https() && is_ssl()`, o entorno `local`; nunca `X-Forwarded-Proto`), código pendiente y límite por IP (10 fallos / 15 min por `REMOTE_ADDR`; nunca `X-Forwarded-For`). Se evalúa una sola vez por petición (WordPress vuelve a llamar a los permisos para la cabecera `Allow`).
3. Comparación con `hash_equals`; cada fallo resta un intento (403 `wp.pairing_invalid` con `attempts_left`; a 0 borra el código). Caducado o usado → 410 `wp.pairing_expired`. Los intentos se descuentan con `Faro_Option_Store` (comparar e intercambiar), no con `update_option`.
4. Éxito: se borra el código (**un solo uso**) y `Faro_Connection::create()` **reemplaza** la conexión anterior y responde una sola vez `{api_version: 1, connection_id, token, hmac_secret, site: {…}}` (`token` y `hmac_secret`: 32 bytes aleatorios en base64url sin relleno = 43 caracteres).
5. `faro_connection` (`autoload = no`) guarda `connection_id`, `token_sha256`, `hmac_secret` cifrado (`sodium_crypto_secretbox` con clave `hash_hmac( 'sha256', 'faro-hmac-secret-v1', wp_salt( 'auth' ), true )`, formato `v1:` + base64(nonce ‖ cifrado)), `app_instance_id`, `app_version`, `created_at`, `last_seen_at`.
6. La app guarda token y secreto en el llavero (`wp/<site_id>/token`, JSON exacto de `llavero-y-cifrado`) y en `site_connections` solo URL, `connection_id` remoto y `sha256(token)`.

## Peticiones firmadas (firma v1, ADR 0011 §2)

Cabeceras propias (algunos hostings borran `Authorization`): `X-Faro-Connection`, `X-Faro-Token`, `X-Faro-Timestamp` (Unix, s), `X-Faro-Nonce` (16 bytes base64url = 22 caracteres), `X-Faro-Signature`.

```php
$canonical = implode( "\n", array( $method, $route, $timestamp, $nonce, hash( 'sha256', $raw_body ) ) );
// La clave HMAC son los 32 bytes descifrados, NO el texto base64url.
$expected  = base64_encode( hash_hmac( 'sha256', $canonical, $hmac_secret_bytes, true ) );
```

- `$route` = ruta REST (`/faro/v1/posts`) sin barra final + `?` y los parámetros distintos de `rest_route`, `rawurlencode` (RFC 3986), ordenados por clave y valor. Igual con `/wp-json/` o `?rest_route=`. Cuerpo vacío = hash de `""`. Vectores compartidos: `packages/shared/fixtures/wp-signature-v1.json` (PHPUnit y pytest deben coincidir).
- **Orden de verificación** (`Faro_Signature::verify`): cabeceras con su forma → conexión existe y `connection_id` coincide (`wp.revoked`) → descifrar el secreto (`wp.connection_broken`, salts cambiadas) → `hash_equals` del hash del token (`wp.invalid_signature`) → ventana ±300 s (`wp.stale_request`) → HMAC con `hash_equals` (`wp.invalid_signature`) → nonce no usado (`wp.invalid_signature`) → **solo entonces** se guarda el nonce (transient `faro_nonce_<sha256>` 10 min).
- `permission_callback` en **todas** las rutas (`Faro_Signature::permission`; en `/pair`, `Faro_Pairing::permission`). **Nunca `__return_true`** (hay una prueba que recorre las rutas del espacio de nombres).
- Errores: `WP_Error( 'wp.invalid_signature', __( '…', 'faro' ), array( 'status' => 401 ) )`, sin detalles de la firma. El motor los traduce (`site.revoked`, `site.connection_broken`, `site.auth_failed`, `site.clock_skew`).
- `/pair` y `/status` devuelven `api_version: 1`; un cambio incompatible sube ese número.
- Todas las respuestas `faro/v1` llevan `Cache-Control: no-store, private` (`Faro_Rest`).
- Revocación: botón **Desconectar de Faro** en wp-admin (nonce + `manage_options`) o `DELETE /faro/v1/connection` firmado → `Faro_Connection::revoke()`. Después todo devuelve `401 wp.revoked`.
- **Pendiente F4**: guardar el nonce tras leerlo no es atómico. Antes de `/drafts` y `/seo-meta`, reservarlo de forma atómica (`TODO F4` en `class-faro-signature.php`).

## Escrituras de opciones: `Faro_Option_Store` (lección B1)

`update_option()` hace `add_option()` si la fila ya no existe: una petición en curso puede **revivir** una conexión que el usuario acaba de revocar, o pisar una vinculación nueva. Por eso:

- **Nunca** llames a `update_option( 'faro_connection', … )`. Solo se escribe `faro_connection` al **vincular** (`Faro_Connection::create`: `delete_option` + `add_option`) y al **desconectar** (`Faro_Connection::revoke`).
- Cualquier otra escritura condicional (hoy `last_seen_at` y los intentos de `faro_pairing`) usa `Faro_Option_Store`:

```php
$raw = Faro_Option_Store::read_raw( Faro_Connection::OPTION ); // de la base, sin caché; null si no existe
if ( null === $raw ) {
	return; // revocada: no se recrea
}
$stored = /* validar maybe_unserialize( $raw ) y comprobar que es la conexión esperada */;
$stored['last_seen_at'] = Faro_Clock::now();
Faro_Option_Store::compare_and_swap( Faro_Connection::OPTION, $raw, maybe_serialize( $stored ) );
// UPDATE … WHERE option_name = %s AND BINARY option_value = %s; nunca crea la fila
```

- `delete_if_unchanged()` para borrar solo si nadie cambió el valor. Las tres funciones invalidan la caché de objetos (`flush_cache`).
- La comparación es **exacta** (`BINARY`): MySQL compararía sin distinguir mayúsculas ni espacios finales con la colación por defecto.
- `Faro_Connection::touch()` solo se llama si el `connection_id` guardado es el que firmó la petición, y por dentro vuelve a comprobar `connection_id` y `token_sha256` antes de escribir (como mucho cada 5 min).

## Rutas REST (`/wp-json/faro/v1/`)

| Método | Ruta | Autorización | Fase |
| --- | --- | --- | --- |
| POST | `/pair` | código pendiente + límite por IP | F1a |
| GET | `/status`, `/pages`, `/posts`, `/products` (`page` ≥ 1, `per_page` 1–100, 50 por defecto) | firma | F1a |
| DELETE | `/connection` | firma | F1a |
| POST | `/drafts`, `/seo-meta` (con `Idempotency-Key` y nonce atómico) | firma | F4 |

- Contenido: solo `publish`. Páginas y entradas con `WP_Query` (`orderby = ID`, `order = ASC`); títulos con `html_entity_decode( wp_strip_all_tags( get_the_title( $post ) ), ENT_QUOTES, 'UTF-8' )`; fechas UTC con `Z`.
- Escrituras (F4): siempre `post_status = draft`; guardan el valor anterior para "Deshacer"; la misma `Idempotency-Key` devuelve el resultado ya guardado.

## Entrada, salida y admin

- Entrada: `args` con `sanitize_callback` y `validate_callback`; `sanitize_text_field`, `absint`, `esc_url_raw`, `wp_kses_post` para HTML.
- Salida en pantallas: `esc_html`, `esc_attr`, `esc_url`. SQL solo con `$wpdb->prepare` (preferir funciones de WP; las consultas directas de `Faro_Option_Store` llevan `phpcs:ignore` justificado).
- Pantallas y acciones: `current_user_can( 'manage_options' )` + `check_admin_referer`. wp-admin solo muestra el estado de la conexión, generar código y desconectar; en F1a, en lugar de un botón "Abrir Faro en el escritorio" (fase de release), el texto "Abre Faro en tu computadora y ve a Configuración → Sitios conectados." Nada de configuración de agentes.

## WooCommerce

- Solo API CRUD: `wc_get_products( array( 'status' => 'publish', 'limit' => $per_page, 'page' => $page, 'paginate' => true, 'orderby' => 'ID', 'order' => 'ASC' ) )`, `WC_Product`. Nunca `WP_Query`/SQL sobre `wp_posts` para productos o pedidos. En F1a no se leen pedidos ni clientes.
- Declarar HPOS en `before_woocommerce_init`:

```php
add_action( 'before_woocommerce_init', function () {
	if ( class_exists( \Automattic\WooCommerce\Utilities\FeaturesUtil::class ) ) {
		\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility( 'custom_order_tables', FARO_PLUGIN_FILE, true );
	}
} );
```

- Si WooCommerce no está activo, `/products` responde lista vacía con `woocommerce_active: false`.

## SEO (F4)

Detección en `/status` con `Faro_Seo_Detector` (`yoast` si `defined( 'WPSEO_VERSION' )`, `rank_math` si `defined( 'RANK_MATH_VERSION' )`, si no `none`). Los adaptadores de escritura `Faro_Seo_Yoast`, `Faro_Seo_RankMath` y `Faro_Seo_Core` llegan en F4. Nunca escribir metas de los dos a la vez.

## Repositorio oficial de WordPress.org

- [x] Licencia **GPL-2.0-or-later** (ADR 0008).
- [ ] Sin código ofuscado ni minificado sin fuente; sin librerías empaquetadas que WP ya trae.
- [ ] Sin llamadas externas ni rastreo: el plugin solo responde. Todo se documenta en `readme.txt`.
- [ ] `readme.txt` con `Stable tag`, `Tested up to`, `Requires PHP`, `License` y sección de privacidad.
- [ ] Textos con `__()`/`esc_html__()` y dominio `faro`; en F1a, español neutro; antes de publicar, fuente en inglés con traducciones `es_*`. `faro.pot` con `wp i18n make-pot`.
- [ ] `uninstall.php` borra todo lo `faro_*` (opciones y transients, incluidos nonces e IP); desactivar no borra datos.

## Pruebas y verificación

Entornos fijados (nunca `latest-stable`):

| Archivo | Combinación | Puertos |
| --- | --- | --- |
| `.wp-env.json` | WordPress 7.1.2 + WooCommerce 11.1.2 + PHP 8.3 | 8888 / 8889 |
| `.wp-env.min.json` | WordPress 6.0.16 + PHP 8.1, sin WooCommerce | 8890 / 8891 |

Ambos montan el plugin en `wp-content/plugins/faro` y los vectores en `faro-fixtures`, con `WP_ENVIRONMENT_TYPE = local`. wp-env está fijado en el `package.json` de la carpeta (`npm ci --ignore-scripts`; luego `npx @wordpress/env@<versión fijada>`). Levanta un solo entorno a la vez.

```powershell
cd packages/wp-plugin
npx @wordpress/env@11.16.0 start
npx @wordpress/env@11.16.0 run tests-cli --env-cwd=wp-content/plugins/faro vendor/bin/phpunit                      # HPOS activado
npx @wordpress/env@11.16.0 run tests-cli --env-cwd=wp-content/plugins/faro env FARO_TEST_HPOS=0 vendor/bin/phpunit # HPOS desactivado
npx @wordpress/env@11.16.0 run cli wp eval 'echo Faro_Pairing::create_code();'                                    # código para pruebas
```

PHPCS (WordPress Coding Standards + PHPCompatibilityWP 8.1+) y PHPStan (**nivel 8**, `szepeviktor/phpstan-wordpress`, stubs de WooCommerce):

- **Con Docker** (sin PHP local): `docker run --rm -v "$PWD:/app" -w /app composer:2 install`, y luego `--entrypoint php composer:2 vendor/bin/phpcs` / `vendor/bin/phpstan analyse --memory-limit=1G` (ver `packages/wp-plugin/README.md`).
- **En local en Windows** (PHP y Composer instalados): `composer install`, `composer phpcs`, `composer phpstan`.
  - Si la red intercepta HTTPS (en la máquina del usuario, Norton), Composer falla al verificar certificados: exporta la CA raíz de Windows a `%USERPROFILE%\.windows-ca.pem` y añade al `php.ini` de PHP (`php --ini` muestra cuál) las líneas `curl.cainfo` y `openssl.cafile` apuntando a ese archivo; así lo usan Composer y `composer audit`. Nunca desactives la verificación TLS. Si el antivirus renueva su certificado, vuelve a exportar la CA.
  - `vendor/` no se comparte entre Docker (Linux) y Windows: si se instaló desde el contenedor, bórralo y vuelve a crearlo con `composer install` desde Windows antes de usar las herramientas en local (y al revés antes de volver a Docker o wp-env).
- Antes de abrir un PR: PHPCS sin errores también en `tests/` (docblocks incluidos) y PHPStan limpio; la CI (`wp-plugin`, `wp-plugin-integration`) lo exige.

Pruebas mínimas: código caducado, reutilizado, con intentos agotados, límite por IP y ceros a la izquierda; firma válida, alterada (cada campo), fuera de ventana, nonce repetido, sin cabeceras, conexión reemplazada y salts cambiadas; vectores compartidos; ninguna ruta con `__return_true` y todas responden 401 sin firma; `Cache-Control: no-store`; `touch()` no revive una conexión revocada ni pisa una nueva; revocación desde ambos lados; HPOS activo e inactivo; `uninstall.php` limpia todo. Todo cambio lo revisa `revisor-seguridad`.

Observaciones abiertas (spec F1a §12): con caché de objetos persistente los nonces pueden desalojarse antes de tiempo; detrás de un CDN el límite por IP de `/pair` es compartido.
