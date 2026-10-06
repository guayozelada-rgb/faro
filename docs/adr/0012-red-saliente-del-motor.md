# ADR 0012 — Red saliente del motor: SSRF, tiempos, reintentos y sitios locales en desarrollo

- **Fecha:** 2026-09-29
- **Estado:** aceptado; actualizado el 2026-10-01 (cierre de F1a) y el 2026-10-06 (almacén de certificados del sistema, con las condiciones 12 a 14 de su revisión de seguridad), al final
- **Spec:** [F1a — Conexión con WordPress](../specs/2026-09-29-f1a-conexion-wordpress.md)
- **Skill:** `revision-seguridad` §5

## Contexto

En F1a el motor hace por primera vez peticiones a una URL que **escribe el usuario** (su sitio WordPress). En F2 el rastreador (crawler) hará lo mismo a escala. Sin protección, la URL puede apuntar a `localhost` (incluido el propio motor), a la red local del usuario o a direcciones de metadatos de nube (SSRF). Además, las pruebas con wp-env necesitan justo lo contrario: conectar con `http://localhost:8888`.

## Decisión

Un módulo común `faro_engine/net/` (guardia de red + cliente `httpx`) que usan todas las peticiones salientes a URLs no fijas en código. Reglas:

1. **URL**: se recortan espacios; sin esquema → `https://`; solo `https` (salvo el modo de desarrollo, abajo); sin usuario ni contraseña en la URL; host en minúsculas e IDN en punycode; sin puerto o puerto 443; se quitan consulta y fragmento; se conserva la ruta (WordPress en subcarpeta) sin barra final; máximo 2048 caracteres.
2. **Direcciones prohibidas**: se resuelve el nombre y se rechaza si **alguna** dirección es de loopback, privada (RFC 1918), enlace local (`169.254.0.0/16`, `fe80::/10`), CGNAT (`100.64.0.0/10`), ULA (`fc00::/7`), multicast, reservada, no especificada, o IPv4 mapeada en IPv6 a cualquiera de ellas.
3. **Sin segunda resolución** (DNS rebinding): la conexión se hace a la IP ya validada, con SNI y `Host` del nombre original.
4. **Redirecciones**: no se siguen de forma automática. Solo el descubrimiento inicial del sitio sigue hasta 3, validando cada salto con las reglas 1–3. Las peticiones firmadas nunca siguen redirecciones (una redirección → `site.moved`).
5. **TLS siempre verificado**; sin proxies del sistema (`trust_env=False`); `User-Agent: Faro/<versión>`.
6. **Tiempos**: conexión 5 s, lectura 15 s, 20 s por petición; cada operación del motor tiene además un plazo total (5 s menos que el `x-faro-timeout-seconds` que aplica el núcleo).
7. **Tamaño**: respuestas de más de 5 MB se cortan (`site.response_too_large`).
8. **Reintentos**: solo peticiones idempotentes (GET, DELETE): hasta 2 reintentos ante error de red, 502, 503 o 504, con espera de 1 s y 3 s (±20 % aleatorio); ante 429 un reintento respetando `Retry-After` si es ≤ 10 s. Cada reintento firmado lleva nonce y hora nuevos. **Nunca** se reintenta `POST /pair` (un código ya usado no sirve dos veces).
9. **Concurrencia**: como máximo 4 peticiones simultáneas en total y 1 por sitio.

**Modo sitios locales (solo desarrollo)**: permite `http` y hosts de loopback (`localhost`, `127.0.0.1`, `::1`) con cualquier puerto, **excepto el puerto del propio motor**. Se activa solo si:
- el núcleo es un build de depuración (`cfg!(debug_assertions)`) y `.env.local` tiene `FARO_ALLOW_LOCAL_SITES=1`, en cuyo caso lanza el motor con `--allow-local-sites`; o el motor corre con `--dev` y el mismo valor está en `.env.local`;
- y el motor **no** está empaquetado (`--allow-local-sites` con `sys.frozen` → sale con código 2).
Las redes privadas siguen prohibidas también en este modo.

## Consecuencias

- El rastreador de F2 reutiliza `faro_engine/net/` (añadirá robots.txt y límites por dominio).
- Sitios que solo funcionan por `http`, en un puerto distinto de 443, detrás de un proxy corporativo obligatorio o en la red local del usuario no se pueden conectar en F1a. Se acepta: el usuario objetivo tiene su tienda publicada con HTTPS.
- Cobertura del 95 % exigida en el módulo de guardia de red.

## Actualización (2026-10-01, cierre de F1a)

### 1. Reglas más estrictas que las de arriba (ya implementadas)

- **Regla 2**: además de la lista, se rechaza toda dirección que Python no considere **global** (`is_global`), incluidas las de documentación, y las IPv6 de **NAT64** (`64:ff9b::/96`) o IPv4 mapeadas que apunten a una dirección prohibida.
- **Regla 1**: se rechazan los hosts cuya última etiqueta es numérica o hexadecimal (formas raras de escribir una IP) y los `*.localhost`.
- **Modo local**: el `http` solo va a `localhost`, `127.0.0.1` y `::1`, y esos nombres deben resolver **solo** a loopback.
- **Compresión**: el cliente pide `Accept-Encoding: identity` y rechaza cualquier respuesta con otra `Content-Encoding` (`site.bad_response`), para que el límite de 5 MB no se pueda saltar con una respuesta comprimida.
- **Descubrimiento (regla 4)**: el código de vinculación solo se envía al mismo host que escribió el usuario o a su variante con o sin `www.` (mismo esquema y puerto). Una redirección a otro host → `site.moved` antes de llamar a `/pair`.
- **Contenido**: la `url` de cada elemento que devuelve el plugin debe empezar por `http://` o `https://`; si no, la respuesta es inválida.

