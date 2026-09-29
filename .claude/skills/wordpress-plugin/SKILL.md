---
name: wordpress-plugin
description: Estructura, seguridad y pruebas del plugin WordPress/WooCommerce de Faro (packages/wp-plugin) - vinculación con código de 6 dígitos y token HMAC, rutas REST firmadas, WooCommerce con HPOS, requisitos del repositorio oficial y verificación con wp-env, PHPUnit, PHPCS y PHPStan. Úsala al crear o cambiar cualquier cosa del plugin o al revisarlo.
---

# Plugin WordPress/WooCommerce

Mínimos: **PHP 8.1+**, **WordPress 6.x**, WooCommerce opcional (con HPOS). El plugin nunca pide la contraseña de administrador ni guarda claves de IA.

## Estructura

```
packages/wp-plugin/
  faro.php            # cabecera del plugin, constantes, carga de includes/
  uninstall.php       # borra opciones y transients faro_* (nada más)
  includes/           # class-faro-pairing.php, class-faro-signature.php, class-faro-rest.php,
                      # class-faro-content.php, woocommerce/, seo/ (adaptadores Yoast / Rank Math)
  admin/              # pantalla de estado: conexión + botón "Abrir Faro en el escritorio"
  languages/          # faro.pot, faro-es_ES.po
  tests/              # PHPUnit (bootstrap del WP test suite)
  composer.json  phpcs.xml.dist  phpstan.neon.dist  .wp-env.json  readme.txt
```

- Prefijos: funciones, opciones, hooks y transients `faro_`; clases `Faro_*` (o namespace `Concersa\Faro`, uno solo en todo el plugin); constantes `FARO_`.
- Cabecera de `faro.php`: `Requires at least: 6.0`, `Requires PHP: 8.1`, `Text Domain: faro`, `License: GPL-2.0-or-later`, `WC requires at least` / `WC tested up to`.
- Todo archivo PHP empieza con `defined( 'ABSPATH' ) || exit;`.

## Vinculación (sin contraseña)

1. En wp-admin → Faro, un usuario con `manage_options` pulsa "Vincular" (formulario con nonce). El plugin genera un **código de 6 dígitos** con `random_int`, guarda solo `hash_hmac('sha256', $code, wp_salt('auth'))`, caducidad (**10 min**) e intentos restantes (**5**) en la opción `faro_pairing` (`autoload = no`). Un código nuevo invalida el anterior.
2. El usuario pega URL del sitio y código en la app. El motor llama `POST /wp-json/faro/v1/pair` con `{code, app_instance_id}` (solo HTTPS, salvo `wp_get_environment_type() === 'local'`).
3. El plugin compara con `hash_equals`; cada fallo resta un intento y a 0 borra el código. Límite adicional por IP con transients. Éxito = el código se borra (**un solo uso**).
4. Responde una sola vez `{connection_id, token, hmac_secret}` (32 bytes aleatorios cada uno, base64url). Guarda en `faro_connection` (`autoload = no`): `connection_id`, `hash('sha256', $token)`, `hmac_secret` cifrado con `sodium_crypto_secretbox` y llave derivada de `wp_salt('auth')` (un volcado solo de la base no basta para firmar), `created_at`.
5. La app guarda token y secreto en el llavero (`wp/<site_id>/token`, ver `llavero-y-cifrado`) y en `site_connections` solo URL y hash del token.

## Peticiones firmadas

Cabeceras propias (algunos hostings borran `Authorization`): `X-Faro-Connection`, `X-Faro-Token`, `X-Faro-Timestamp` (Unix, s), `X-Faro-Nonce` (16 bytes base64url), `X-Faro-Signature`.

```php
$canonical = implode( "\n", [ $method, $route, $timestamp, $nonce, hash( 'sha256', $raw_body ) ] );
$expected  = base64_encode( hash_hmac( 'sha256', $canonical, $hmac_secret, true ) );
```

- `$route` = ruta REST (`/faro/v1/posts`) + query ordenada por clave. Cuerpo vacío = hash de `""`.
- Rechazo (`401`) si: conexión inexistente, `hash_equals` del hash del token falla, firma no coincide, `|now - timestamp| > 300 s`, o nonce ya visto (transient `faro_nonce_<sha256>` 10 min).
- `permission_callback` = `Faro_Signature::verify( $request )` en **todas** las rutas, incluida `/pair` (que valida límite de intentos y código pendiente). **Nunca `__return_true`.**
- Errores como `WP_Error( 'wp.invalid_signature', __( 'La conexión con Faro no es válida.', 'faro' ), [ 'status' => 401 ] )`; sin detalles de la firma. El motor los traduce al formato `{code, message, details}`.
- Revocación: botón "Desconectar" en wp-admin (nonce + `manage_options`) borra `faro_connection`; `DELETE /faro/v1/connection` firmado desde la app hace lo mismo. Tras revocar, todo devuelve `401 wp.revoked`.

