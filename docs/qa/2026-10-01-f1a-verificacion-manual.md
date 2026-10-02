# F1a — Lista de verificación manual

- **Fecha:** 2026-10-01
- **Autor:** qa-pruebas (T12)
- **Especificación:** [`docs/specs/2026-09-29-f1a-conexion-wordpress.md`](../specs/2026-09-29-f1a-conexion-wordpress.md), §10
- **Para quién:** la persona que cierra F1a. Estos pasos necesitan ver la ventana, el Administrador de credenciales de Windows y un WordPress real en wp-env, por eso no los hace un agente.
- **Máquina:** Windows 11, `npm run dev` (build de depuración) y wp-env según la sección "Probar con un WordPress local (wp-env)" del `README.md`.

Marca cada paso solo si el resultado coincide con lo esperado. Si algo no coincide, anota qué viste, la hora y el trozo de `faro.<fecha>.log` correspondiente (sin códigos, tokens ni llaves).

**Leyenda de la columna Resultado**

| Marca | Significado |
| --- | --- |
| ✅ verificado por el usuario el 2026-10-01 | El usuario ejecutó ese punto en Windows 11 con wp-env y `npm run dev` y confirmó que funciona. |
| ⏳ pendiente | Todavía nadie lo ha comprobado a mano. No hay resultado. |
| ❌ | Se comprobó y no coincide (anotar en Notas). |

## Antes de empezar

