# F0 — Lista de verificación manual

- **Fecha:** 2026-09-29
- **Autor:** qa-pruebas (T11)
- **Especificación:** [`docs/specs/2026-09-28-f0-esqueleto.md`](../specs/2026-09-28-f0-esqueleto.md), §10.5
- **Para quién:** la persona que cierra F0. Estos pasos necesitan ver la ventana o usar una clave real, por eso no los hace un agente.

Hazla completa **dos veces**: una en **Windows 11** y otra en **Windows 10 22H2** (máquina física o VM). Marca cada casilla solo si el resultado coincide con lo esperado. Si algo no coincide, anota qué viste, la hora y adjunta el trozo de `faro.log` correspondiente (sin claves).

## Antes de empezar

- [ ] Máquina: ☐ Windows 11 ☐ Windows 10 22H2 — versión exacta (`winver`): ______________
- [ ] Windows 10: ¿hubo que instalar WebView2? ☐ Sí ☐ No. Cómo comprobarlo: `Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' | Select-Object pv` (si falla, falta WebView2).
- [ ] Ten a mano una clave **real** de **un** proveedor (Anthropic, OpenAI o Gemini de Google AI Studio). Úsala solo en la app; nunca la pegues en la terminal, en un archivo ni en un chat.
- [ ] Ten a mano una clave **inventada** con forma válida, por ejemplo `test-key-000000000000000000001a2B`.
- [ ] No debe existir `.env.local` en la raíz del repositorio (`Test-Path .env.local` → `False`), salvo en el paso 12.

**Dónde mirar**

| Qué | Dónde |
| --- | --- |
| Logs de la app | `%LOCALAPPDATA%\app.faro.desktop\logs\faro.<fecha>.log` (una línea JSON por evento) |
| Credenciales | Panel de control → Administrador de credenciales → **Credenciales de Windows** → Credenciales genéricas. También `cmdkey /list` en PowerShell |
| Procesos | Administrador de tareas → Detalles (columna "Línea de comandos"), o `Get-CimInstance Win32_Process -Filter "Name='python.exe'" \| Select ProcessId, CommandLine` |

Nombre de la credencial en Windows: `llm/<proveedor>/default.app.faro.desktop` (por ejemplo `llm/openai/default.app.faro.desktop`). El servicio es siempre `app.faro.desktop`.

Para buscar una clave en el log sin escribirla en la terminal, busca sus **últimos 4 caracteres** junto con los 4 anteriores, o simplemente comprueba que no hay líneas con `sk-`, `AIza` ni `Bearer`:

```powershell
Select-String -Path "$env:LOCALAPPDATA\app.faro.desktop\logs\faro.*.log" -Pattern 'sk-|AIza|Bearer|Authorization'
```

El resultado esperado es **vacío**.

---

## 1. Instalación desde cero siguiendo solo el README

- [ ] En una copia limpia del repositorio, sigue únicamente el README (sección "Instalación en Windows" y "Primer arranque").
- [ ] `npm run setup` termina sin errores.
- [ ] `npm run dev` abre una ventana titulada **Faro**.

**Esperado:** no hace falta ningún paso que no esté en el README. Anota cualquier paso extra: ______________

## 2. Inicio y motor (< 5 s)

- [ ] Al abrir, Inicio muestra primero **"Encendiendo el motor de Faro…"** con un esqueleto de tarjeta.
- [ ] En menos de 5 segundos pasa a **"Motor conectado"** con un icono de check verde.
- [ ] Al pasar el ratón por el icono de información del título aparece: **"El motor es la parte de Faro que hace el trabajo en tu computadora."**
- [ ] La versión del motor (`Versión 0.1.0`) solo aparece en un tooltip, no a la vista.
- [ ] Debajo, **"Qué hacer ahora"**: si no hay claves, la bienvenida **"Te damos la bienvenida a Faro"** con la frase "Aquí verás cada día qué hacer para atraer más clientes a tu sitio." y el botón **Agregar clave de IA**, que lleva a Configuración.
- [ ] Con al menos una clave guardada, en su lugar se lee: **"Todo listo por ahora. Pronto podrás conectar tu sitio desde aquí."**

**Dónde mirar:** en `faro.log`, la línea `"message":"estado del motor","state":"Ready"` debe estar a menos de 5 s de la primera línea de la sesión.

## 3. Navegación, textos y tema