### 2. Reserva para deshacer dentro del plazo del motor (regla 6)

El plazo total del motor (5 s menos que el `x-faro-timeout-seconds` del núcleo) se reparte así en las operaciones que crean o borran secretos:

- **`connectSite`** (núcleo 60 s, motor 55 s): el trabajo (descubrir, `pair`, `create`, `status`, insertar en la base) puede durar hasta los **45 s**. Los últimos **10 s** (`UNDO_RESERVE_SECONDS`) quedan para deshacer una vinculación a medias: primero el `delete` del secreto (lo crítico: un secreto huérfano no lo ve nadie) y después el `revoke` remoto, sin reintentos y con **5 s** como mucho (`UNDO_REVOKE_SECONDS`). Así el `delete` llega mientras la concesión del núcleo sigue viva (caduca a `timeout + 5 s`).
- La inserción en la base también va acotada por el plazo del trabajo (`site.timeout`).
- Si ya no queda tiempo de trabajo, no se envía la `create`: solo se revoca el `pair`.
- Cada `secret_request` espera `min(10 s, plazo restante)`; sin tiempo, falla cerrado sin escribir nada (ADR 0010, actualización D).
- **`removeSite`**: el `revoke` termina `UNDO_RESERVE_SECONDS` antes del final, para que el `delete` del secreto tenga su tiempo.
- Código: `faro_engine/core/config.py`, `sites/service.py`, `net/client.py` (`Deadline.ending_before`, `Deadline.capped`, `SafeHttpClient.limited_to`).

### 3. Modo de sitios locales en `--dev`: se alinea el ADR con el código

**Decisión:** en el motor con `--dev`, el modo de sitios locales se activa con el argumento `--allow-local-sites` **o** con `FARO_ALLOW_LOCAL_SITES=1` en `.env.local`; cualquiera de los dos basta. Sustituye a "o el motor corre con `--dev` y el mismo valor está en `.env.local`" de arriba. El modo gestionado no cambia: el núcleo pasa el argumento solo en un build de depuración **y** con `FARO_ALLOW_LOCAL_SITES=1` (leída del entorno del proceso o de `.env.local`).

Motivos:

- La doble llave del modo gestionado protege contra que el **núcleo** active el modo sin querer. En `--dev` no hay núcleo de por medio: el argumento lo escribe el propio desarrollador en su línea de comandos, que ya es una decisión explícita.
- `--dev` ya exige un motor no empaquetado (`sys.frozen` → código 2) y un `.env.local` con token propio; un build empaquetado rechaza tanto `--dev` como `--allow-local-sites`.
- En `--dev` no hay canal de secretos (`engine.secrets_unavailable`): ninguna operación con sitios puede terminar. Como mucho se descubre y vincula un WordPress en loopback, y la vinculación se deshace al fallar la `create`.
- Las redes privadas siguen prohibidas también en este modo, y el puerto del propio motor también.

Pendiente de release (spec F1a §12): una prueba que demuestre que el lanzador PyInstaller nunca pasa `--allow-local-sites` ni `--dev`.

## Actualización (2026-10-06): HTTPS con el almacén de certificados del sistema (`truststore`)

- **Spec y tarea:** [F1b](../specs/2026-10-05-f1b-capa-ia-y-motor-de-agentes.md), tarea **T2b**.
- **Origen:** informe de T2 ([`docs/qa/2026-10-05-f1b-t2-dependencias.md`](../qa/2026-10-05-f1b-t2-dependencias.md)): §8 (criterio 8), hallazgo 7 de §10 y la recomendación de `revisor-seguridad` en §12.

### Contexto

La regla 5 ("TLS siempre verificado") se implementó con `verify=True` en `net/client.py`, es decir, con las raíces de `certifi` (la lista de Mozilla que viaja dentro del motor). En el equipo del usuario, un antivirus (Norton Web/Mail Shield) intercepta HTTPS **de forma selectiva, según el dominio**, y firma lo que intercepta con su propia raíz, instalada en el almacén de Windows:

- con `certifi`, `https://pypi.org` falla con `CERTIFICATE_VERIFY_FAILED`; con el almacén del sistema (`truststore`) funciona;
- `https://api.anthropic.com` funciona con los dos, porque Norton no intercepta ese dominio.

Así que conectar un WordPress real por HTTPS puede fallar en F1a con `site.tls_error` sin que nadie haya hecho nada mal. LiteLLM (F1b) ya necesita `truststore` por el mismo motivo (`truststore==0.10.4` en `uv.lock`, condición 15 del informe). Exportar la raíz del antivirus y apuntar `SSL_CERT_FILE` a ella también funciona, pero se rompe cada vez que el antivirus renueva su certificado y no sirve para otros usuarios.

### Decisión

