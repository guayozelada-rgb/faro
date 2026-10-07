# Alertas de Dependabot en herramientas de desarrollo

- **Fecha:** 2026-10-07
- **Autor:** devops-release
- **Rama:** `fix/dependabot-dev-tools` (basada en `main`)
- **Máquina:** Windows 11 Home 10.0.26200, Node 24.16.0, npm 11.13.0. Norton intercepta HTTPS; npm se ejecutó con `NODE_OPTIONS=--use-system-ca`, sin desactivar `strict-ssl`.
- **Para quién:** quien decide sobre las alertas en GitHub y `revisor-seguridad`.

Las siete alertas abiertas (#4–#10) son de alcance `development`: afectan a `wp-env` (`packages/wp-plugin`, solo para levantar WordPress en local y en la CI) y a las herramientas de build de la interfaz (raíz). Nada de esto va en el instalador de Faro ni en el zip del plugin (`npm run build:wp-plugin` no incluye `node_modules/`).

## Resumen

| Alerta | Paquete | Quién lo trae | Acción | Versión resultante | Estado |
| --- | --- | --- | --- | --- | --- |
| #4 alta | `source-map-js` | `vite` → `postcss`, `@tailwindcss/node`, `magicast` (raíz) | `npm update source-map-js` (dentro de `^1.2.1`) | 1.2.2 | Corregida |
| #5 alta | `http-cache-semantics` | `@wordpress/env` → `got@11` → `cacheable-request@7` | `npm update http-cache-semantics` (dentro de `^4.0.0`) | 4.3.0 | Se cierra por rango (ver §2) |
| #6 media | `sprintf-js` | `@wordpress/env` → `js-yaml@3` → `argparse@1` | Ninguna: no hay versión corregida ni padre alternativo | 1.0.3 | **Descartar con motivo** (§3) |
| #7, #8 altas; #9, #10 críticas | `simple-git`, `@simple-git/argv-parser` | `@wordpress/env` → `simple-git@3.36.0` | Ninguna por ahora: forzar la v4 rompe wp-env (§1) | 3.36.0 / 1.1.1 | **Mantener abiertas** hasta el próximo `@wordpress/env` |

## 1. `simple-git` (#7, #8, #9, #10)

**Corrección:** solo en `simple-git` 4.0.2 (que fija `@simple-git/argv-parser` 2.0.1). No hay 3.x corregida.

**Padre:** ninguna versión publicada de `@wordpress/env` usa la v4. La última, 11.17.0 (publicada el 2026-10-07 a las 12:41 UTC), sigue pidiendo `simple-git ^3.32.3`. Gutenberg fusionó la actualización en trunk ese mismo día a las 13:24 UTC ([WordPress/gutenberg#84137](https://github.com/WordPress/gutenberg/pull/84137), commit `2efd910d919d92fe2e592ade61ae7806252bd9a9`), así que llegará en la próxima versión de `@wordpress/env`. Al ritmo de las últimas versiones (11.16.0 el 23-09, 11.17.0 el 07-10), sería hacia el 21-10.

**Por qué no usamos `overrides`:** `simple-git` 4 eliminó la exportación por defecto. Comprobado con 4.0.2: `require('simple-git')` devuelve un objeto cuyas claves son `simpleGit`, `CheckRepoActions`, etc., no una función. wp-env 11.16.0 hace `const SimpleGit = require( 'simple-git' ); SimpleGit( { progress } )` en `lib/download-sources.js` y en `lib/runtime/docker/download-wp-phpunit.js`. Con el override, `wp-env start` fallaría con `TypeError: SimpleGit is not a function`:
- al descargar el núcleo: `.wp-env.json` y `.wp-env.min.json` usan `"core": "WordPress/WordPress#<versión>"`, que es una fuente git;
- al descargar la suite de PHPUnit, cosa que hace en todos los arranques que configuran el entorno.

Un parche local de wp-env no sirve: la CI instala con `npm ci --ignore-scripts`, así que `patch-package` no se ejecutaría. Un paquete intermedio en el repositorio, por su parte, añadiría código propio a la cadena de suministro solo para adelantarse dos semanas.

**Alcance en nuestro uso:** los cuatro avisos son rodeos de la protección `unsafe` de `simple-git`. Para explotarlos, un atacante tiene que controlar los argumentos o la configuración que se le pasan a `simple-git`. wp-env solo pasa opciones fijas (`--depth`, `--filter`, `--no-single-branch`, `--tags`, `--no-checkout`, `--cone`) y las URL y refs de nuestros `.wp-env*.json`, que están versionados en el repositorio. En la CI, quien pudiera cambiar esos archivos en un PR ya podría cambiar cualquier script que la CI ejecuta, así que el aviso no le da ningún privilegio extra. El trabajo `wp-plugin-integration` corre sin secretos.

**Recomendación:** no descartarlas, porque sí habrá corrección. Cuando salga la versión de `@wordpress/env` con #84137:
1. Subir `@wordpress/env` en `packages/wp-plugin/package.json` (versión exacta) y regenerar el lockfile.
2. Actualizar la versión fijada en `.claude/skills/wordpress-plugin/SKILL.md` y en `packages/wp-plugin/README.md`.
3. Que la CI (`wp-plugin-integration`) pruebe `wp-env start` con las dos configuraciones.

Cambio de comportamiento a tener en cuenta: `simple-git` 4 ya no pasa a `git` las variables `GIT_*` del entorno, ni tampoco `EDITOR`, `VISUAL`, `PAGER`, `PREFIX` o `SSH_ASKPASS`. En la máquina con Norton, si alguien usaba `GIT_SSL_CAINFO` para que wp-env clonara, tendrá que pasar a `git config --global http.sslCAInfo <ruta>`.

## 2. `http-cache-semantics` (#5)

**Aviso:** [GHSA-ch52-4w7c-c8xp](https://github.com/advisories/GHSA-ch52-4w7c-c8xp) (CVE-2026-93748). Una caché compartida que respeta `max-stale` del cliente puede servir a un usuario la respuesta con `Set-Cookie` de otro. Rango afectado: `<= 4.2.0`, sin versión corregida declarada.

**Acción:** actualizamos dentro del rango `^4.0.0` de `cacheable-request` a **4.3.0** (publicada por el mantenedor el 2026-10-04). Dependabot y `npm audit` la dan por no afectada, y la alerta se cierra.

**Para que conste:** la 4.3.0 no cambia el manejo de `max-stale`. El diff contra 4.2.0 (revisado) solo corrige `Vary` (`*` y propiedades heredadas) y añade `status()`; no tiene dependencias ni scripts de instalación. El mantenedor considera infundado el aviso, porque RFC 9111 §7.3 permite compartir respuestas con `Set-Cookie` y para evitarlo está `Cache-Control: private` ([kornelski/http-cache-semantics#56](https://github.com/kornelski/http-cache-semantics/issues/56), [github/advisory-database#10139](https://github.com/github/advisory-database/issues/10139)). La alerta se cierra porque el rango del aviso ya no incluye la 4.3.0, no porque el código haya cambiado.

**Alcance en nuestro uso (aunque siguiera abierta):** inalcanzable. `got` 11 solo usa `cacheable-request` (y, por tanto, `http-cache-semantics`) si se le pasa la opción `cache`, que por defecto es `undefined`. wp-env la llama sin ella en dos sitios: `got.stream( source.url )`, que descarga zips como el de WooCommerce, y `got( 'https://api.wordpress.org/core/stable-check/1.0/' ).json()`. Además, wp-env es un cliente de línea de comandos, no una caché compartida entre usuarios.

## 3. `sprintf-js` (#6)

**Aviso:** [GHSA-hp3w-g68c-fv3c](https://github.com/advisories/GHSA-hp3w-g68c-fv3c). Una cadena de formato con una precisión enorme lanza un `RangeError` no capturado, lo que provoca una denegación de servicio. Hace falta que el atacante controle la cadena de formato. Rango `<= 1.1.3`, sin versión corregida.

**Cadena:** `@wordpress/env` → `js-yaml@3.15.2` (pide `^3.15.2`) → `argparse@1.0.10` → `sprintf-js@1.0.3`.

**Alcance en nuestro uso:** inalcanzable.
- `js-yaml` 3 solo carga `argparse` desde su ejecutable de línea de comandos (`bin/js-yaml.js`); la librería no lo usa. Ni nosotros ni wp-env ejecutamos ese comando.
- wp-env solo usa `yaml.dump( dockerComposeConfig )` (`lib/runtime/docker/docker-config.js`), que no pasa por `argparse` ni por `sprintf-js`.
- Además, `argparse` usa `sprintf-js` (`lib/help/formatter.js`, `lib/argument_parser.js`) para los textos de uso y de ayuda que define el propio programa, no para cadenas de terceros.

**Alternativas evaluadas:**
- Forzar `js-yaml` 4 (que usa `argparse` 2, sin `sprintf-js`): rompería la API que pide wp-env (`^3`). Gutenberg trunk también sigue en `^3.15.2`.
- Forzar `argparse` 2 o quitar `sprintf-js` con `overrides`: solo afectaría al ejecutable `js-yaml`, que no usamos, pero dejaría un árbol con dependencias que no cumplen sus rangos solo para silenciar una alerta inalcanzable.
- No hay otra versión de `@wordpress/env` sin esta cadena.

**Recomendación:** descartarla en GitHub con el motivo **"Vulnerable code is not actually used"** y este texto:

> sprintf-js solo llega por js-yaml@3 → argparse@1, que js-yaml carga únicamente en su ejecutable de línea de comandos. wp-env (dev, no distribuido) solo usa yaml.dump. No hay versión corregida. Ver docs/qa/2026-10-07-dependabot-herramientas-dev.md §3.

## 4. `source-map-js` (#4)

`npm update source-map-js` en la raíz: de 1.2.1 a **1.2.2** para todos los consumidores (`postcss` 8.5.28, `@tailwindcss/node` 4.3.3, `magicast` 0.5.5), todos con `^1.2.1`. Sin `overrides`. Solo cambian versión, URL e integridad de una entrada de `package-lock.json`.

## 5. Hallazgo fuera de las alertas: `qs`

`npm audit` en `packages/wp-plugin` señala también `qs` 6.15.3 (moderada; [GHSA-x5fp-wj9c-mxmx](https://github.com/advisories/GHSA-x5fp-wj9c-mxmx) y [GHSA-4mjr-xmp4-gh2g](https://github.com/advisories/GHSA-4mjr-xmp4-gh2g), corregida en 6.16.0), que Dependabot todavía no reporta. Cadena: `@wordpress/env` → `@wp-playground/cli` (fija `express` **4.22.2** exacto, también en su última versión, 3.1.57) → `express` (`qs ~6.15.1`). `express` 4.22.3 ya pide `~6.16.0`, pero solo se podría usar con un override de `express`. Solo se alcanza si se usa el entorno de Playground de wp-env, que tiene un servidor HTTP local; nosotros usamos Docker. No se toca en este cambio. Lo más probable es que se resuelva al actualizar `@wordpress/env` o `@wp-playground/cli`.

## 6. Verificación (sin Docker)

| Comprobación | Resultado |
| --- | --- |
| `npm ci` (raíz) | Pasa; `found 0 vulnerabilities` |
| `npm ci --prefix packages/wp-plugin --ignore-scripts` | Pasa |
| `npm run lint` | Pasa (ESLint, Clippy `-D warnings`, Ruff) |
| `npm run typecheck` | Pasa (tsc en `@faro/desktop` y `@faro/shared`, mypy en 94 archivos) |
| `npm run test` | Pasa: Vitest 443/443, Rust 243 + 14 (2 ignoradas a propósito porque usan el llavero real), pytest 939, contratos y scripts 44 + 13 + 13 |
| `npm run build:wp-plugin` | Pasa; `dist/faro-wordpress.zip`, 20 archivos |
| `npx --no-install wp-env --version` (en `packages/wp-plugin`) | `11.16.0` |
| `got`, `cacheable-request` y `http-cache-semantics` 4.3.0 cargan | Sí (prueba con `node -e`) |

`wp-env start` de verdad lo prueba la CI en el trabajo `wp-plugin-integration`.

### `npm audit`

| Carpeta | Antes | Después |
| --- | --- | --- |
| Raíz | 1 alta (`source-map-js`) | **0** |
| `packages/wp-plugin` | 10 (3 críticas, 1 alta, 6 moderadas): `simple-git`/`argv-parser`/`@wordpress/env`, `http-cache-semantics`, `qs`/`express`/`@wp-playground/cli`, `sprintf-js`/`argparse`/`js-yaml` | 9 (3 críticas, 6 moderadas): sale `http-cache-semantics`; quedan `simple-git` (§1), `qs` (§5) y `sprintf-js` (§3) |

`npm audit fix --force` propone bajar a `@wordpress/env@11.8.0`, que es un cambio incompatible y no corrige `simple-git`. No se aplica.