- Docker Desktop abierto (WSL 2) y `docker run --rm hello-world` responde.
- `npm run setup` ejecutado al menos una vez.
- `.env.local` en la raíz con `FARO_ALLOW_LOCAL_SITES=1` (salvo en el paso 11).
- **Copia de seguridad antes del paso 14** (borra la llave de la base): Administrador de credenciales → **Credenciales de Windows** → **Hacer copia de seguridad de credenciales**, y copia la carpeta `%APPDATA%\app.faro.desktop\profiles\` a otro sitio. El paso 14 actúa sobre tus datos reales de Faro.

**Dónde mirar**

| Qué | Dónde |
| --- | --- |
| Base cifrada del perfil | `%APPDATA%\app.faro.desktop\profiles\<uuid>.db` |
| Logs de la app | `%LOCALAPPDATA%\app.faro.desktop\logs\faro.<AAAA-MM-DD>.log` (una línea JSON por evento) |
| Credenciales | Panel de control → Administrador de credenciales → **Credenciales de Windows** → Credenciales genéricas, o `cmdkey /list \| Select-String faro` |
| WordPress | http://localhost:8888/wp-admin (`admin` / `password`) → **Ajustes → Faro** |
| WP-CLI de wp-env | desde `packages/wp-plugin`: `npx --no-install wp-env run cli wp <comando>` |

Nombres de las credenciales (servicio `app.faro.desktop`): `db/<uuid-del-perfil>/key.app.faro.desktop` (llave de la base) y `wp/<uuid-del-sitio>/token.app.faro.desktop` (conexión con el sitio).

---

## Pasos

| # | Paso | Resultado esperado | Resultado | Notas |
| --- | --- | --- | --- | --- |
| 1a | `npm run setup` | Termina sin errores y crea `packages/wp-plugin/dist/faro-wordpress.zip`. | ⏳ pendiente | |
| 1b | `npm run dev` | Se abre la ventana **Faro** y la tarjeta del motor pasa a **Motor conectado**. | ✅ verificado por el usuario el 2026-10-01 | Confirmado al ejecutar el flujo con `npm run dev`. |
| 1c | Mirar el `.db` del perfil: `$db = Get-ChildItem "$env:APPDATA\app.faro.desktop\profiles\*.db" \| Select-Object -First 1; [Text.Encoding]::ASCII.GetString((Get-Content $db.FullName -Encoding Byte -TotalCount 15))` | Hay un `.db` y el texto **no** es `SQLite format 3` (está cifrado). | ⏳ pendiente | |
| 1d | Administrador de credenciales | Aparece `db/<uuid>/key.app.faro.desktop`, con el mismo `<uuid>` que el `.db`. | ⏳ pendiente | |
| 2a | `npx --no-install wp-env start` en `packages/wp-plugin`, con `FARO_ALLOW_LOCAL_SITES=1` en `.env.local` | wp-env arranca; http://localhost:8888 responde. | ⏳ pendiente | Implícito en el paso 4 (sin esto no se puede conectar `http://localhost:8888`), pero no se registró aparte. |
| 2b | Configuración → **Sitios conectados** sin sitios | Estado vacío: **Conecta tu sitio de WordPress**, la frase "Faro leerá tus páginas, entradas y productos…" y el botón **Conectar tu sitio**. | ⏳ pendiente | |
| 3a | Asistente → paso 1 → **Guardar el plugin en Descargas** | Texto "Guardamos faro-wordpress.zip en tu carpeta Descargas." y se abre el Explorador con `faro-wordpress.zip` seleccionado. | ✅ verificado por el usuario el 2026-10-01 | |
| 3b | Instalar ese zip en un WordPress **limpio** de wp-env (`npx --no-install wp-env destroy` + `start`, o un segundo entorno) con **Plugins → Añadir nuevo → Subir plugin** → **Instalar ahora** → **Activar** | Se instala y activa sin errores; aparece **Ajustes → Faro**. | ⏳ pendiente | |
| 4a | wp-admin → **Ajustes → Faro** → **Generar código de conexión**; en Faro, **Conectar tu sitio** con `http://localhost:8888` y el código | Toast "Tu sitio … está conectado." y la tarjeta queda **Conectado**. | ✅ verificado por el usuario el 2026-10-01 | |
| 4b | Comparar los conteos de la tarjeta con wp-admin (Páginas, Entradas y Productos publicados) | Coinciden. | ⏳ pendiente | |
| 4c | Administrador de credenciales | Aparece `wp/<uuid>/token.app.faro.desktop`. | ⏳ pendiente | |
| 5a | Código ya usado. **Hazlo en el paso 7b**, en **Volver a conectar**, antes de generar el código nuevo: escribe el código del paso 4a | "Este código ya no sirve: caducó o ya se usó. Genera uno nuevo en WordPress." bajo el campo del código. | ⏳ pendiente | Con el sitio todavía conectado, **Conectar otro sitio** con la misma dirección responde antes "Este sitio ya está en Faro." (el motor comprueba la dirección antes de hablar con el plugin, spec §4.2 `connect` paso 2). Por eso se prueba en **Volver a conectar**. |
| 5b | Código incorrecto. También en **Volver a conectar** (paso 7b): genera un código, escribe otro de 6 números | "El código no coincide. Revísalo en WordPress (Ajustes → Faro). Te quedan 4 intentos." Luego el código correcto sigue sirviendo. | ⏳ pendiente | Mismo motivo que 5a. |
| 6a | Tarjeta → **Ver contenido**: pestañas **Páginas**, **Entradas** y **Productos** | Se ven las tablas con título, dirección y fecha. | ✅ verificado por el usuario el 2026-10-01 | |
| 6b | Comparar cada pestaña con wp-admin (solo publicados), incluida la paginación si hay más de 50 | Coinciden elemento a elemento. | ⏳ pendiente | |
| 6c | WooCommerce → Ajustes → Avanzado → Funciones → cambiar **Almacenamiento de datos de pedidos** (HPOS ↔ tablas de entradas) y volver a **Ver contenido → Productos** y **Comprobar conexión**, en los dos modos | Sigue funcionando en ambos. | ⏳ pendiente | |
| 7a | wp-admin → **Ajustes → Faro** → **Desconectar de Faro**. Cierra Faro y vuelve a abrirlo (`npm run dev`) para tener una sesión nueva; abre **Sitios conectados** | La comprobación automática deja la tarjeta en **Desconectado** con "Tu sitio se desconectó de Faro desde WordPress…". | ⏳ pendiente | |
| 7b | **Volver a conectar** (antes, haz 5a y 5b) con un código nuevo | El asistente abre en el paso 2 con la dirección fija; tras conectar, la tarjeta vuelve a **Conectado**. | ⏳ pendiente | |
| 8 | Cambiar las salts: `npx --no-install wp-env run cli wp config shuffle-salts` → en Faro, **Comprobar conexión** | Tarjeta **Desconectado** con "La conexión dejó de funcionar porque cambiaron las claves de seguridad de WordPress…"; **Ajustes → Faro** en wp-admin muestra la conexión rota. | ⏳ pendiente | Cambiar las salts cierra la sesión de wp-admin: vuelve a entrar. Después, **Volver a conectar** con un código nuevo para seguir. |
| 9a | `npx --no-install wp-env stop` → **Comprobar conexión** | Toast "No pudimos conectar con tu sitio…" y la tarjeta sigue como estaba. | ⏳ pendiente | |
| 9b | Con wp-env detenido, cerrar y abrir Faro (sesión nueva) y abrir **Sitios conectados** | La comprobación automática deja la tarjeta en **Sin comprobar** con el mensaje, sin toasts. | ⏳ pendiente | |
| 10 | Con wp-env detenido: **Más acciones → Desconectar sitio** → confirmar | Toast "Quitamos … de Faro, pero no pudimos avisar a tu sitio. Para terminar, entra a WordPress → Ajustes → Faro y pulsa Desconectar."; la credencial `wp/…/token.app.faro.desktop` desaparece. | ⏳ pendiente | |
| 10b | (Fuera de §10) Con wp-env arrancado: **Más acciones → Desconectar sitio** → confirmar | Toast "Desconectamos …."; la tarjeta desaparece. | ✅ verificado por el usuario el 2026-10-01 | El usuario confirmó "desconectar" con el sitio en marcha. La variante con wp-env detenido (paso 10) sigue pendiente. |
| 11 | `FARO_ALLOW_LOCAL_SITES=0` en `.env.local`, reiniciar `npm run dev` y conectar `http://localhost:8888` | "Esa dirección apunta a esta computadora o a tu red local. Faro solo se conecta a sitios publicados en internet." bajo la dirección. | ⏳ pendiente | Vuelve a poner `1` para el resto. |
| 12 | Buscar secretos en el log (comando abajo) | Sin coincidencias del código, el token, el secreto HMAC ni la llave. | ⏳ pendiente | |
| 13 | Inicio → **Qué hacer ahora** con un sitio **Conectado** | "Conecta tu sitio" aparece como hecho. | ⏳ pendiente | |
| 14 | **Haz antes la copia de seguridad.** Cierra Faro, borra `db/<uuid>/key.app.faro.desktop` en el Administrador de credenciales (con el `.db` en su sitio) y abre Faro | Aviso de `db.key_missing` ("No encontramos la llave de tus datos…") en la tarjeta del motor de Inicio y en **Sitios conectados** (sin botón de reintentar). **No** aparece una credencial `db/…/key` nueva. Después, **Restaurar credenciales** desde la copia y reabrir Faro: todo vuelve a la normalidad. | ⏳ pendiente | |
| 15 | (Fuera de §10) Modo claro y modo oscuro de Windows en Sitios conectados, asistente y Ver contenido | Todo se lee bien en ambos modos y la app cambia sin reiniciar. | ✅ verificado por el usuario el 2026-10-01 | |

