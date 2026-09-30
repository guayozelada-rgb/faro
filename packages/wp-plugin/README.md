# Plugin de WordPress de Faro

Plugin de WordPress/WooCommerce que conecta un sitio con la app Faro (spec [F1a](../../docs/specs/2026-09-29-f1a-conexion-wordpress.md), ADR 0008 y 0011). Licencia **GPL-2.0-or-later** (ver `LICENSE`); no contiene código de la app.

- PHP 8.1+, WordPress 6.0+, WooCommerce opcional (compatible con HPOS).
- Sin dependencias de Composer en tiempo de ejecución: Composer solo instala herramientas de desarrollo (`vendor/` no va en el zip ni en git).
- El plugin nunca hace peticiones salientes.

## API (`/wp-json/faro/v1/`)

| Método | Ruta | Autorización |
| --- | --- | --- |
| POST | `/pair` | código de 6 dígitos pendiente + límite por IP |
| GET | `/status`, `/pages`, `/posts`, `/products` | firma HMAC v1 |
| DELETE | `/connection` | firma HMAC v1 |

Firma v1: ver ADR 0011 §2 y los vectores compartidos en `packages/shared/fixtures/wp-signature-v1.json` (la wp-env los monta en `/var/www/html/faro-fixtures`).

## Desarrollo

Sin PHP local: todo corre en contenedores (Docker).

wp-env y sus dependencias están fijados en `package.json` y `package-lock.json` de esta carpeta (no es un workspace del monorepo). Instálalos una vez con `npm ci --ignore-scripts`; después `npx @wordpress/env@11.16.0` usa esa copia fijada.

El zip para instalar en WordPress se genera desde la raíz del repositorio con `npm run build:wp-plugin` (`dist/faro-wordpress.zip`, carpeta `faro/`, sin `tests/`, `vendor/` ni configuración de desarrollo).

```sh
# Herramientas de desarrollo
docker run --rm -v "$PWD:/app" -w /app composer:2 install

# Estilo y análisis estático
docker run --rm -v "$PWD:/app" -w /app --entrypoint php composer:2 vendor/bin/phpcs
docker run --rm -v "$PWD:/app" -w /app --entrypoint php composer:2 vendor/bin/phpstan analyse --memory-limit=1G

# Configuración actual: WordPress 7.1.2 + WooCommerce 11.1.2, PHP 8.3
npx @wordpress/env@11.16.0 start
npx @wordpress/env@11.16.0 run tests-cli --env-cwd=wp-content/plugins/faro vendor/bin/phpunit                      # HPOS activado
npx @wordpress/env@11.16.0 run tests-cli --env-cwd=wp-content/plugins/faro env FARO_TEST_HPOS=0 vendor/bin/phpunit # HPOS desactivado
npx @wordpress/env@11.16.0 stop

# Combinación mínima: WordPress 6.0 + PHP 8.1, sin WooCommerce
npx @wordpress/env@11.16.0 start --config .wp-env.min.json
npx @wordpress/env@11.16.0 run tests-cli --config .wp-env.min.json --env-cwd=wp-content/plugins/faro vendor/bin/phpunit
npx @wordpress/env@11.16.0 stop --config .wp-env.min.json
```

Levanta un solo entorno a la vez. En la instancia de desarrollo (`http://localhost:8888`) activa el plugin con `npx @wordpress/env@11.16.0 run cli wp plugin activate faro`.

Generar un código sin pasar por wp-admin (prueba de integración): `npx @wordpress/env@11.16.0 run cli wp eval 'echo Faro_Pairing::create_code();'`.

Plantilla de traducción: `npx @wordpress/env@11.16.0 run cli --env-cwd=wp-content/plugins/faro wp i18n make-pot . languages/faro.pot --exclude=vendor,tests`.
