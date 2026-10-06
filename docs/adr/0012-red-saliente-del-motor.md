# ADR 0012 — Red saliente del motor: SSRF, tiempos, reintentos y sitios locales en desarrollo

- **Fecha:** 2026-09-29
- **Estado:** aceptado; actualizado el 2026-10-01 (cierre de F1a) y el 2026-10-06 (almacén de certificados del sistema), al final
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
- No se depende de `truststore.inject_into_ssl()`. La inyección global sigue en la capa de IA solo como **segunda barrera**, para los contextos que LiteLLM crea por su cuenta (condición 1 del informe, T6). Lo que protege es el contexto explícito.

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

### Modelo de amenaza: qué cambia y qué no

**Qué cambia**

- **Raíces de confianza:** el motor confía en las mismas raíces que el navegador del usuario. Eso incluye las que el propio usuario, su empresa (directiva de grupo o MDM) o un programa instalado (antivirus, proxy de inspección) hayan añadido al almacén del sistema.
- **Lo que ve quien intercepta:** quien controle una de esas raíces puede descifrar el tráfico HTTPS del motor. Antes, el motor rechazaba esa conexión (fallo cerrado); ahora la acepta. Para los dominios que intercepte, el interceptor verá:
  - las claves de IA en las cabeceras;
  - las peticiones firmadas al plugin;
  - la respuesta de `/pair`, que trae el token y el secreto HMAC del sitio.
- **Por qué se acepta:** instalar una raíz en el almacén exige controlar el equipo, o administrarlo en el caso de una empresa. Quien puede hacer eso ya puede leer la memoria del motor, registrar el teclado o leer el llavero cuando el usuario lo desbloquea. Es el mismo modelo que aplican el navegador y el resto de aplicaciones del sistema. La alternativa (`certifi`) no protege al usuario de su antivirus: solo hace que Faro no funcione.
- **Validación que hace el sistema operativo:** según el sistema, la validación puede descargar certificados intermedios que falten o consultar listas de revocación por su cuenta, con su propia pila de red. Esas peticiones no pasan por la guardia SSRF ni por `trust_env=False`. Van a URLs que vienen dentro del certificado, no llevan secretos de Faro y su respuesta no llega al motor. Riesgo residual aceptado, igual que en el navegador.
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
5. **Comprobación estática de la condición 1:** ningún módulo de `faro_engine/` fuera de `net/tls.py` crea contextos TLS ni pasa `verify=` con otro valor.
6. **Ejecutable congelado:** la comprobación `tls` de `scripts/bundle_smoke.py` construye además `tls_context()` y comprueba sus propiedades (prueba 1) dentro del ejecutable. Esto demuestra que `truststore` y sus módulos de plataforma entran en el paquete.

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
- **F1b:** T6 usa `tls_context()` en los transportes propios de LiteLLM (condición 15), en vez de construir un `truststore.SSLContext` por llamada (skill `capa-llm`).
- **F2:** el rastreador hereda el mismo contexto.
- **Dependencias:**
  - `truststore` pasa a ser dependencia del núcleo de red, no solo de la capa de IA;
  - `certifi` se queda (dependencia de httpx y respaldo);
  - `trustme` entra en el grupo `dev`, con `cryptography` como dependencia transitiva y solo para pruebas; la cubre el trabajo `audit`.