- [ ] La barra lateral muestra, en este orden: Inicio, Bandeja, Investigación, Contenido, Auditoría, Anuncios, Agentes, Configuración.
- [ ] Con **Tab** se recorre la barra y con **Enter** se abre cada sección; el foco se ve claramente.
- [ ] El botón de contraer deja la barra solo con iconos; al pasar el ratón o enfocar cada icono aparece su nombre en un tooltip. Al cerrar y abrir Faro, recuerda si estaba contraída.
- [ ] Cada sección sin funcionalidad muestra su título, su frase y **"Esta sección estará disponible en una próxima versión."** (textos de la spec §3.3). Ningún texto en inglés, ninguna clave del tipo `inbox:emptyState.title`.
- [ ] Cambia Windows a tema oscuro (Configuración → Personalización → Colores) y vuelve a Faro: la app cambia a oscuro sin reiniciar y todo se lee bien. Vuelve a claro.
- [ ] Configuración → **Claves de IA**: el tooltip del título dice **"También se conocen como claves de API."**; sin claves aparece **"Agrega tu primera clave de IA"** y tres filas (Anthropic (Claude), OpenAI (ChatGPT), Google Gemini) en **"Sin conectar"** con **Agregar clave**.

## 4. Diálogos y avisos bajo la política de seguridad (CSP)

- [ ] Abre **Agregar clave** de cualquier proveedor: el diálogo aparece centrado, con fondo oscurecido, bordes redondeados y la tipografía Inter (no la de sistema). El campo **"Pega tu clave"** oculta lo escrito y no tiene botón "mostrar".
- [ ] El texto de ayuda coincide con el proveedor. En Gemini dice exactamente: **"Encuéntrala en Google AI Studio, en la sección de claves de API."**
- [ ] La clave enmascarada de una fila (`••••••••xxxx`) usa la fuente monoespaciada JetBrains Mono.
- [ ] Los avisos emergentes (toasts) de los pasos siguientes tienen estilo (fondo, borde, icono), no texto suelto sin formato.
- [ ] Abre las herramientas de desarrollo del webview (clic derecho → Inspeccionar, solo en `npm run dev`) → pestaña **Console**: no hay errores `Refused to load` / `Content Security Policy` ni fuentes cargadas desde internet.

## 5. Agregar una clave real

- [ ] **Agregar clave** del proveedor de tu clave real → pégala → **Guardar y probar**. El botón muestra "Probando la clave…".
- [ ] El diálogo se cierra, aparece el aviso **"Clave de <Proveedor> guardada y funcionando."** y la fila queda en **"Conectada"** con los últimos 4 caracteres.
- [ ] En el Administrador de credenciales aparece `llm/<proveedor>/default.app.faro.desktop` (o `cmdkey /list | Select-String faro`).
- [ ] La clave **no** aparece en `faro.log` (comando de búsqueda de arriba → vacío).
- [ ] **Probar clave** → "Probando…" → aviso **"La clave funciona."**

## 6. Agregar una clave inventada

- [ ] En **otro** proveedor (sin clave), **Agregar clave** → pega la clave inventada → **Guardar y probar**.
- [ ] El diálogo **sigue abierto** y debajo del campo aparece: **"El proveedor rechazó esta clave. Revisa que esté completa y activa."**
- [ ] Al cerrar el diálogo la fila sigue en **"Sin conectar"** y en el Administrador de credenciales **no** aparece ninguna entrada nueva.
- [ ] Pega un texto corto como `abc` → mensaje **"Esa clave no tiene el formato esperado. Cópiala de nuevo desde la página del proveedor."**

## 7. Sin internet, probar una clave

- [ ] Desconecta la red (modo avión o desactiva el adaptador).
- [ ] En la fila de la clave real, **Probar clave** → aviso **"No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo."**
- [ ] La fila **sigue en "Conectada"** (una prueba que no pudo hacerse no cambia el estado).
- [ ] Vuelve a conectar la red.

## 8. Prueba automática al abrir la sección (una vez por sesión)

- [ ] Con la clave real guardada, cierra Faro y vuelve a abrirlo (`npm run dev`).
- [ ] Ve a Configuración → Claves de IA **sin pulsar nada**: la fila muestra "Probando…" un instante y pasa sola a **"Conectada"**. No aparece ningún aviso emergente.
- [ ] Ve a otra sección y vuelve: **no** se repite la prueba.
- [ ] En `faro.log`, en esta sesión hay **un solo** registro de prueba (`clave probada`) por proveedor, sin la clave.