**Todo el HTTPS saliente del motor verifica los certificados contra el almacén de certificados del sistema operativo, con `truststore`, en lugar de `certifi`.** Esto incluye `faro_engine/net` (sitios WordPress y, más adelante, el rastreador) y los clientes propios de la capa de IA (skill `capa-llm`). La regla 5 queda así: *TLS siempre verificado contra el almacén del sistema*.

Forma:

- Un único módulo, `faro_engine/net/tls.py`, construye el contexto **de forma explícita y una sola vez por proceso**: `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`, con las propiedades de abajo. Lo expone una función con caché (`tls_context()`).
- Todo cliente HTTPS del motor recibe ese mismo objeto: `httpx.AsyncHTTPTransport(verify=tls_context(), trust_env=False, …)`. Esto vale para `net/client.py` (`default_transport`) y para los transportes de LiteLLM de la condición 15.
- No se depende de `truststore.inject_into_ssl()`. La inyección global sigue en la capa de IA solo como **segunda barrera**, para los contextos que LiteLLM crea por su cuenta (condición 1 del informe, T6), y solo a través de `net/tls.py::install_system_trust_for_libraries()` (condición 13). Lo que protege es el contexto explícito.

### Condiciones (obligatorias; las comprueba `revisor-seguridad`)

1. **Construido una sola vez y compartido.** Solo `net/tls.py` crea contextos TLS de cliente. Ningún otro módulo llama a `ssl.create_default_context`, `ssl.SSLContext(...)`, `truststore.SSLContext(...)`, `httpx.create_ssl_context` ni pasa `verify=True`, `verify=<ruta>` o `verify=False`. Una prueba recorre `faro_engine/` y lo comprueba. La única excepción es `net/tls.py`.
2. **Verificación del certificado:** `verify_mode == ssl.CERT_REQUIRED`.
3. **Verificación del nombre:** `check_hostname is True`. En Windows y macOS la hace `truststore` con la API del sistema, a partir del `server_hostname` del socket. Ese `server_hostname` es el `sni_hostname` que pone `pin_request`, así que el nombre que se valida es el que escribió el usuario, nunca la IP fijada.
4. **Versión mínima:** `minimum_version = ssl.TLSVersion.TLSv1_2`.
5. **Sin raíces añadidas por Faro.** El contexto de producción nunca llama a `load_verify_locations`, `load_default_certs` ni `set_default_verify_paths`. En Windows y macOS, `get_ca_certs()` devuelve una lista vacía (con `truststore`, las raíces cargadas así se sumarían a las del sistema). No hay opción de "confiar en este certificado" ni de "ignorar errores de certificado": ni en la interfaz, ni en la configuración, ni en variables de entorno.
6. **Nada de `SSL_CERT_FILE`, `SSL_CERT_DIR` ni variables parecidas.** El motor las quita del entorno **al principio de `__main__`, antes de cualquier otro import**, junto a `SSLKEYLOGFILE`, que ya se quita. Esto **no estaba hecho**: hasta ahora solo se quitaba `SSLKEYLOGFILE`, y la limpieza completa de `SSL_*` era la condición 1 de T6. T2b lo adelanta para estas dos. Motivo: en Linux, `truststore` usa las rutas por defecto de OpenSSL, que leen esas variables; en Windows y macOS, si algo llamara a `load_default_certs`, también se cargarían. El ejecutable de PyInstaller hace lo mismo (condición 17 del informe, T11).
7. **`trust_env=False`** en el transporte y en el cliente, como hasta ahora. Esto excluye proxies del entorno y del registro de Windows, y también `SSL_CERT_FILE` vía httpx.
8. **La fijación de IP con SNI queda intacta** (reglas 2 y 3): se resuelve una vez, se conecta a la IP validada y se usan `Host` y `sni_hostname` del nombre original. Con una IP literal en la URL no hay `sni_hostname` y se valida contra la IP, como hoy.
9. **Contexto inmutable tras construirse.** Nadie cambia sus propiedades después de `tls_context()`. httpcore llama a `set_alpn_protocols` en cada conexión con el mismo valor (`http/1.1`), así que todos los transportes que lo compartan deben usar `http2=False`, como hoy. Si algún día uno necesita HTTP/2, tendrá su propio contexto construido en `net/tls.py`.
10. **Sin datos sensibles en los registros.** Al construir el contexto se escribe una línea `net.tls_context_ready` con `store` (`system` o `certifi`, ver el plan de respaldo) y la versión de `truststore`. Nunca se registran certificados, huellas ni nombres de las raíces.
11. **Sin cambios en los errores:** un fallo de verificación sigue siendo `site.tls_error` (en la capa de IA, el código `llm.*` que corresponda), con el mismo texto para el usuario.
12. **(Revisiones de T2b) Un cerrojo por contexto.** `truststore` guarda `verify_mode` y `check_hostname` del contexto interno, pone `CERT_NONE` y `check_hostname=False` durante cada `wrap_bio` y `wrap_socket`, y al salir restaura lo que guardó. Además, verifica la cadena dentro de `do_handshake` del `SSLObject` y lee ahí `verify_mode` y `check_hostname` del contexto interno. `wrap_bio`, que es lo que usan asyncio y anyio, no toma ningún cerrojo, y `wrap_socket` solo lo toma al entrar. Si dos hilos se cruzan, uno "restaura" el estado degradado del otro y el contexto compartido queda **para siempre** en `CERT_NONE` (el revisor lo reprodujo 3 de 3), o un handshake se hace mientras otro hilo tiene el contexto degradado y se acepta cualquier certificado (reproducido con una CA desconocida y un nombre distinto). Y varios hilos **sí** usan el contexto en el funcionamiento normal: anyio 4.15 (`TLSStream.wrap`, `anyio/streams/tls.py:153-168`), que es lo que usa httpx, llama a `wrap_bio` con `to_thread.run_sync` (en un **hilo de trabajo**) cuando `type(ssl_context) is not ssl.SSLContext`, y luego hace `do_handshake` en el hilo del bucle. Por eso:
    - el contexto de `truststore` es una subclase con **un `threading.Lock` (no reentrante) por contexto** (`_LockedContext`). El cerrojo cubre toda la ventana degradada de `wrap_bio` y `wrap_socket` (incluida la restauración) y **cada llamada** a `do_handshake` de los `SSLObject` y `SSLSocket` que crea el contexto, que es donde `truststore` lee `verify_mode` y `check_hostname`. Para eso sustituye `sslobject_class` y `sslsocket_class` de su contexto interno por subclases que toman el cerrojo. Así ningún handshake ve el estado degradado y ninguna restauración se cruza con otra, venga del hilo que venga: las conexiones de cualquier hilo se **serializan** en esos puntos y todas verifican. No basta con protegerlo en `tls_context()`, porque el transporte guarda el objeto;
    - con BIO no bloqueante (asyncio, anyio), `do_handshake` se llama varias veces y cada llamada es corta: el cerrojo solo se retiene durante cada llamada, y un `wrap_bio` en un hilo de trabajo solo espera unos microsegundos. Con `wrap_socket` bloqueante (hoy el motor no lo usa), el cerrojo se retiene durante **todo** el handshake de red y ningún otro hilo puede conectar con ese contexto mientras tanto. El handshake que `wrap_socket` hace dentro (`do_handshake_on_connect`) ocurre en el mismo hilo con el cerrojo ya tomado y no lo vuelve a pedir (una marca por hilo lo indica; si no, el hilo se bloquearía a sí mismo);
    - `read` y `write` de ese `SSLObject` pasan antes por el `do_handshake` protegido si aún no se completó: sin eso, OpenSSL haría el handshake implícito con el `CERT_NONE` copiado al crear el objeto y sin la verificación de `truststore`;
    - los objetos TLS guardan una **referencia débil** al contexto: si ya no existe, el handshake falla cerrado (`ssl.SSLError`, registro `net.tls_context_gone`);
    - **sin renegociación:** todo contexto lleva `OP_NO_RENEGOTIATION`, tanto el del motor (`_harden`) como el respaldo y los inyectados (en el constructor de la subclase, condición 13). Mientras se acepte TLS 1.2, una renegociación pedida por el servidor haría un handshake nuevo dentro de OpenSSL, con el `CERT_NONE` copiado al crear el objeto y sin pasar por la verificación de `truststore`, y podría cambiar el certificado del servidor a mitad de conexión;
    - el respaldo con `certifi` es un `ssl.SSLContext` de la biblioteca estándar y no lleva el cerrojo: su `wrap_bio` y su `wrap_socket` no cambian el contexto (OpenSSL lee una configuración que nadie modifica, condición 9). Una prueba con cuatro hilos lo comprueba;
    - **historia:** la primera revisión de T2b pidió un "dueño de hilo" (el primer hilo que conectaba era el único que podía usar el contexto y los demás recibían `WrongThreadError`). La revisión final lo rechazó: con anyio, el `wrap_bio` en un hilo de trabajo fijaba ese hilo como dueño y el handshake en el bucle fallaba, así que **todo** el HTTPS real con `default_transport()` daba `site.tls_error` (reproducido contra example.com, pypi.org y google.com, y en la CI de los tres sistemas). Además, varios hilos de trabajo de anyio hacían `wrap_bio` a la vez, justo la carrera que había que evitar. `WrongThreadError` y `net.tls_wrong_thread` ya no existen;
    - pruebas (todas en memoria, sin loopback, así que también corren con un antivirus que intercepta loopback): el patrón de anyio (`wrap_bio` en `anyio.to_thread.run_sync` y `do_handshake` en el bucle) verifica y acepta la CA de prueba inyectada y rechaza una CA desconocida y otro nombre; `TLSStream.wrap` real sobre flujos en memoria hace lo mismo y un espía comprueba que `wrap_bio` ocurrió en un hilo de trabajo; `httpx.AsyncHTTPTransport(verify=<contexto del motor>)` con una red de httpcore en memoria completa una petición (200, SNI del nombre) y `SafeHttpClient` con `default_transport()` también, con la IP fijada; los rechazos (CA desconocida, otro nombre, caducado) dan `site.tls_error` sin que el servidor reciba un byte de la petición. Carrera: ocho hilos (dos solo envuelven sin parar y seis hacen handshakes con la CA válida y con una desconocida), 360 handshakes por ejecución, ninguna CA desconocida aceptada, todas las válidas aceptadas y el contexto sigue en `CERT_REQUIRED` con `check_hostname`; con el cerrojo desactivado, la prueba falla (se aceptan CA desconocidas y el contexto queda en `CERT_NONE`). Reproducciones del revisor: `rev2_readside` (otro hilo dentro de `wrap_socket` mientras se hace el handshake de un `SSLObject`) ahora **espera** al cerrojo, verifica y rechaza; `rev2_readnohs` (`write` sin handshake) sigue rechazando, también con el objeto creado en otro hilo; `rev2_bypass`: las vías que esquivan la subclase (`ssl.SSLContext.wrap_bio(ctx, …)`, `ssl.SSLObject._create(context=ctx)` y el contexto interno) siguen fallando cerradas, y las de "otro hilo" (el primer hilo termina, ocho hilos a la vez en el primer `wrap_bio`, dos bucles en dos hilos y `run_in_executor`) ahora se serializan y verifican. Además: `wrap_bio`, `wrap_socket` y `do_handshake` de `SSLObject`/`SSLSocket` esperan mientras otro hilo tiene el cerrojo; `wrap_socket` desde otro hilo verifica sin bloquearse a sí mismo; falla cerrado si el contexto ya no existe; y `OP_NO_RENEGOTIATION` está en el contexto del motor, en el respaldo, en los inyectados y en cada objeto TLS.