### Comando del paso 12

Sustituye `123456` por los códigos que usaste en la sesión. El token y el secreto HMAC solo viven en el llavero, así que se buscan por su forma (43 caracteres base64url) y por nombre; la llave de la base, por su forma (64 hexadecimales):

```powershell
$log = "$env:LOCALAPPDATA\app.faro.desktop\logs\faro.*.log"
Select-String -Path $log -Pattern '"123456"', 'X-Faro-Token', 'X-Faro-Signature', 'hmac_secret"\s*:\s*"[A-Za-z0-9_-]', '"token"\s*:\s*"[A-Za-z0-9_-]'
Select-String -Path $log -Pattern '(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])', '(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])'
```

**Esperado:** sin resultados, o solo líneas donde el valor aparece como `[redactado]`. Si el segundo comando muestra algo, revisa que no sea un identificador conocido (los UUID tienen guiones cada pocos caracteres y no miden 43).

## Cobertura automática relacionada

Lo que estos pasos verifican a mano ya tiene pruebas automáticas (spec §9); la lista manual comprueba el sistema completo en Windows con el llavero real, la ventana y WordPress real:

- Flujo motor ↔ plugin contra wp-env: `apps/engine/tests/integration/test_wp_env.py` (CI `wp-plugin-integration`).
- Plugin (vinculación, firma, contenido, HPOS, salts, `uninstall.php`): `packages/wp-plugin/tests/` (PHPUnit en CI).
- Interfaz (estados, asistente, Ver contenido, quitar, Inicio): `apps/desktop/src/features/sites/*.test.tsx` y `apps/desktop/src/pages/HomePage.test.tsx`.
- Núcleo (perfil y llave, `db.key_missing` sin generar llave, concesiones, logs sin secretos): `apps/desktop/src-tauri/src/profile/tests.rs`, `src/secrets/tests.rs`, `src/engine/supervisor_tests.rs`.

## Cierre

- [ ] Todos los pasos de §10 en ✅ (o ❌ con su incidencia abierta).
- [ ] `FARO_ALLOW_LOCAL_SITES=0` en `.env.local` y wp-env detenido.
- Firma y fecha: ______________