## Rutas REST (`/wp-json/faro/v1/`)

| Método | Ruta                                                                     | Fase      |
| ------ | ------------------------------------------------------------------------ | --------- |
| POST   | `/pair`                                                                  | F1        |
| GET    | `/status`, `/pages`, `/posts`, `/products` (paginadas, `per_page` ≤ 100) | F1        |
| DELETE | `/connection`                                                            | F1        |
| POST   | `/drafts`, `/seo-meta` (con `Idempotency-Key`)                           | posterior |

- Escrituras: siempre `post_status = draft`; guardan el valor anterior (post meta `_faro_previous_<campo>` o respuesta `previous`) para "Deshacer"; la misma `Idempotency-Key` devuelve el resultado ya guardado sin repetir la acción.

## Entrada, salida y admin

- Entrada: `args` con `sanitize_callback` y `validate_callback`; `sanitize_text_field`, `absint`, `esc_url_raw`, `wp_kses_post` para HTML.
- Salida en pantallas: `esc_html`, `esc_attr`, `esc_url`. SQL solo con `$wpdb->prepare` (preferir funciones de WP).
- Pantallas y acciones: `current_user_can( 'manage_options' )` + `check_admin_referer`. wp-admin solo muestra estado de conexión y "Abrir Faro en el escritorio"; nada de configuración de agentes.

## WooCommerce

- Solo API CRUD: `wc_get_products( [ 'limit' => 50, 'page' => $p, 'return' => 'objects' ] )`, `WC_Product`, `wc_get_orders`. Nunca `WP_Query`/SQL sobre `wp_posts` para productos o pedidos.
- Declarar HPOS en `before_woocommerce_init`:

```php
add_action( 'before_woocommerce_init', function () {
	if ( class_exists( \Automattic\WooCommerce\Utilities\FeaturesUtil::class ) ) {
		\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility( 'custom_order_tables', FARO_PLUGIN_FILE, true );
	}
} );
```

- Si WooCommerce no está activo, `/products` responde lista vacía con `woocommerce_active: false`.

## SEO (fase posterior)

Interfaz `Faro_Seo_Adapter` con `Faro_Seo_Yoast`, `Faro_Seo_RankMath` y `Faro_Seo_Core` (sin plugin SEO). Detección con `defined( 'WPSEO_VERSION' )` / `defined( 'RANK_MATH_VERSION' )`. Nunca escribir metas de los dos a la vez.

## Repositorio oficial de WordPress.org

- [ ] Licencia **GPL-2.0-or-later** del plugin. **Decisión a confirmar por el usuario en ADR**: el plugin se publica con licencia GPL compatible aunque la app de escritorio sea privativa.
- [ ] Sin código ofuscado ni minificado sin fuente; sin librerías empaquetadas que WP ya trae.
- [ ] Sin llamadas externas no declaradas ni rastreo: el plugin solo responde; no hace peticiones salientes. Todo se documenta en `readme.txt`.
- [ ] `readme.txt` con `Stable tag`, `Tested up to`, `Requires PHP`, `License` y sección de privacidad.
- [ ] Textos con `__()`/`esc_html__()` y dominio `faro`; español primero, `faro.pot` generado con `wp i18n make-pot`.
- [ ] `uninstall.php` borra todo lo `faro_*`; desactivar no borra datos.

## Pruebas y verificación

`.wp-env.json` (requiere Docker):

```json
{
  "core": null,
  "phpVersion": "8.1",
  "plugins": [".", "https://downloads.wordpress.org/plugin/woocommerce.latest-stable.zip"]
}
```

```powershell
cd packages/wp-plugin
npx @wordpress/env start
npx @wordpress/env run tests-cli --env-cwd=wp-content/plugins/wp-plugin vendor/bin/phpunit
composer phpcs      # WordPress Coding Standards (phpcs.xml.dist)
composer phpstan    # nivel 6+ con szepeviktor/phpstan-wordpress
```

Pruebas mínimas: código caducado, reutilizado y con intentos agotados; firma válida, alterada, fuera de ventana y nonce repetido; ruta sin firma → 401; revocación desde ambos lados; HPOS activo e inactivo; `uninstall.php` limpia todo. Todo cambio lo revisa `revisor-seguridad`.