13. **(Revisión de T2b) La inyección global, solo con `install_system_trust_for_libraries()`.** Esa función de `net/tls.py` construye primero el contexto del motor (así el respaldo se decide antes de sustituir `ssl.SSLContext`) y, solo si el almacén es el del sistema, inyecta. Con el respaldo no inyecta. T6 la llama antes de importar LiteLLM; `__main__` todavía no la llama. La comprobación estática prohíbe `inject_into_ssl` y cualquier import de `truststore` fuera de `net/tls.py`.
    - **(Segunda revisión de T2b) Se inyecta la subclase protegida, no `truststore.SSLContext`.** Con `truststore.inject_into_ssl()`, `ssl.SSLContext`, `ssl.create_default_context()` y `urllib3.util.ssl_.SSLContext` pasan a dar la clase de `truststore` sin protección, y cualquier contexto **compartido** creado después (`aiohttp.connector._SSL_CONTEXT_VERIFIED`, `_ssl_context_cache` de LiteLLM) vuelve a tener la carrera de la condición 12 y puede quedar en `CERT_NONE`. Por eso la función hace lo mismo que `inject_into_ssl()` de `truststore` 0.10.4, en los mismos puntos (`ssl.SSLContext`, `urllib3.util.ssl_.SSLContext` y, si la versión de `requests` lo tiene, `requests.adapters._preloaded_ssl_context`), pero con `_LockedContext` (que también protege el handshake y lleva `OP_NO_RENEGOTIATION`, condición 12). `truststore.extract_from_ssl()` la sigue deshaciendo. Además, `__class__` de la subclase devuelve la propia subclase (el de `truststore` devolvía `truststore.SSLContext`), así que `ctx.__class__(...)` tampoco da un contexto sin protección. Cada biblioteca que comparta un contexto entre hilos queda serializada por su cerrojo y verifica en todos los hilos, en vez de degradarse.
    - Pruebas, en un proceso aparte (`tests/net/injection_probe.py`) para no contaminar el resto: tras la inyección, `ssl.SSLContext`, el de urllib3, el contexto precargado de `requests` (si existe), el contexto compartido de aiohttp y `ssl.create_default_context()` son la subclase protegida y llevan `OP_NO_RENEGOTIATION`; sobre un `ssl.create_default_context()` compartido, un segundo hilo conecta, y con cuatro hilos haciendo handshakes a la vez mientras otro envuelve sin parar, ninguna de las 400 CA desconocidas se acepta, las 400 válidas sí y el contexto sigue en `CERT_REQUIRED` con `check_hostname` (sin el cerrojo, se aceptan todas las desconocidas); el handshake desde otro hilo verifica y rechaza una CA desconocida; y `extract_from_ssl()` restaura `ssl.SSLContext` y el de urllib3.
    - **(Revisión final de T2b) Excepción conocida: `httpx2` y `httpcore2`.** `langsmith` 0.14.4 (dependencia de LangGraph) trae `httpx2` 2.13.1 y `httpcore2` 2.13.1, que construyen su contexto llamando directamente a `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` (`httpx2/_config.py`, `httpcore2/_ssl.py`). Esa llamada no pasa por `ssl.SSLContext`, así que la inyección no le llega: es un `truststore.SSLContext` **sin cerrojo**, con la carrera de la condición 12. Se acepta solo porque ningún código del motor usa esos paquetes y LangSmith está siempre desactivado (`langsmith.configure(enabled=False)` y `LANGSMITH_*` fuera del entorno: condición 7 y fila 3 de riesgos del informe de T2). Si LangSmith se activara, o si otra dependencia empezara a usar `httpx2`/`httpcore2`, habría que protegerlos antes (o quitarlos). **La prueba de T8** (LangSmith desactivado) debe vigilarlo: comprobar que, al construir y ejecutar un grafo, `httpx2` y `httpcore2` no crean ningún contexto TLS ni abren conexiones.
