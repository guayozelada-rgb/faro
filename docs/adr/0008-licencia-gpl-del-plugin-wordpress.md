# ADR 0008 — El plugin de WordPress se publica con licencia GPL-2.0-or-later

- **Fecha:** 2026-09-29
- **Estado:** aceptado
- **Decidido por:** el usuario (2026-09-29)
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Relacionado:** [ADR 0005](0005-repositorio-publico-y-ci.md) (repositorio público, `LICENSE` "Todos los derechos reservados")

## Contexto

Faro es una app de escritorio privativa de Concersa. El repositorio es público pero sin licencia open source: el `LICENSE` de la raíz dice "Copyright © 2026 Concersa. Todos los derechos reservados." (ADR 0005).

En F1a nace `packages/wp-plugin`, el plugin que el usuario instala en su sitio WordPress/WooCommerce para que Faro pueda leer su contenido. El plan es distribuirlo en el **repositorio oficial de WordPress.org**, que exige una licencia compatible con GPL (en la práctica, GPL-2.0-or-later). Además, la postura de la comunidad y de la WordPress Foundation es que el código PHP que se ejecuta dentro de WordPress y usa sus funciones es obra derivada de WordPress (GPL), así que distribuirlo con una licencia privativa sería discutible aunque no fuera a WordPress.org.

La app de escritorio y el plugin son programas distintos: se ejecutan en máquinas distintas (la computadora del usuario y el servidor del sitio) y solo se comunican por HTTP con peticiones firmadas (ADR 0011).

## Decisión

1. **El plugin (`packages/wp-plugin`) se licencia como GPL-2.0-or-later.**
   - `packages/wp-plugin/LICENSE` contiene el texto completo de la GPL v2 y el aviso "Copyright © 2026 Concersa" con la opción "or (at your option) any later version".
   - La cabecera de `faro.php` lleva `License: GPL-2.0-or-later` y `License URI: https://www.gnu.org/licenses/gpl-2.0.html`; `readme.txt` lleva los mismos campos.
   - El zip del plugin que genera el build incluye `LICENSE`.
2. **El resto del repositorio sigue siendo privativo.** El `LICENSE` de la raíz ("Todos los derechos reservados") aplica a todo salvo a `packages/wp-plugin/`. El `README.md` de la raíz y el propio `LICENSE` de la raíz lo indican con una línea explícita: "Excepción: el contenido de `packages/wp-plugin/` se distribuye bajo GPL-2.0-or-later; ver `packages/wp-plugin/LICENSE`."
3. **Separación estricta app ↔ plugin:**
   - El plugin **no incluye código de la app** (ni de `apps/*` ni de `packages/shared` salvo lo indicado abajo), ni la app incluye código del plugin: la app solo empaqueta el **zip** del plugin como archivo para que el usuario lo instale, igual que lo haría con cualquier descarga.
   - La comunicación es exclusivamente por la API REST del plugin (HTTP + firma HMAC). No hay enlazado, bibliotecas compartidas ni código común en tiempo de ejecución.
   - Los vectores de prueba de la firma (`packages/shared/fixtures/wp-signature-v1.json`) son **datos** de prueba, no código; los usan las pruebas de ambos lados y no se incluyen en el zip.
4. **Dependencias del plugin**: solo con licencia compatible con GPL-2.0-or-later. Las herramientas de desarrollo de Composer (PHPCS, PHPStan, PHPUnit) no se distribuyen en el zip.

## Consecuencias

- Cualquiera puede leer, modificar y redistribuir el código del plugin según la GPL. Se acepta: el plugin no contiene lógica de negocio (solo vinculación, firma y lectura/escritura de contenido); el valor de Faro está en la app y la nube, que siguen privativas.
- Comunicarse con un programa GPL por HTTP a distancia no convierte a la app en obra derivada; es el mismo modelo que usan las apps de escritorio y SaaS que hablan con plugins GPL. **Recomendación**: que un abogado lo confirme antes del lanzamiento público; la arquitectura ya está diseñada para mantener la separación.
- Regla permanente: **ningún archivo de `apps/` se copia ni se importa en `packages/wp-plugin/`** (y al revés). `revisor-seguridad` y `wordpress-php` lo verifican en cada PR del plugin.
- Tareas (spec F1a): `wordpress-php` crea `packages/wp-plugin/LICENSE` y las cabeceras; `devops-release` añade la excepción al `LICENSE` y al `README.md` de la raíz y se asegura de que el zip incluya `LICENSE` y excluya pruebas y herramientas de desarrollo.
- Una versión del plugin publicada con GPL no se puede "cerrar" después: las versiones ya distribuidas siguen siendo GPL. Cambiar la licencia del plugin en el futuro requiere un ADR nuevo.