## 9. Prueba automática sin internet

- [ ] Cierra Faro, desconecta la red, abre Faro y ve a Claves de IA.
- [ ] La fila queda en **"Sin probar"** con el mensaje **"No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo."** y **Probar clave** habilitado.
- [ ] Reconecta la red y pulsa **Probar clave**: pasa a **"Conectada"**.

## 10. Credencial con el servicio correcto

- [ ] En el Administrador de credenciales, la entrada se llama `llm/<proveedor>/default.app.faro.desktop` (servicio `app.faro.desktop`). No hay ninguna otra entrada con `faro` que no reconozcas.

## 11. Borrar la clave

- [ ] **Borrar clave** → aparece la confirmación con el texto **"Los agentes ya no podrán usar <Proveedor> hasta que agregues otra clave."** y un botón rojo **Borrar clave de <Proveedor>**.
- [ ] **Cancelar** no borra nada.
- [ ] Confirmar: aviso **"Clave de <Proveedor> borrada."**, la fila vuelve a **"Sin conectar"**.
- [ ] La entrada **desaparece** del Administrador de credenciales (`cmdkey /list | Select-String faro` → vacío).

## 12. Cerrar la ventana

- [ ] Cierra Faro con la X de la ventana.
- [ ] En 10 s como máximo no queda ningún `python.exe` con `faro_engine` en la línea de comandos (Administrador de tareas → Detalles).
- [ ] En `faro.log`, las últimas líneas son `motor apagado` y `Faro se cierra`.

## 13. Terminar el motor desde el Administrador de tareas

- [ ] Con Faro abierto en Inicio ("Motor conectado"), en el Administrador de tareas → Detalles, finaliza el `python.exe` cuyo comando incluye `faro_engine` (cualquiera de los dos si hay dos).
- [ ] Inicio muestra **"El motor no responde. Estamos intentando reconectarlo."** (sin botón) y en unos segundos vuelve a **"Motor conectado"**.

## 14. Modo externo del motor

- [ ] Sigue la sección "Modo de desarrollo del motor → Externo" del README: crea `.env.local`, genera el token con el comando de la plantilla, `npm run dev:engine` en una terminal y `npm run dev` en otra.
- [ ] Inicio llega a **"Motor conectado"**.
- [ ] Detén el motor externo (Ctrl+C en su terminal): en menos de ~1 min Inicio muestra **"No encontramos el motor de desarrollo en la dirección configurada."** con **Reintentar conexión**.
- [ ] Arranca otra vez `npm run dev:engine` y pulsa **Reintentar conexión** → **"Motor conectado"**.
- [ ] Cierra todo y **borra `.env.local`** (`Remove-Item .env.local`). Comprueba `Test-Path .env.local` → `False`.

## 15. Revisión final de logs

- [ ] `Select-String -Path "$env:LOCALAPPDATA\app.faro.desktop\logs\faro.*.log" -Pattern 'sk-|AIza|Bearer|Authorization'` → vacío.
- [ ] Ninguna línea del log contiene la clave real ni el token de `.env.local`.

---

## Resultado

| Sistema | Fecha | Persona | Resultado | Observaciones |
| --- | --- | --- | --- | --- |
| Windows 11 | 2026-09-29 | Usuario | ☒ Todo OK ☐ Con fallos | Aprobado. Un error visual encontrado y ya corregido (ver abajo). |
| Windows 10 22H2 | | | ☐ Todo OK ☐ Con fallos | Pendiente. WebView2 instalado a mano: ☐ Sí ☐ No |

**Fecha:** 2026-09-29

- **Windows 11: aprobado por el usuario.** Terminó la verificación manual completa; la app corre bien y de forma estable.
- **Error encontrado y corregido:** con la barra lateral contraída se veían formas oscuras detrás de los iconos. Causa: el `Slot` de Radix convertía en texto el `className` en forma de función del `NavLink`, así que las clases de la sección activa no se aplicaban bien. Arreglo: las clases de la sección activa se aplican con selectores sobre `aria-current`, y se añadieron pruebas que lo cubren. Registrado en la spec, §13 fila 14.
- **Windows 10 22H2: pendiente.**
- Nota: tras esta verificación el usuario cambió la paleta de colores ([ADR 0007](../adr/0007-paleta-grises-y-turquesa.md)). Cuando se aplique, repetir el paso 3 (tema claro y oscuro) en Windows 11 y hacer la verificación de Windows 10 22H2 ya con la paleta nueva.