14. **(Revisión de T2b) Comprobación estática ampliada** (condición 1, prueba 5). Además de lo anterior, detecta:
    - alias de import: `from ssl import create_default_context as mk`, `import ssl as s`, cualquier import de `truststore` o `_ssl`, y alias de `httpx` y `httpcore`;
    - acceso dinámico: `getattr`, `setattr`, `delattr` y `hasattr` con nombres TLS, y `getattr` sobre `ssl` o `truststore`;
    - argumentos: `**kwargs`, `*args`, posicionales a transportes y `mounts=` en llamadas a `httpx` y `httpcore`; `ssl=`, `ssl_context=`, `context=`, `cafile=`, `capath=` y `cadata=` con algo distinto de `tls_context()`, también dentro de `**{...}` o `**dict(...)`;
    - clientes sin control: `httpx.Client` y `httpx.AsyncClient` sin `transport=`; `httpx.get`, `post`, `request`, `stream` y similares; `urlopen`, `build_opener`, `HTTPSHandler` y `HTTPSConnection`;
    - atributos del contexto: asignaciones a `verify_mode`, `check_hostname`, `minimum_version`, `maximum_version`, `options`, `verify_flags`, `keylog_filename`, `hostname_checks_common_name`, `post_handshake_auth`, `sslobject_class` y `sslsocket_class`, y cualquier referencia a `_create_unverified_context` o `_create_default_https_context`;
    - **(segunda revisión de T2b)** referencias a `_create_stdlib_context`, `get_server_certificate`, `set_ciphers`, `set_ecdh_curve`, `set_alpn_protocols`, `set_npn_protocols`, `load_cert_chain`, `SSLObject` y `SSLSocket`; `cert_reqs=` con cualquier valor; el uso directo de `requests`, `urllib3` y `aiohttp` dentro de `faro_engine` (import, `import_module` o `__import__`; esas bibliotecas solo las usa LiteLLM, al que llega la condición 13); transportes, pools y proxies de `httpx`/`httpcore` sin `verify=tls_context()`/`ssl_context=tls_context()`, incluidos `httpx.AsyncHTTPTransport()` y `httpx.AsyncClient(transport=httpx.AsyncHTTPTransport())`; `trust_env` distinto de `False` en llamadas a `httpx`/`httpcore`; `start_tls` sin `sslcontext=tls_context()` (el contexto por posición en `loop.start_tls` y `StreamWriter.start_tls`); y el contexto por posición en `asyncio.open_connection` y `loop.create_connection`.

    Cada forma tiene su caso en la parametrización de control.

