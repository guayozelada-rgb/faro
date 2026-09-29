=== Faro ===
Contributors: concersa
Tags: seo, marketing, woocommerce, contenido
Requires at least: 6.0
Tested up to: 7.1
Requires PHP: 8.1
Stable tag: 0.1.0
License: GPL-2.0-or-later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Conecta tu sitio con Faro, la app de escritorio de marketing digital, para que pueda leer tus páginas, entradas y productos.

== Description ==

Faro es una app de escritorio (Windows y macOS) que te ayuda a atraer más clientes a tu sitio de WordPress o WooCommerce. Este plugin conecta tu sitio con la app para que Faro pueda leer tu contenido publicado.

* No necesita tu contraseña de WordPress: la conexión se hace con un código de 6 números que generas en Ajustes → Faro.
* El código dura 10 minutos y solo sirve una vez.
* Faro solo lee páginas, entradas y productos publicados. En esta versión no cambia nada en tu sitio.
* Puedes desconectar Faro cuando quieras desde Ajustes → Faro.
* Compatible con WooCommerce y con las tablas de pedidos de alto rendimiento (HPOS). WooCommerce es opcional.

== Installation ==

1. Entra al panel de WordPress de tu sitio.
2. Ve a Plugins → Añadir nuevo → Subir plugin, elige faro-wordpress.zip, pulsa Instalar ahora y después Activar.
3. Ve a Ajustes → Faro y pulsa Generar código de conexión.
4. Abre Faro en tu computadora, ve a Configuración → Sitios conectados y escribe la dirección de tu sitio y el código.

Tu sitio debe usar HTTPS.

== Frequently Asked Questions ==

= ¿Faro guarda mi contraseña? =

No. Faro nunca te pide tu contraseña de WordPress.

= ¿Qué pasa si cambian las claves de seguridad de WordPress? =

La conexión deja de funcionar. Genera un código nuevo en Ajustes → Faro y escríbelo en Faro.

== Privacy ==

* El plugin no hace peticiones a otros servidores ni envía datos por su cuenta: solo responde a las peticiones firmadas de la app Faro que tú conectaste.
* No usa cookies ni rastreo.
* Guarda en tu base de datos una conexión (opción faro_connection) con un identificador, un resumen (hash) de la credencial, un secreto cifrado con las claves de seguridad de tu WordPress, un identificador aleatorio de la app, la versión de la app y las fechas de conexión y de última lectura.
* Mientras generas un código guarda su resumen (opción faro_pairing) durante 10 minutos, y guarda datos temporales para evitar repeticiones y limitar intentos (transients faro_*). Del límite de intentos solo guarda un resumen (hash) de la dirección IP, durante 15 minutos.
* La app Faro lee títulos, direcciones, slugs y fechas de modificación de tus páginas, entradas y productos publicados, y datos generales del sitio (nombre, dirección, versiones de WordPress y WooCommerce, número de páginas, entradas y productos, y si usas Yoast SEO o Rank Math). No lee pedidos, clientes ni otros datos personales.
* Al borrar el plugin se eliminan todas sus opciones y datos temporales.

== Changelog ==

= 0.1.0 =
* Primera versión: conexión con código de 6 números, lectura de páginas, entradas y productos, pantalla de estado en Ajustes → Faro.