### Modelo de amenaza: qué cambia y qué no

**Qué cambia**

- **Raíces de confianza:** el motor confía en las mismas raíces que el navegador del usuario. Eso incluye las que el propio usuario, su empresa (directiva de grupo o MDM) o un programa instalado (antivirus, proxy de inspección) hayan añadido al almacén del sistema.
- **Lo que ve quien intercepta:** quien controle una de esas raíces puede descifrar el tráfico HTTPS del motor. Antes, el motor rechazaba esa conexión (fallo cerrado); ahora la acepta. Para los dominios que intercepte, el interceptor verá:
  - las claves de IA en las cabeceras;
  - las peticiones firmadas al plugin;
  - la respuesta de `/pair`, que trae el token y el secreto HMAC del sitio.
- **Por qué se acepta:** instalar una raíz en el almacén exige controlar el equipo, o administrarlo en el caso de una empresa. Quien puede hacer eso ya puede leer la memoria del motor, registrar el teclado o leer el llavero cuando el usuario lo desbloquea. Es el mismo modelo que aplican el navegador y el resto de aplicaciones del sistema. La alternativa (`certifi`) no protege al usuario de su antivirus: solo hace que Faro no funcione.
- **Validación que hace el sistema operativo:** según el sistema, la validación puede descargar certificados intermedios que falten o consultar listas de revocación por su cuenta, con su propia pila de red. Esas peticiones no pasan por la guardia SSRF ni por `trust_env=False`. Van a URLs que vienen dentro del certificado, no llevan secretos de Faro y su respuesta no llega al motor. Riesgo residual aceptado, igual que en el navegador.
- **(Revisión de T2b; riesgo conocido, medio) AIA en Windows bloquea el bucle.** En Windows, `truststore` llama a `CertGetCertificateChain` con `chain_flags=0` dentro de `do_handshake`, es decir, **de forma síncrona en el hilo del bucle de eventos**. Si el certificado del servidor no trae el intermedio y su extensión AIA apunta a una URL que no responde (un servidor hostil puede elegirla), Windows intenta descargarlo y el motor entero queda bloqueado unos **15 s por conexión**. El revisor lo reprodujo con una URL AIA en loopback que acepta la conexión y no contesta. Mientras tanto el motor no atiende ninguna otra petición, `/health` incluido. Hoy solo se conecta a los sitios que añade el usuario y a los proveedores de IA, así que se acepta como riesgo conocido. Mitigación prevista: la condición para F2 (Consecuencias); si hiciera falta antes, mover la verificación a un hilo de trabajo con su propio contexto o limitar el tiempo de recuperación de URL de la cadena.
- **Actualización de las raíces:** las raíces retiradas de la lista de Mozilla dejan de depender de que publiquemos una versión con `certifi` nuevo y pasan a depender de las actualizaciones del sistema. En un sistema sin actualizar puede seguir habiendo raíces que Mozilla ya retiró. Se acepta: Faro exige un sistema con soporte (Windows 10/11, macOS con soporte de Tauri 2).

**Qué no cambia**

- La guardia SSRF, la IP fijada, el SNI y la validación del nombre original.
- Sin redirecciones automáticas, salvo el descubrimiento (regla 4); las peticiones firmadas nunca siguen redirecciones.
- Sin proxies del sistema ni del entorno; los proxies corporativos **obligatorios** siguen sin estar soportados (Consecuencias). Lo que ahora sí funciona son los interceptores **transparentes** cuya raíz está en el almacén.
- TLS verificado siempre: un certificado autofirmado, caducado, de una CA que no está en el almacén o con un nombre que no coincide se sigue rechazando.
- Ninguna variable de entorno, archivo ni opción de la interfaz puede añadir raíces ni desactivar la verificación.

### Comportamiento por plataforma

| Plataforma | Qué verifica | Notas |
| --- | --- | --- |
| **Windows** (objetivo) | La CryptoAPI del sistema (cadena y política SSL con el nombre del servidor): raíces de la máquina, del usuario y de empresa | Es el caso de Norton. Windows puede descargar raíces de confianza desde Windows Update cuando le falta una. |
| **macOS** (objetivo) | Security.framework (`SecTrust` con política SSL y nombre): raíces del sistema y de los llaveros con ajustes de confianza del usuario, del administrador o de MDM | `truststore` carga Security y CoreFoundation con `ctypes`. Hay que comprobarlo en el ejecutable **firmado con hardened runtime** (skill `release-y-firma`) antes de la primera versión para macOS. |
| **Linux** (solo CI y desarrollo; no se distribuye) | OpenSSL con el paquete de CA de la distribución (rutas por defecto) | OpenSSL hace la verificación del nombre. Aquí `SSL_CERT_FILE` y `SSL_CERT_DIR` **sí** cambiarían las raíces: por eso la condición 6 las quita. |

### Pruebas exigidas (T2b; en loopback, sin red real)

Se hacen con certificados generados en la prueba (`trustme` en el grupo `dev`) y un servidor TLS en `127.0.0.1`. Usan el transporte real que construye `default_transport()` y una petición preparada con `pin_request` (conexión a `127.0.0.1` con `sni_hostname` = nombre de prueba). Se ejecutan en el trabajo `engine` de la CI en **Windows, macOS y Linux**. Ninguna queda en `xfail`.

1. **Propiedades del contexto:** `tls_context()` devuelve siempre el mismo objeto, de tipo `truststore.SSLContext`, con `CERT_REQUIRED`, `check_hostname`, mínimo TLS 1.2 y `get_ca_certs() == []`. `default_transport()` lo usa con `trust_env=False` y `http2=False`.
2. **Autofirmado rechazado aunque esté en `SSL_CERT_FILE`:** en un proceso aparte, con `SSL_CERT_FILE` y `SSL_CERT_DIR` apuntando a la CA de prueba que firmó el certificado del servidor, se importa el punto de entrada del motor y se hace una petición → `site.tls_error`. El servidor no recibe ningún byte de la petición HTTP (la cabecera nunca sale).
   - **Prueba de control:** un `ssl.create_default_context()` creado **antes** de la limpieza sí acepta ese servidor. Así se demuestra que la variable habría tenido efecto.
   - **Variante solo para Windows y macOS:** aunque la variable siga en el entorno (sin la limpieza), `tls_context()` rechaza el servidor.
3. **Nombre que no coincide, rechazado:** con un contexto construido por la misma función que `tls_context()` (sin caché) y la CA de prueba añadida **solo en la prueba** con `load_verify_locations`, un certificado válido para `otro.test`, pedido con `sni_hostname="sitio.test"` → `site.tls_error`. Con `sni_hostname="otro.test"` → 200. La única diferencia entre los dos casos es el nombre, así que el rechazo se debe a la verificación del nombre. Es la prueba que demuestra que el nombre se sigue validando en Windows y en macOS, donde lo hace `truststore` y no OpenSSL.
4. **Certificado caducado y certificado de una CA desconocida, rechazados**, con el mismo montaje.
   - **(Revisión de T2b) IP literal y comodines, en memoria** (se ejecutan en los tres sistemas): un certificado con la IP correcta se acepta; con otra IP, se rechaza; un certificado DNS pedido por IP, se rechaza; un comodín `*.sitio.test` no cubre `a.b.sitio.test`.
5. **Comprobación estática de la condición 1:** ningún módulo de `faro_engine/` fuera de `net/tls.py` crea contextos TLS ni pasa `verify=` con otro valor.
6. **Ejecutable congelado:** la comprobación `tls` de `scripts/bundle_smoke.py` construye además `tls_context()` y comprueba sus propiedades (prueba 1) dentro del ejecutable. Esto demuestra que `truststore` y sus módulos de plataforma entran en el paquete.

**(Revisión de T2b) En la CI no se omite nada:** si `CI` o `GITHUB_ACTIONS` están definidas y se detecta un interceptor de TLS en loopback, las pruebas que lo comprueban fallan (`pytest.fail`) en vez de omitirse.

**Una CA del sistema se acepta.** Esto no se puede probar en la CI sin instalar una raíz en el almacén del runner: en Windows exige escribir en el almacén de la máquina como administrador, y en macOS cambiar ajustes de confianza, que puede pedir autorización interactiva. Se verifica así:

- **Obligatorio, en el equipo del usuario (Windows 11 con Norton):** `uv run python scripts/manual_tls_check.py` hace un GET con `SafeHttpClient` y la configuración de producción a `https://pypi.org/simple/`, un dominio que Norton intercepta y que no lleva claves. Muestra solo el código HTTP y `store=system`. Resultado esperado: HTTP 200, cuando antes de T2b daba `site.tls_error`. Además, conectar un WordPress real por HTTPS desde la app (flujo de F1a) funciona. Los resultados se anotan en el informe de T2b.
- **Opcional:** un trabajo manual (`workflow_dispatch`) en `windows-latest` que instala la CA de prueba en el almacén de la máquina con `certutil` y repite la prueba 3 sin `load_verify_locations`. Si se hace, también en `macos-latest` con `security add-trusted-cert`; si el runner pide autorización, macOS se verifica a mano antes de su primera versión.

### Plan si `truststore` no está disponible

`truststore` es Python puro y es compatible con Windows, macOS 10.8+ y Linux con OpenSSL, desde Python 3.10. Cubre todas las plataformas de Faro, así que no se espera que falte. Aun así:

- **En el build:** que falte es un error de empaquetado. La comprobación `tls` del humo del ejecutable (prueba 6) falla y la versión no se publica.
- **En ejecución:** si importar `truststore` o construir su contexto lanza una excepción (`ImportError`, `OSError`, `NotImplementedError`), `net/tls.py`:
  - construye una sola vez un contexto con las raíces de `certifi`: `ssl.create_default_context(cafile=certifi.where())` con las mismas condiciones 2–4 (es el comportamiento de F1a);
  - registra `net.tls_context_ready` con `store="certifi"` y un `warning` con solo el nombre de la clase de la excepción.

  Es un respaldo **más restrictivo**, no menos: menos raíces. Nunca hay respaldo a `verify=False`, a `SSL_CERT_FILE` ni a raíces del usuario. Detrás de un interceptor, ese equipo vuelve a ver `site.tls_error`. Hay una prueba con `truststore` simulado como no disponible.
- Si alguna versión futura de `truststore` deja de cumplir las condiciones 2–5, se fija la versión anterior y se reabre este ADR.

### Consecuencias

- **F1a:** los sitios WordPress detrás de un antivirus o un proxy de inspección **transparente** se conectan. Los proxies obligatorios siguen sin estar soportados.
- **F1b:** T6 usa `tls_context()` en los transportes propios de LiteLLM (condición 15), en vez de construir un `truststore.SSLContext` por llamada (skill `capa-llm`). **Condiciones de la revisión de T2b para T6:**
  - solo `acompletion`, con el cliente asíncrono propio de cada llamada;
  - prohibidos `completion` síncrono, `litellm.ssl_verify`, `litellm.aclient_session` y cualquier cliente que LiteLLM construya por su cuenta;
  - la inyección global, solo con `install_system_trust_for_libraries()` antes de importar LiteLLM (condición 13);
  - una prueba que espíe `wrap_bio`, `wrap_socket` y `do_handshake` y demuestre que, durante `acompletion` con los tres proveedores, (a) todos los contextos TLS que se usan son `tls_context()` o un `_LockedContext` (nunca un `truststore.SSLContext` sin cerrojo) y, tras cada llamada, siguen en `CERT_REQUIRED` con `check_hostname`; (b) no se usa ningún contexto de `_ssl_context_cache` de LiteLLM; y (c) nada llama a `wrap_socket` (bloqueante) en el hilo del bucle. `wrap_bio` puede ocurrir en un hilo de trabajo (anyio lo hace así, condición 12): eso ya no es un error.
- **F2 (condición de la revisión de T2b):** el rastreador usa contextos de `net/tls.py` con las mismas condiciones, pero **los handshakes contra hosts no confiables no se hacen en el bucle principal**: van en un proceso aparte o en hilos de trabajo. Cada hilo de trabajo usa **su propio** contexto construido en `net/tls.py` (con `threading.local` y las mismas condiciones 2–5 y 12). No es por corrección (el cerrojo de la condición 12 ya hace seguro compartirlo), sino por rendimiento: con un contexto compartido, los handshakes de todos los hilos se serializarían en su cerrojo, y la verificación de Windows con AIA puede tardar unos 15 s. Nunca se usa `wrap_socket` bloqueante con un contexto compartido con el bucle: retendría el cerrojo durante todo el handshake de red. Motivo: el riesgo de AIA en Windows (modelo de amenaza) y la condición 12.
  - **(Segunda revisión de T2b) Prueba obligatoria en F2:** con un certificado sin intermedio cuya extensión AIA apunta a una URL de loopback que acepta la conexión y no responde, mientras dura el handshake contra ese servidor, `/health` sigue respondiendo dentro de su plazo normal (la prueba mide que el bucle no queda bloqueado). En la CI de Windows es donde el riesgo existe; en macOS y Linux la prueba debe pasar igual.
- **Dependencias:**
  - `truststore` pasa a ser dependencia del núcleo de red, no solo de la capa de IA;
  - `certifi` se queda (dependencia de httpx y respaldo);
  - `trustme` entra en el grupo `dev`, con `cryptography` como dependencia transitiva y solo para pruebas; la cubre el trabajo `audit`.
