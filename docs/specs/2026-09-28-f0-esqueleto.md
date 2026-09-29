# F0 — Esqueleto del proyecto Faro

- **Fecha:** 2026-09-28
- **Autor:** arquitecto
- **Estado:** implementada (2026-09-29). Historial: aprobada (2026-09-28) → implementada (2026-09-29). Las diferencias entre este documento y el código están en §13 y lo que queda abierto en §14.
- **ADR relacionados:** [0001](../adr/0001-monorepo-y-herramientas.md), [0002](../adr/0002-formato-de-errores-entre-capas.md), [0003](../adr/0003-prueba-de-claves-en-el-nucleo.md), [0004](../adr/0004-arranque-del-motor-en-f0-y-desarrollo.md), [0005](../adr/0005-repositorio-publico-y-ci.md), [0006](../adr/0006-csp-endurecida.md), [0007](../adr/0007-paleta-grises-y-turquesa.md) (paleta, posterior a F0)
- **Verificación manual:** [docs/qa/2026-09-29-f0-verificacion-manual.md](../qa/2026-09-29-f0-verificacion-manual.md)

---

## 1. Objetivo

Tener una app de escritorio que abre, muestra las 8 secciones de Faro, confirma que su motor local está funcionando y permite guardar de forma segura una clave de IA, sobre un repositorio con CI que impide integrar código roto.

## 2. Alcance

### Entra en F0

1. **Monorepo** con `apps/desktop` (Tauri 2 + React + TS + Tailwind + shadcn/ui), `apps/engine` (Python 3.12 + uv + FastAPI), `apps/cloud` y `packages/wp-plugin` (solo README), `packages/shared` (contratos generados). Gestor JS: **npm workspaces** (ADR 0001).
2. **Ventana** con barra lateral de 8 secciones, enrutamiento, layout base, tema claro/oscuro según el sistema, i18n base (`es` completo, `en` y `pt-BR` con `[TODO] `).
3. **Estado vacío** de cada sección (textos en §3.3).
4. **Motor sidecar** con el protocolo de token de `tauri-sidecar-python`, en la parte que no depende del empaquetado (ver tabla siguiente).
5. **Inicio** muestra el estado del motor: conectando, conectado, reconectando, error con botón para reintentar.
6. **Bóveda v1** en Configuración: agregar, probar, reemplazar y borrar una clave de Anthropic, OpenAI o Gemini (solo Google AI Studio), guardada en el Administrador de credenciales de Windows. La prueba se hace en el núcleo Rust (ADR 0003). Ninguna clave se guarda sin una prueba exitosa, y las claves `untested` se prueban automáticamente una vez por sesión al abrir Configuración → Claves de IA (§3.4).
7. **Contratos**: exportar `openapi.json` del motor y generar `packages/shared/engine.d.ts` y `engine-operations.json` (`npm run contracts`), con verificación en CI.
8. **CI** en GitHub Actions sobre un repositorio **público** en plan gratuito, sin licencia open source ("Todos los derechos reservados", ADR 0005): lint, formato, typecheck y pruebas de TS, Rust y Python; auditoría de dependencias; sin firma ni release.

### Plataformas soportadas

- **Windows 11** y **Windows 10 22H2** (x64). Versiones anteriores de Windows 10 no se soportan.
- Windows 10 terminó su soporte el 14/10/2025; la ESU para consumidores dura hasta ~13/10/2026 (dos semanas después de esta spec), y desde entonces no recibe parches de seguridad. Por decisión del usuario se soporta igual; **la decisión se revisa en octubre de 2027**.
- GitHub no ofrece runners de Windows 10: la CI corre en `windows-latest` (Windows Server) y la verificación manual (§10.5) se hace en Windows 11 **y** en Windows 10 22H2 (máquina física o VM).

### Pendientes para la fase de release (anotados aquí para no perderlos)

- Empaquetado PyInstaller, `externalBin` e integridad SHA-256 (tabla siguiente).
- **Bootstrapper de WebView2 en el instalador**: WebView2 puede no venir instalado en Windows 10; el instalador debe usar el modo bootstrapper (`webviewInstallMode` de Tauri) para instalarlo si falta.

### Protocolo del sidecar: qué entra y qué se pospone

| Parte del protocolo | F0 | Nota |
| --- | --- | --- |
| Token aleatorio de 32 bytes por la primera línea de stdin | Sí | |
| Motor en `127.0.0.1`, puerto `0`, línea `ready` por stdout | Sí | stdout reservado al protocolo; logs por stderr |
| Espera de `ready` máx. 20 s → `engine.start_failed` | Sí | |
| `Authorization: Bearer` + comparación en tiempo constante (401) | Sí | También en `/health` (ADR 0004) |
| Validación de `Host` = `127.0.0.1:<port>` (403) | Sí | |
| `GET /health` cada 15 s, 3 fallos → reinicio, máx. 3 reinicios en 10 min | Sí | |
| Apagado limpio con `{"event":"shutdown"}`, 10 s, luego matar | Sí | El motor también sale si stdin se cierra (EOF), salvo en modo `--dev`, que se detiene con Ctrl+C (ADR 0004) |
| Modo desarrollo `FARO_ENGINE_DEV_URL` (solo `debug_assertions`) | Sí | Modo "externo" (ADR 0004) |
| Lanzamiento en debug desde `apps/engine/.venv` | Sí | Modo "gestionado" en desarrollo (ADR 0004) |
| Empaquetado PyInstaller `--onedir` + `externalBin` | **No** → fase de release | |
| Integridad SHA-256 del binario (`engine.integrity_failed`) | **No** → fase de release | El código de error queda reservado |
| Canal `secret_request` / `secret_response` | **No** → F1 (primera tarea que use un LLM) | En F0 el motor no usa claves |
| Llave de SQLCipher por stdin, SQLite, migraciones | **No** → F1 | F0 no tiene base de datos |
| Eventos SSE / `engine://run/{run_id}` | **No** → F1 | |
| Comando `engine_call` con lista permitida | **No** → F1 | En F0 ninguna pantalla consulta datos del motor; sí se genera `engine-operations.json` |

### Queda fuera de F0 (explícito)

- Onboarding, selector de sitio, paleta de comandos (Ctrl+K), perfil y botón **Pausar agentes** de la barra superior: no hay agentes ni sitios que controlar todavía. Se agregan en la primera fase con agentes.
- Acción principal en los estados vacíos de secciones sin funcionalidad (excepción temporal a `sistema-diseno-faro`; ver §3.3).
- Varias claves por proveedor, alias editables, historial de uso de claves (`last_used_at`).
- Comprobar el saldo de la clave (listar modelos no lo verifica; ver ADR 0003).
- Guardar claves sin conexión a internet o sin prueba exitosa (no hay "Guardar sin probar").
- Gemini mediante Vertex AI (cuentas de servicio u OAuth): solo claves de API de Google AI Studio.
- Windows anteriores a Windows 10 22H2.
- Modo experto, bandeja del sistema, inicio con Windows, notificaciones, updater, licencias.
- macOS (el código se escribe portable, pero CI y pruebas manuales solo en Windows).
- Pruebas extremo a extremo automáticas (WebdriverIO + tauri-driver) → F1. En F0 hay lista de verificación manual (§10.5).
- Cobertura mínima obligatoria para Rust (se mide desde F1; en F0 se exige para TS y Python).
- `apps/cloud` y `packages/wp-plugin` con código.

---

## 3. Experiencia

> **Colores (cambio posterior a F0):** la paleta cambió después de implementar F0 por decisión del usuario del 2026-09-29 ([ADR 0007](../adr/0007-paleta-grises-y-turquesa.md)): grises neutros, turquesa como único color de acción, azul claro para la IA, sin morado ni violeta y nada más oscuro que `#333333`. Donde esta sección nombra tokens (`primary`, `success`, `warning`, `critical`, neutral), sus valores son los de la skill `sistema-diseno-faro`.

### 3.1 Ventana y navegación

- Ventana `main`, título "Faro", tamaño inicial 1280×800, mínimo 1100×700.
- Barra lateral fija de 240 px, colapsable a 64 px (solo iconos con tooltip). Orden, icono lucide y ruta:

| Sección | Icono (lucide) | Ruta | Namespace i18n |
| --- | --- | --- | --- |
| Inicio | `house` | `/` | `home` |
| Bandeja | `inbox` | `/inbox` | `inbox` |
| Investigación | `search` | `/research` | `research` |
| Contenido | `file-text` | `/content` | `content` |
| Auditoría | `stethoscope` | `/audit` | `audit` |
| Anuncios | `megaphone` | `/ads` | `ads` |
| Agentes | `bot` | `/agents` | `agents` |
| Configuración | `settings` | `/settings` | `settings` |

- Sección activa con fondo `primary` suave y `aria-current="page"`. Navegable con teclado (Tab, Enter) y foco visible.
- Barra superior en F0: solo el nombre de la sección actual.
- Contenido con ancho máximo 1280 px y márgenes de 32 px.

### 3.2 Inicio

Arriba, la tarjeta de estado del motor. Tooltip del título: "El motor es la parte de Faro que hace el trabajo en tu computadora."

| Estado | Qué ve el usuario |
| --- | --- |
| Conectando (`starting`) | Esqueleto de la tarjeta + "Encendiendo el motor de Faro…" |
| Conectado (`ready`) | Icono de check `success` + "**Motor conectado**". La versión va en tooltip. |
| Reconectando (`restarting`) | Icono `warning` + "El motor no responde. Estamos intentando reconectarlo." (sin botón) |
| Error (`error`) | Icono `critical` + mensaje traducido desde el `code` (§5.4) + botón **Reintentar conexión** |

Debajo, "Qué hacer ahora":
- Sin claves en la Bóveda → estado vacío de bienvenida (tabla §3.3) con la acción **Agregar clave de IA** (lleva a Configuración).
- Con al menos una clave → "Todo listo por ahora. Pronto podrás conectar tu sitio desde aquí."

### 3.3 Estados vacíos (texto `es`)

Formato: ilustración lineal simple (o icono de la sección a 48 px en F0), título, una frase de valor y, cuando exista algo que hacer, una acción. Las secciones sin funcionalidad agregan la línea común: "Esta sección estará disponible en una próxima versión." (`common:comingSoon`).

| Sección | Título | Frase | Acción F0 |
| --- | --- | --- | --- |
| Inicio | Te damos la bienvenida a Faro | Aquí verás cada día qué hacer para atraer más clientes a tu sitio. | **Agregar clave de IA** |
| Bandeja | Nada por aprobar | Antes de publicar o gastar dinero, los agentes te pedirán permiso aquí. | — |
| Investigación | Descubre qué busca tu cliente | Encuentra sobre qué escribir para atraer compradores a tu tienda. | — |
| Contenido | Aún no tienes contenido planeado | Aquí prepararás artículos y descripciones de productos con ayuda de la IA. | — |
| Auditoría | Revisa la salud de tu sitio | Encuentra páginas que no existen, títulos repetidos y otros problemas que te quitan visitas. | — |
| Anuncios | Aún no tienes campañas | Aquí prepararás anuncios de Google. Siempre se crean en pausa para que los revises. | — |
| Agentes | Conoce a tus agentes | Investigan, revisan y escriben por ti. Tú decides qué se publica. | — |
| Configuración (Bóveda sin claves) | Agrega tu primera clave de IA | Los agentes la usan para escribir y analizar. Se guarda en el llavero de tu computadora. | **Agregar clave** |

Claves i18n: `<namespace>:emptyState.title`, `<namespace>:emptyState.description`, `<namespace>:emptyState.action`.

### 3.4 Configuración → "Claves de IA" (Bóveda v1)

Tooltip del título de la sección: "También se conocen como claves de API."

Se muestran siempre tres filas, una por proveedor:

| Proveedor (texto visible) | `provider` |
| --- | --- |
| Anthropic (Claude) | `anthropic` |
| OpenAI (ChatGPT) | `openai` |
| Google Gemini | `gemini` |

Cada fila: nombre, chip de estado (texto + icono, nunca solo color), clave enmascarada en JetBrains Mono (`••••••••1a2B`) y acciones.

| Estado de la fila | Chip | Acciones |
| --- | --- | --- |
| Sin clave | "Sin conectar" (neutral) | **Agregar clave** |
| `valid` | "Conectada" (`success`) | **Probar clave**, **Reemplazar clave**, **Borrar clave** |
| `invalid` | "Falla" (`critical`) + mensaje del error | **Reemplazar clave**, **Probar clave**, **Borrar clave** |
| Probando (solo en la interfaz, durante la prueba automática o manual) | "Probando…" (neutral, con indicador de carga) | Todas deshabilitadas |
| `untested` (tras reiniciar la app, antes de la prueba automática o si esta no pudo ejecutarse) | "Sin probar" (neutral) + mensaje del error si la prueba automática falló | **Probar clave**, **Reemplazar clave**, **Borrar clave** |

Si no hay ninguna clave, arriba de las filas aparece el estado vacío de Configuración (§3.3).

**Errores siempre visibles**: cualquier fallo al guardar, probar (manual o automáticamente), borrar o listar muestra al usuario un mensaje claro traducido desde el `code` con el catálogo de §5.4; nunca un fallo silencioso ni un texto técnico. Un `code` que no está en el catálogo muestra el `message` del backend (español, sin datos sensibles) y, si viene vacío, el de `internal.unexpected` (orden del ADR 0002).

**Prueba automática al abrir la sección** (ADR 0003): para que toda clave esté siempre probada, al montar la sección "Claves de IA" la interfaz prueba automáticamente cada clave en estado `untested`, **una sola vez por sesión de la app** y por proveedor.
- Mientras se prueba, la fila muestra el chip "Probando…" (neutral, con indicador de carga) y sus botones quedan deshabilitados.
- Resultado `valid` → "Conectada". Resultado `invalid` → "Falla" con su mensaje traducido (`vault.invalid_key` o `vault.key_restricted`).
- Si la prueba no pudo ejecutarse (`vault.provider_unreachable`, `vault.provider_rate_limited`, `vault.provider_error`, `vault.keyring_unavailable` u otro error), la fila queda en "Sin probar" y debajo aparece el mensaje traducido del error, con **Probar clave** disponible. No se reintenta sola en esa sesión.
- Por eso el chip "Sin probar" solo se ve un instante (antes de que empiece la prueba) o cuando la prueba automática no pudo ejecutarse.
- No hay toasts por la prueba automática (el resultado se ve en la fila); los toasts quedan para las acciones que pulsa el usuario.
- Solo ocurre en Configuración → Claves de IA; Inicio no prueba claves.

**Diálogo "Agregar clave de Anthropic"** (igual para cada proveedor):
- Campo de tipo contraseña, etiqueta "Pega tu clave", `autocomplete="off"`, `spellcheck=false`, sin botón "mostrar".
- Ayuda (sin enlace en F0), clave i18n `settings:vault.help.<provider>`:
  - Anthropic: "Encuéntrala en la página de claves de tu cuenta de Anthropic."
  - OpenAI: "Encuéntrala en la página de claves de API de tu cuenta de OpenAI."
  - Gemini: "Encuéntrala en Google AI Studio, en la sección de claves de API." (solo claves de AI Studio; Vertex AI queda fuera).
- Botón principal **Guardar y probar** (con indicador de carga "Probando la clave…"); secundario **Cancelar**.
- Al terminar bien: el diálogo se cierra, el campo se vacía y aparece un toast "Clave de Anthropic guardada y funcionando."
- Si falla: el error va debajo del campo (traducido por `code`), el diálogo sigue abierto y la clave **no** se guarda.
- Si ya existe clave, el mismo diálogo se abre como "Reemplazar clave de Anthropic" y envía `replace: true`.

**Probar clave**: el botón muestra "Probando…"; resultado en toast: "La clave funciona." o el mensaje de `vault.invalid_key`.

**Borrar clave**: diálogo de confirmación con botón `destructive` **Borrar clave de Anthropic** y el texto "Los agentes ya no podrán usar Anthropic hasta que agregues otra clave."

**Cargando**: tres filas esqueleto. **Error al listar** (`vault.keyring_unavailable`): mensaje + botón **Intentar de nuevo**.

---

## 4. Diseño técnico por capa

### 4.1 Repositorio (devops-release)

Árbol propuesto (solo lo que crea F0):

```
C:\Faro_App\
├─ .github/workflows/ci.yml
├─ .github/dependabot.yml             # alertas y actualizaciones (npm, cargo, uv/pip, github-actions)
├─ .github/workflows/codeql.yml       # CodeQL (JS/TS, Python, Actions)
├─ .vscode/extensions.json            # extensiones recomendadas (§9)
├─ apps/
│  ├─ desktop/
│  │  ├─ package.json  index.html  vite.config.ts  vitest.config.ts
│  │  ├─ tsconfig.json  eslint.config.js  components.json
│  │  ├─ src/
│  │  │  ├─ main.tsx  App.tsx
│  │  │  ├─ app/            AppShell.tsx  Sidebar.tsx  routes.tsx  sections.ts
│  │  │  ├─ pages/          HomePage.tsx  InboxPage.tsx  ResearchPage.tsx  ContentPage.tsx
│  │  │  │                  AuditPage.tsx  AdsPage.tsx  AgentsPage.tsx  SettingsPage.tsx
│  │  │  ├─ features/engine/  EngineStatusCard.tsx  useEngineStatus.ts
│  │  │  ├─ features/vault/   VaultSection.tsx  ProviderKeyRow.tsx  KeyDialog.tsx
│  │  │  │                    DeleteKeyDialog.tsx  useVaultKeys.ts  useAutoTestKeys.ts
│  │  │  ├─ components/ui/    # shadcn/ui: button, dialog, input, tooltip, toast, skeleton, badge
│  │  │  ├─ components/faro/  EmptyState.tsx  ConnectionChip.tsx
│  │  │  ├─ lib/api/          invoke.ts  errors.ts  types.ts  engine.ts  vault.ts
│  │  │  ├─ lib/i18n.ts
│  │  │  ├─ locales/{es,en,pt-BR}/{common,home,inbox,research,content,audit,ads,agents,settings,errors}.json
│  │  │  ├─ styles/           tokens.css  globals.css
│  │  │  └─ test/setup.ts
│  │  └─ src-tauri/
│  │     ├─ Cargo.toml  Cargo.lock  build.rs  tauri.conf.json
│  │     ├─ capabilities/main.json
│  │     ├─ permissions/engine.toml  vault.toml
│  │     ├─ src/  main.rs  lib.rs  error.rs  state.rs  logging.rs
│  │     ├─ src/commands/  mod.rs  engine.rs  vault.rs
│  │     ├─ src/engine/    mod.rs  supervisor.rs  launcher.rs  protocol.rs  client.rs
│  │     ├─ src/vault/     mod.rs  store.rs  providers.rs  secret.rs
│  │     ├─ src/engine/supervisor_tests.rs   # pruebas del supervisor con launcher falso (§13)
│  │     └─ tests/         engine_real.rs (#[ignore])
│  ├─ engine/
│  │  ├─ pyproject.toml  uv.lock  .python-version  README.md
│  │  ├─ faro_engine/
│  │  │  ├─ __init__.py  __main__.py  export_openapi.py
│  │  │  └─ core/  app.py  config.py  security.py  errors.py  logging.py  protocol.py
│  │  │         routes/health.py   schemas/common.py
│  │  └─ tests/  conftest.py  test_security.py  test_protocol.py  routes/test_health.py
│  └─ cloud/README.md                 # "Nube mínima de Faro: se implementa en una fase posterior."
├─ packages/
│  ├─ shared/  package.json  openapi.json  engine.d.ts  engine-operations.json  README.md
│  └─ wp-plugin/README.md             # placeholder
├─ scripts/generate-contracts.mjs
├─ docs/specs/  docs/adr/
├─ package.json  package-lock.json     # workspaces: apps/desktop, packages/shared
├─ .node-version  rust-toolchain.toml
├─ .editorconfig  .gitattributes (eol=lf)  .gitignore  .prettierrc.json  .prettierignore
├─ .env.local.example                  # ver ADR 0004; .env.local está en .gitignore desde el primer commit
├─ LICENSE                             # "Todos los derechos reservados", sin licencia open source (ADR 0005)
└─ README.md                           # cómo instalar (§9) y comandos
```

**Scripts npm en la raíz** (todos los agentes y CI usan estos nombres):

| Script | Qué hace |
| --- | --- |
| `setup` | `npm ci` + `uv sync --directory apps/engine --locked` |
| `dev` | `npm run tauri dev -w apps/desktop` (el núcleo lanza el motor desde `.venv`, ADR 0004) |
| `dev:engine` | `uv run --directory apps/engine python -m faro_engine --dev` (modo externo) |
| `lint` | `lint:ts` (ESLint) + `lint:rs` (`cargo clippy --all-targets -- -D warnings`) + `lint:py` (`ruff check`) |
| `format` / `format:check` | Prettier + `cargo fmt` + `ruff format` (con `--check` en la variante de verificación) |
| `typecheck` | `tsc --noEmit` (desktop y shared) + `mypy` en el motor |
| `test` | `test:desktop` (vitest) + `test:core` (`cargo test`) + `test:engine` (`uv run --directory apps/engine pytest`) |
| `test:all` | Igual que `test` en F0 (se amplía con e2e en F1) |
| `contracts` | `node scripts/generate-contracts.mjs` |

**Herramientas por lenguaje** (ADR 0001):

| Lenguaje | Lint | Formato | Tipos | Pruebas |
| --- | --- | --- | --- | --- |
| TypeScript | ESLint 9 (flat) + typescript-eslint + react-hooks + jsx-a11y + `eslint-plugin-i18next` (`no-literal-string`) | Prettier 3 + `prettier-plugin-tailwindcss` | `tsc --noEmit`, `strict: true` | Vitest + Testing Library + jsdom, `mockIPC` de `@tauri-apps/api/mocks` |
| Rust | `cargo clippy -D warnings` | `rustfmt` | compilador | `cargo test` (+ `tokio::test`, `httpmock` o `wiremock`) |
| Python | `ruff check` | `ruff format` | `mypy --strict` + plugin de pydantic | pytest + pytest-asyncio + httpx (ASGI) + pytest-cov |

### 4.2 Interfaz (frontend-react)

- Vite + React + TS estricto + Tailwind v4 + shadcn/ui (componentes copiados en `components/ui`, personalizados con los tokens de `sistema-diseno-faro`).
- Fuentes Inter y JetBrains Mono **empaquetadas localmente** (`@fontsource/*`); la CSP no permite fuentes remotas.
- Enrutamiento: React Router 7 con `createHashRouter` (evita depender de cómo el protocolo de Tauri resuelve rutas profundas). Rutas de §3.1; ruta desconocida → Inicio.
- Layout: `AppShell` = `Sidebar` + barra superior + `<Outlet/>`. Estado de colapso de la barra en `localStorage` (no es sensible).
- i18n: `i18next` + `react-i18next`, recursos importados estáticamente (sin backend HTTP), idioma `es` fijo en F0, `fallbackLng: "es"`. Se agrega el namespace `home` a la lista de la skill.
- Tema: claro/oscuro según `prefers-color-scheme` (sin selector en F0).
- Colores: tokens de `sistema-diseno-faro` en `styles/tokens.css`. **La paleta cambió después de F0** ([ADR 0007](../adr/0007-paleta-grises-y-turquesa.md)): grises neutros, turquesa de acción, `ai` en azul claro (antes violeta), gris más oscuro `#333333` y contraste verificado por `tokens.test.ts`. Aplicarla en `tokens.css` y crear esa prueba queda pendiente (§14).
- Capa de API (`src/lib/api/`):
  - `invoke.ts`: envoltorio tipado de `invoke()` que convierte cualquier rechazo en la clase `FaroError` (`errors.ts`) construida a partir del objeto `FaroErrorData {code, message, details}` (`types.ts`); si el rechazo no tiene esa forma → `internal.unexpected` (ADR 0002).
  - `engine.ts`: `getEngineStatus()`, `restartEngine()`, `onEngineStatus(cb)` (escucha `engine://status`).
  - `vault.ts`: `listKeys()`, `addKey(input)`, `testKey(provider)`, `deleteKey(provider)`.
  - `types.ts`: tipos de §5.2 escritos a mano en F0 (generarlos desde Rust queda para después).
- Datos: TanStack Query para `listKeys` (`queryKey: ["vaultKeys"]`). **`addKey` no usa `useMutation` con la clave como variable** (quedaría en la caché de mutaciones): se llama a `addKey` directamente desde el manejador del formulario y luego se invalida `["vaultKeys"]`. Sin React Query Devtools en el build.
- `useAutoTestKeys` (prueba automática, §3.4 y ADR 0003): cuando `listKeys` se resuelve, por cada `KeySummary` con `status: "untested"` cuyo proveedor no esté aún en el conjunto de la sesión, lo agrega al conjunto **antes** de llamar y ejecuta `testKey(provider)`. Las llamadas se lanzan a la vez; el núcleo las serializa. Cada resultado actualiza su fila con `queryClient.setQueryData(["vaultKeys"], …)`; un error se guarda en el estado local de la fila (código para el mensaje) y la fila queda en "Sin probar". El conjunto de la sesión es un `Set<Provider>` a nivel de módulo (sobrevive a desmontar y volver a montar la sección; se pierde solo si el webview se recarga, lo que en uso normal equivale a una nueva sesión). Si `listKeys` falla, no se prueba nada. Mientras una fila está probando (automática o manualmente), sus acciones quedan deshabilitadas.
- La clave solo vive en el estado del campo del diálogo; se vacía al guardar con éxito y al cerrar el diálogo. Nunca en `console.*`, `localStorage` ni en mensajes de error.
- `useEngineStatus`: lee `getEngineStatus()` al montar y se suscribe al evento; se desuscribe al desmontar.

### 4.3 Núcleo Rust (tauri-rust)

**Configuración (`tauri.conf.json`)**
- `identifier`: **`app.faro.desktop`, definitivo**. **No debe cambiarse nunca**: es el nombre del servicio de las claves en el Administrador de credenciales y define la carpeta de datos y logs de la app; cambiarlo dejaría huérfanas las claves y los datos de todos los usuarios. Cualquier propuesta de cambio requiere ADR y plan de migración.
- Ventana `main` con los tamaños de §3.1. `withGlobalTauri: false`.
- `app.security.csp` exactamente la de `tauri-comandos-y-permisos`, endurecida en T12 (ADR 0006): `default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src ipc: http://ipc.localhost; base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'`. `freezePrototype: true`.
- `build.rs` declara explícitamente los comandos de la app con `tauri_build::AppManifest` para que el ACL no dependa de que existan los `.toml` de `permissions/` (hallazgo T12).
- Sin plugins de Tauri en F0 (no se necesita `shell`: en F0 el motor se lanza con `std::process`/`tokio::process` desde Rust, y en release se añadirá `tauri-plugin-shell` usado solo desde Rust).

**Estado (`state.rs`)**: `AppState { engine: EngineSupervisorHandle, vault: VaultService }`.

**Errores (`error.rs`)**: `AppError { code: &'static str, message: String, details: serde_json::Value }` con `Serialize` manual a `{code, message, details}` (ADR 0002). Constructores por código (`AppError::vault_invalid_key()`, etc.).

**Logs (`logging.rs`)**: `tracing` + `tracing-subscriber` en JSON a `<app_log_dir>` con rotación diaria (`tracing-appender`), 7 archivos. El archivo real se llama `faro.AAAA-MM-DD.log` (tracing-appender añade la fecha) y en Windows está en `%LOCALAPPDATA%\app.faro.desktop\logs\`. Donde este documento dice `faro.log` se refiere a esos archivos. Nunca se registran: token del motor, claves, cabeceras, cuerpos hacia proveedores, líneas crudas del stdout del motor.

**Supervisor del motor (`engine/`)**
- `launcher.rs`: trait `EngineLauncher` → devuelve un proceso con `stdin`, líneas de `stdout`, líneas de `stderr` y `wait()`. Implementaciones:
  - `DevVenvLauncher` (solo `cfg(debug_assertions)`): ejecuta `apps/engine/.venv/Scripts/python.exe -m faro_engine --host 127.0.0.1 --port 0 --data-dir <app_data_dir>` (ruta calculada desde `CARGO_MANIFEST_DIR`; en macOS `.venv/bin/python`). Si no existe `.venv` → `engine.start_failed` con `details.reason = "dev_env_missing"`.
  - `SidecarLauncher` (release): **fuera de F0**; en F0, un build de release sin sidecar muestra `engine.start_failed`.
  - Fake en pruebas (canales en memoria + servidor HTTP local de prueba).
- `protocol.rs`: tipos de las líneas JSON (`Ready {port, version, pid}`, `Shutdown`). Líneas de stdout que no sean JSON válido se ignoran y se registra solo "línea de protocolo no reconocida" (sin contenido).
- `supervisor.rs`, máquina de estados `starting → ready ↔ restarting → error`:
  1. Genera token de 32 bytes con el CSPRNG del SO, codificado base64url sin relleno (43 caracteres). Solo en memoria (`secrecy::SecretString`).
  2. Lanza el proceso, escribe `token + "\n"` en stdin y deja stdin abierto.
  3. Espera `ready` ≤ 20 s. Valida `port` en 1024–65535. Si no llega, el proceso termina o la línea es inválida → mata el proceso y pasa a `error` con `engine.start_failed` (`details.reason`: `timeout` | `exited` | `bad_ready` | `spawn` | `dev_env_missing`).
  4. Primer `GET /health` correcto → `ready` (guarda `version`).
  5. Cada 15 s `GET /health` (timeout 5 s). 3 fallos seguidos, o salida inesperada del proceso → `restarting` y relanza. Más de 3 reinicios en una ventana de 10 min → `error` con `engine.restart_limit`.
  6. `stderr` del motor se reenvía a `tracing` con nivel `debug` (el motor ya escribe JSON sin secretos).
  7. Cada cambio de estado emite el evento `engine://status` con `EngineStatus`.
  8. Apagado (`RunEvent::ExitRequested`/`Exit`): escribe `{"event":"shutdown"}\n`, espera la salida ≤ 10 s, si no, mata el proceso.
- Modo externo (ADR 0004, solo `debug_assertions`): si existe `FARO_ENGINE_DEV_URL`, no lanza nada; toma URL y `FARO_ENGINE_DEV_TOKEN` (leídos con `dotenvy` desde `.env.local` en la raíz del repo, solo en debug). La URL debe ser `http://127.0.0.1:<puerto>`; si no, `engine.dev_unreachable`. Health igual, sin reinicios: 3 fallos → `error` con `engine.dev_unreachable`.
- Job Object de Windows (crate `win32job`): el proceso del motor se asigna a un job con "matar al cerrar" para que no quede huérfano si el núcleo muere. La carrera entre el `spawn` y la asignación al job está mitigada en desarrollo; en release, el lanzador del sidecar debe crear el proceso suspendido, asignarlo al job y reanudarlo (§14).
- `client.rs`: `reqwest` (0.13) con `rustls`, sin proxy (`no_proxy()`), sin redirecciones, añade `Authorization: Bearer`. Nunca registra cabeceras.
- Tiempos (20 s, 15 s, 5 s, 3, 10 min, 10 s) en una estructura `SupervisorConfig` inyectable para pruebas.

**Bóveda (`vault/`)** — ADR 0003
- `store.rs`: trait `SecretStore { get(ref) -> Option<SecretString>, set(ref, &SecretString), delete(ref) }`.
  - `KeyringStore`: crate `keyring` v3 con feature `windows-native` (y `apple-native` para más adelante). `Entry::new(service = "app.faro.desktop", user = <secret_ref>)` (el identificador de la app, inmutable). En el Administrador de credenciales de Windows la entrada aparece como `llm/<provider>/default.app.faro.desktop` (formato `<user>.<service>` del crate keyring). Cualquier error de la plataforma → `vault.keyring_unavailable` (sin detalles de la plataforma en `message`).
  - `MemoryStore` para pruebas (`MockKeyring` de `pruebas-faro`).
- Referencia del secreto: `llm/<provider>/default` (`llm/anthropic/default`, `llm/openai/default`, `llm/gemini/default`). En F0 una clave por proveedor.
- `secret.rs`: `SecretString` (crate `secrecy`) con `zeroize`; `AddKeyInput` implementa `Debug` manual que muestra `secret: "[oculto]"`. `last4` = últimos 4 caracteres.
- Validación de formato (`vault.invalid_input`): se recortan espacios al inicio y final; longitud 20–512; solo ASCII imprimible sin espacios. No se exige prefijo (los formatos de los proveedores cambian).
- `providers.rs`: trait `ProviderChecker` con URL base inyectable (pruebas con servidor simulado). Cliente `reqwest` + `rustls`, timeout 10 s, sin redirecciones, `User-Agent: Faro/<versión>`, hosts fijos:

| Proveedor | Petición de prueba (gratuita, no consume tokens — verificar al implementar) |
| --- | --- |
| Anthropic | `GET https://api.anthropic.com/v1/models?limit=1` con `x-api-key` y `anthropic-version: 2023-06-01` |
| OpenAI | `GET https://api.openai.com/v1/models` con `Authorization: Bearer` |
| Gemini | `GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1` con cabecera `x-goog-api-key` (nunca la clave en la URL) |

  Mapeo de respuesta: 2xx → `valid`; 401 (y 400 de Gemini con `API_KEY_INVALID`) → `vault.invalid_key`; 403 → `vault.key_restricted`; 429 → `vault.provider_rate_limited`; 5xx u otra → `vault.provider_error`; error de red o timeout → `vault.provider_unreachable`. El cuerpo de la respuesta no se registra ni se devuelve.
- Estado de prueba (`valid`/`invalid` + `last_tested_at`) solo en memoria (`Mutex<HashMap<Provider, TestState>>`); tras reiniciar la app las claves aparecen como `untested` (y la interfaz las prueba automáticamente al abrir la sección, §3.4). Sin archivo índice: `vault_list_keys` consulta las 3 referencias fijas.
- `vault_test_key` actualiza el estado en memoria solo cuando el proveedor da un veredicto (`valid`, o `invalid` por 401/403). Si la prueba no pudo ejecutarse (red, 429, 5xx, llavero), devuelve el error y **no modifica** el estado guardado (una clave `untested` sigue `untested`; una `valid` sigue `valid`). `vault_add_key` con éxito deja el estado en `valid`; `vault_delete_key` elimina el estado.
- Un `tokio::sync::Mutex` serializa las operaciones de la Bóveda (incluidas varias `vault_test_key` simultáneas de la prueba automática; peor caso 3 × 10 s de timeout).

### 4.4 Motor Python (motor-python)

- `pyproject.toml`: proyecto `faro-engine`, `requires-python = ">=3.12,<3.13"`. Dependencias: `fastapi`, `uvicorn` (sin extras pesados), `pydantic` v2, `structlog`. Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`, `ruff`, `mypy`. Versiones fijadas con `uv.lock`.
- `__main__.py`:
  1. Argumentos `--host` (solo acepta `127.0.0.1`; otro valor → sale con código 2), `--port` (0 por defecto), `--data-dir` (se crea si no existe; en F0 no se usa más), `--dev`.
  2. Sin `--dev`: lee la primera línea de stdin con timeout de 10 s; si falta o no mide 43 caracteres base64url → sale con código 2 y registra "token ausente o inválido" (sin valor).
  3. Con `--dev`: rechaza arrancar si `sys.frozen` (build empaquetado); lee `FARO_ENGINE_DEV_TOKEN` y `FARO_ENGINE_DEV_PORT` (por defecto 8765) de `.env.local` de la raíz del repo.
  4. Crea el socket en `127.0.0.1:<port>`, obtiene el puerto real, escribe en stdout **una línea** `{"event":"ready","port":…,"version":…,"pid":…}` y hace `flush`. Nada más se escribe en stdout salvo eventos del protocolo.
  5. Arranca `uvicorn.Server(...).serve(sockets=[sock])` con `access_log=False`.
  6. Un hilo lee stdin: `{"event":"shutdown"}` o EOF → `server.should_exit = True`; si en 10 s no terminó, `os._exit(0)`. Con `--dev` el EOF no apaga el motor (se detiene con Ctrl+C; ADR 0004).
- `core/security.py`: dependencia/middleware global, en este orden:
  1. `Host` distinto de `127.0.0.1:<port>` → 403 `engine.forbidden_host`.
  2. `Authorization` ausente o token distinto (`secrets.compare_digest`) → 401 `engine.unauthorized`.
  Aplica a **todas** las rutas, incluida `/health` (ADR 0004). En modo `--dev` se deshabilitan `/docs`, `/redoc` y `/openapi.json` igual que en normal (el esquema se exporta con `export_openapi.py`, no por HTTP).
- `core/errors.py`: `FaroError(code, message, status, details={})`, modelo `ErrorOut {code, message, details}`, manejadores para `FaroError`, 404 (`engine.not_found`), 422 (`engine.invalid_request`) y excepciones no controladas (500 `internal.unexpected`, sin traza en la respuesta).
- `core/logging.py`: `structlog` JSON a **stderr**, con `request_id`. Prohibido registrar cabeceras.
- `routes/health.py`: `GET /health`, `operation_id="getHealth"`, respuesta `HealthOut {status: "ok", version: str}`.
- `export_openapi.py`: escribe el OpenAPI de la app a stdout (lo usa `npm run contracts`).

### 4.5 Contratos (devops-release + motor-python)

`scripts/generate-contracts.mjs`:
1. `uv run --directory apps/engine python -m faro_engine.export_openapi` → `packages/shared/openapi.json` (formateado de forma estable).
2. `openapi-typescript` → `packages/shared/engine.d.ts`.
3. Genera `packages/shared/engine-operations.json`: `[{ "operationId": "getHealth", "method": "GET", "path": "/health" }]`.
Los tres archivos se versionan; CI falla si `npm run contracts` produce diferencias.

### 4.6 Base de datos, nube y plugin

Sin cambios en F0 (solo README en `apps/cloud` y `packages/wp-plugin`).

### 4.7 CI (devops-release)

Repositorio **público** en GitHub, plan gratuito (ADR 0005). En repos públicos los minutos de Actions en runners estándar (incluido Windows) son gratis e ilimitados; los límites reales son la concurrencia del plan gratuito (≈20 trabajos simultáneos, ≈5 de macOS) y 6 h por trabajo (verificar al configurar). Por eso todos los trabajos, incluidos los de Windows, corren en cada PR y en cada push a `main`.

`.github/workflows/ci.yml`:
- **Disparadores**: `pull_request` (hacia `main`), `push` a `main` y `workflow_dispatch`. **Nunca `pull_request_target`** ni `workflow_run` con código del PR.
- **Permisos**: `permissions: contents: read` a nivel de workflow; ningún trabajo pide más. El workflow de CI no usa secretos.
- **Concurrencia**: `concurrency: { group: ci-${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true }`.
- **Acciones fijadas por SHA completo** (Dependabot las actualiza). `TZ=UTC` en todos los trabajos.
- **Cachés**: npm (`setup-node` con `cache: npm`), uv (`setup-uv` con `enable-cache: true`), Rust (`Swatinem/rust-cache` con `save-if: github.ref == 'refs/heads/main'`, para que la caché se genere en `main` y la reutilicen todos los PR sin llenar el límite de almacenamiento de cachés del repositorio).
- **`timeout-minutes` en cada trabajo** (valores en la tabla).
- **Sin filtros por rutas (`paths`)**: con minutos ilimitados no compensan el riesgo de que un filtro mal escrito salte un trabajo y la CI quede en verde sin validar. Se reconsideran solo si la duración de la CI llega a molestar.

| Trabajo | Runner | Timeout | Pasos |
| --- | --- | --- | --- |
| `ts` | `ubuntu-latest` | 15 min | `setup-node` (versión de `.node-version`, caché npm) → `npm ci` → `format:check` (Prettier) → `lint:ts` → `typecheck` (TS) → `test:desktop` con cobertura (umbral 70 %) |
| `engine` | matriz `ubuntu-latest` y `windows-latest` | 20 min | `astral-sh/setup-uv` (caché) → `uv sync --locked` → `ruff format --check` → `ruff check` → `mypy` → `pytest --cov --cov-fail-under=80` (95 % en `core/security.py` y `__main__.py`) |
| `core` | `windows-latest` | 45 min | `setup-node` + `npm ci` + `npm run build -w apps/desktop` (necesario para `generate_context!`) → toolchain de `rust-toolchain.toml` con `clippy` y `rustfmt` → `Swatinem/rust-cache` → `cargo fmt --check` → `cargo clippy --all-targets -- -D warnings` → `cargo test` → `setup-uv` + `uv sync --directory apps/engine --locked` → `cargo test -- --ignored engine_real` (arranque real del motor) |
| `contracts` | `ubuntu-latest` | 10 min | `npm ci` + `uv sync` → `npm run contracts` → `git diff --exit-code packages/shared` |
| `audit` | `ubuntu-latest` | 15 min | `npm audit --omit=dev --audit-level=high` → `cargo audit` (instalado con `taiki-e/install-action`; con archivo de ignorados que solo admite crates exclusivos de Linux, cada uno con su justificación) → `uvx pip-audit` sobre el lock exportado → `gitleaks detect` (historial completo: `fetch-depth: 0`) |
| `ci-ok` | `ubuntu-latest` | 5 min | `needs: [ts, engine, core, contracts, audit]`, `if: always()`. Falla si algún trabajo necesario terminó en `failure`, `cancelled` o `skipped`. Es el **único check requerido** en la protección de `main`: agregar o renombrar trabajos no obliga a tocar la configuración de la rama. |

`.github/workflows/codeql.yml`: CodeQL (configuración por defecto o avanzada) para JavaScript/TypeScript, Python y GitHub Actions, en `pull_request` y `push` a `main`, `permissions: contents: read, security-events: write` solo en ese workflow. Rust se añade cuando CodeQL lo soporte de forma estable (verificar al configurar).

`.github/dependabot.yml`: ecosistemas `npm` (raíz), `cargo` (`apps/desktop/src-tauri`), `uv`/`pip` (`apps/engine`, verificar soporte de `uv.lock` al configurar) y `github-actions`, frecuencia semanal.

**Configuración del repositorio** (la aplica el usuario desde Settings, o aprueba los comandos `gh` que prepare `devops-release`; ver §7 y ADR 0005):
1. Settings → Actions → General: aprobación obligatoria para ejecutar workflows de PR de forks de **todos los colaboradores externos** ("Require approval for all outside collaborators"); permisos por defecto del `GITHUB_TOKEN` en **solo lectura**; no permitir que Actions cree ni apruebe PR.
2. Advanced Security (gratis en repos públicos): **secret scanning** + **push protection**, **Dependabot alerts**, **CodeQL**.
3. Protección de `main` (regla de rama o ruleset): PR obligatorio, check `ci-ok` requerido y rama al día, sin force-push ni borrado.
4. **Nunca** registrar runners propios (self-hosted) en este repositorio.

Rust no corre en Ubuntu en F0: la app se valida donde se ejecuta (Windows) y en Linux requeriría dependencias de WebKitGTK. macOS se agrega con el release.

---

## 5. Contratos

### 5.1 Endpoints del motor

| Método | Ruta | `operationId` | Entrada | Salida | Errores |
| --- | --- | --- | --- | --- | --- |
| GET | `/health` | `getHealth` | — (Bearer) | `200 {"status":"ok","version":"0.1.0"}` | 401 `engine.unauthorized`, 403 `engine.forbidden_host` |

### 5.2 Comandos Tauri

Tipos (serde en `snake_case`; así llegan también a TS):

```ts
type Provider = "anthropic" | "openai" | "gemini";
type KeyStatus = "valid" | "invalid" | "untested";

interface KeySummary {
  provider: Provider;
  secret_ref: string;          // "llm/anthropic/default"
  last4: string;               // "1a2B"
  status: KeyStatus;
  last_tested_at: string | null; // ISO-8601 UTC
  last_error_code: string | null; // "vault.invalid_key" | "vault.key_restricted" cuando status = "invalid"
}

type EngineState = "starting" | "ready" | "restarting" | "error";

interface EngineStatus {
  state: EngineState;
  version: string | null;      // solo cuando ready
  error: FaroErrorData | null; // solo cuando error
}

// En TS el objeto que llega por IPC se llama FaroErrorData (types.ts);
// FaroError es la clase de errors.ts que lo envuelve (ADR 0002).
interface FaroErrorData { code: string; message: string; details: Record<string, unknown>; }
```

| Comando | Argumentos (`invoke`) | Devuelve | Errores posibles |
| --- | --- | --- | --- |
| `engine_status` | — | `EngineStatus` | — |
| `engine_restart` | — | `EngineStatus` (solo actúa en estado `error`; reinicia contadores; en otro estado devuelve el actual) | — |
| `vault_list_keys` | — | `KeySummary[]` (solo proveedores con clave, orden anthropic, openai, gemini) | `vault.keyring_unavailable` |
| `vault_add_key` | `{ input: { provider, secret, replace } }` | `KeySummary` con `status: "valid"` | `vault.invalid_input`, `vault.already_exists` (si existe y `replace=false`), `vault.invalid_key`, `vault.key_restricted`, `vault.provider_unreachable`, `vault.provider_rate_limited`, `vault.provider_error`, `vault.keyring_unavailable` |
| `vault_test_key` | `{ input: { provider } }` | `KeySummary` con `status` `valid` o `invalid` (una clave rechazada **no** es error del comando) | `vault.not_found`, `vault.provider_unreachable`, `vault.provider_rate_limited`, `vault.provider_error`, `vault.keyring_unavailable` |
| `vault_delete_key` | `{ input: { provider } }` | `null` (idempotente: borrar algo inexistente no es error) | `vault.keyring_unavailable` |

Orden de `vault_add_key`: validar formato → comprobar existencia → probar con el proveedor → **solo si es válida** guardar en el llavero → devolver resumen. Una clave 403 (`key_restricted`) no se guarda.

`vault_test_key` con 401 o 403 del proveedor → `status: "invalid"` y `last_error_code` (`vault.invalid_key` o `vault.key_restricted`), que la interfaz traduce para el chip "Falla" y el toast. Si devuelve error, el estado en memoria no cambia (§4.3).

**Prueba automática**: no hay comando nuevo. La interfaz llama a `vault_test_key` una vez por cada clave `untested` al montar la sección (una sola vez por sesión y proveedor; ADR 0003 justifica por qué no existe `vault_test_all`). No requiere permisos nuevos.

Evento: `engine://status` con carga `EngineStatus`, emitido en cada cambio de estado.

### 5.3 Permisos (capabilities)

`permissions/engine.toml`: `allow-engine-status` (`engine_status`), `allow-engine-restart` (`engine_restart`).
`permissions/vault.toml`: `allow-vault-list-keys`, `allow-vault-add-key`, `allow-vault-test-key`, `allow-vault-delete-key` (uno por comando).

`capabilities/main.json`:
```json
{
  "identifier": "main",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "allow-engine-status",
    "allow-engine-restart",
    "allow-vault-list-keys",
    "allow-vault-add-key",
    "allow-vault-test-key",
    "allow-vault-delete-key"
  ]
}
```
Sin `shell:*`, `fs:*`, `http:*`, `opener:*`, `updater:*` ni `notification:*` en F0.

`core:default` se mantiene en F0 (lo necesita la escucha de `engine://status`), pero concede más de lo necesario: en F1 se sustituye por los permisos de eventos concretos que use la interfaz (hallazgo T12, pendiente, §14). Los comandos también se declaran en `build.rs` con `AppManifest` (§4.3).

### 5.4 Catálogo de errores de F0 (`locales/es/errors.json`)

| `code` | Mensaje en español |
| --- | --- |
| `engine.start_failed` | No pudimos iniciar el motor de Faro. Intenta de nuevo. |
| `engine.restart_limit` | El motor se detuvo varias veces seguidas. Cierra Faro y vuelve a abrirlo. |
| `engine.dev_unreachable` | No encontramos el motor de desarrollo en la dirección configurada. |
| `engine.unauthorized` | El motor rechazó la conexión. Reinicia Faro. |
| `engine.forbidden_host` | El motor rechazó la conexión. Reinicia Faro. |
| `engine.not_found` | No encontramos lo que buscabas. |
| `engine.invalid_request` | La solicitud no es válida. Intenta de nuevo. |
| `engine.integrity_failed` | *(reservado, fase de release)* Faro no pudo verificar sus archivos. Reinstala la app. |
| `vault.invalid_input` | Esa clave no tiene el formato esperado. Cópiala de nuevo desde la página del proveedor. |
| `vault.invalid_key` | El proveedor rechazó esta clave. Revisa que esté completa y activa. |
| `vault.key_restricted` | La clave funciona, pero no tiene los permisos que Faro necesita. Crea una clave con acceso completo. |
| `vault.already_exists` | Ya tienes una clave de este proveedor. Reemplázala si quieres usar otra. |
| `vault.not_found` | No encontramos esa clave. Puede que ya la hayas borrado. |
| `vault.keyring_unavailable` | No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo. |
| `vault.provider_unreachable` | No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo. |
| `vault.provider_rate_limited` | El proveedor está recibiendo muchas solicitudes. Espera un minuto e intenta de nuevo. |
| `vault.provider_error` | El proveedor tuvo un problema. Intenta de nuevo en unos minutos. |
| `internal.unexpected` | Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro. |

## 6. Datos

Sin tablas ni migraciones en F0. Persistencia:
- Claves: Administrador de credenciales de Windows (servicio = `app.faro.desktop`, el identificador de la app, que no cambia nunca; usuario = `secret_ref`).
- Estado de prueba de claves: solo memoria.
- Logs: `<app_log_dir>` del núcleo (JSON, rotación diaria, 7 archivos), archivos `faro.AAAA-MM-DD.log` en `%LOCALAPPDATA%\app.faro.desktop\logs\`.
- Preferencia "barra lateral colapsada": `localStorage`.

## 7. Seguridad

- **Secretos que toca**: claves de Anthropic, OpenAI y Gemini (llavero del SO); token de sesión del motor (solo memoria del núcleo y del motor; en modo externo, `.env.local` ignorado por git, solo debug).
- **Recorrido de la clave**: campo del diálogo → `invoke` (IPC) → núcleo (`SecretString`) → proveedor por HTTPS o llavero. Nunca vuelve a la interfaz (solo `last4`), nunca al motor en F0, nunca a logs, errores ni `details`.
- **Red desde el núcleo**: solo `127.0.0.1:<puerto del motor>` y los 3 hosts de proveedores, fijos en código. Sin redirecciones.
- **Permisos Tauri**: los de §5.3, nada de la lista prohibida. CSP endurecida con `base-uri`, `form-action`, `object-src` y `frame-src` en `'none'` (ADR 0006, ya recogida en la skill); cualquier relajación futura requiere ADR.
- **Motor**: solo `127.0.0.1`; Bearer + `Host` en todas las rutas; `/docs` y `/openapi.json` deshabilitados por HTTP; `--dev` bloqueado en release por ambos lados (Rust con `cfg!(debug_assertions)`, Python con `sys.frozen`).
- **Acciones que publican o gastan**: ninguna. Probar una clave lista modelos y no consume saldo (verificar al implementar); la prueba automática hace como máximo una petición por proveedor y sesión.
- **Repositorio público** (ADR 0005), medidas obligatorias:
  1. **Nunca runners propios (self-hosted)**: un PR desde un fork podría ejecutar código en esa máquina.
  2. **Nunca `pull_request_target`** (ni `workflow_run` que ejecute código del PR).
  3. Aprobación obligatoria de workflows de PR de forks para todos los colaboradores externos; `permissions: contents: read` por defecto y `GITHUB_TOKEN` en solo lectura.
  4. Secret scanning + push protection, Dependabot alerts y CodeQL activados.
  5. Protección de `main`: PR obligatorio y `ci-ok` requerido.
  6. Secretos futuros de firma y updater solo en un **Environment protegido con aprobación**, nunca disponibles en workflows de PR.
  7. `gitleaks` en CI sobre todo el historial.
  8. `.env.local` en `.gitignore` desde el primer commit; ningún secreto real en el repositorio, ni siquiera de pruebas.
  9. `LICENSE` "Todos los derechos reservados": el código es visible pero no reutilizable. Toda verificación de licencias en el cliente se puede quitar compilando desde el código; la protección real está en el servidor (fases futuras).
- **Revisión obligatoria** de `revisor-seguridad`: T5, T6, T9, la configuración de T4 (CSP, capabilities) y T10 (workflows y configuración del repositorio).

---

## 8. Tareas

Orden: **T1 → (T2 ∥ T3 ∥ T4) → (T5 ∥ T6 ∥ T7) → (T8 ∥ T9) → T10 → T11 → T12 → T13**. `qa-pruebas` puede empezar T11 por capa en cuanto termina cada tarea.

| # | Agente | Tarea | Depende de | Terminado cuando |
| --- | --- | --- | --- | --- |
| T1 | devops-release | Raíz del monorepo: `package.json` con workspaces y scripts de §4.1, `.node-version`, `rust-toolchain.toml` (versión estable exacta), `.editorconfig`, `.gitattributes` (`* text=auto eol=lf`), `.gitignore` (incluye `.env.local`, `.venv`, `target`, `node_modules`, `dist`; presente desde el primer commit), `LICENSE` con el aviso "Todos los derechos reservados" (sin licencia open source, ADR 0005), Prettier, `.env.local.example`, `.vscode/extensions.json`, README raíz con la guía de §9, README placeholder de `apps/cloud` y `packages/wp-plugin`, `packages/shared/package.json`. | — | `npm install` funciona en una copia limpia; los scripts existen (pueden fallar hasta que existan las capas); README describe instalación y comandos; `LICENSE` existe y `.env.local` está ignorado en el primer commit. |
| T2 | frontend-react | Crear `apps/desktop` (Vite + React + TS + Tailwind v4 + shadcn/ui) y su configuración (package.json, vite, tsconfig, ESLint con `i18next/no-literal-string`, jsx-a11y, vitest). Tokens de color, fuentes locales, `AppShell`, `Sidebar` (8 secciones, colapsable), router, i18n base con los 10 namespaces en `es`/`en`/`pt-BR`, `EmptyState` y las 8 páginas con los textos de §3.3. `lib/api/invoke.ts` y `errors.ts`. *(Autorizado en F0 a crear los archivos de configuración de `apps/desktop/` fuera de `src/`.)* | T1 | `npm run lint:ts`, `typecheck` y `test:desktop` en verde; navegar por teclado a las 8 secciones; cero textos en duro; claves `en`/`pt-BR` con `[TODO] `. |
| T3 | motor-python | `apps/engine` completo según §4.4: proyecto uv, `__main__` con protocolo de token/ready/shutdown/EOF, seguridad Host+Bearer, errores comunes, logs a stderr, `/health`, `export_openapi.py`, `ruff` y `mypy --strict` configurados. | T1 | `uv run pytest` con cobertura ≥ 80 % (95 % en seguridad y protocolo); `ruff` y `mypy` limpios; arranque manual escribe una sola línea `ready` en stdout. |
| T4 | tauri-rust | Crear `apps/desktop/src-tauri`: `tauri.conf.json` (ventana, identifier `app.faro.desktop` definitivo, CSP, `freezePrototype`), `error.rs` (ADR 0002), `state.rs`, `logging.rs`, estructura de `commands/`, `permissions/` y `capabilities/main.json` de §5.3. | T1 (y `dist` de T2 para compilar) | `npm run dev` abre la ventana con la interfaz de T2; `cargo fmt`, `clippy -D warnings` y `cargo test` en verde. |
| T5 | tauri-rust | Supervisor del motor (§4.3): launchers debug y fake, protocolo, health, reinicios, apagado, modo externo, comandos `engine_status`/`engine_restart` y evento `engine://status`. | T3, T4 | Con `npm run dev` el estado llega a `ready`; matar el proceso Python lleva a `restarting` y vuelve a `ready`; al cerrar la ventana no queda ningún `python.exe` del motor; pruebas del supervisor con motor falso en verde. |
| T6 | tauri-rust | Bóveda (§4.3): `SecretStore` (keyring + memoria), `ProviderChecker` con los 3 proveedores, validación, los 4 comandos `vault_*` y sus permisos; `vault_test_key` no modifica el estado en memoria cuando la prueba no pudo ejecutarse. | T4 | Pruebas con `MemoryStore` y servidor HTTP simulado para cada mapeo de respuesta y para varias `vault_test_key` simultáneas; ninguna ruta de código devuelve o registra la clave; prueba manual con una clave real de un proveedor guarda la credencial en el Administrador de credenciales y la borra. |
| T7 | devops-release (+ motor-python para `export_openapi`) | `scripts/generate-contracts.mjs` y archivos generados en `packages/shared` (§4.5). | T3 | `npm run contracts` es determinista (dos ejecuciones seguidas no generan diferencias). |
| T8 | frontend-react | Inicio: `EngineStatusCard` con los 4 estados y **Reintentar conexión**, bloque "Qué hacer ahora" (§3.2), `lib/api/engine.ts`, `useEngineStatus`. | T2, T5 | Los 4 estados probados con `mockIPC` y eventos simulados; con la app real se ve "Motor conectado". |
| T9 | frontend-react | Configuración → Claves de IA (§3.4): filas por proveedor, diálogos agregar/reemplazar/borrar (con la ayuda por proveedor), probar, prueba automática de claves `untested` una vez por sesión (`useAutoTestKeys`), toasts, `lib/api/vault.ts`, traducciones de `errors.json` de §5.4. Sin `useMutation` para la clave. | T2, T6 | Estados vacío/cargando/error/éxito y "Probando…" probados; la prueba automática llama a `vault_test_key` exactamente una vez por clave `untested` y sesión; todo error muestra su mensaje traducido; el campo se vacía al cerrar; no hay la clave en caché de Query, `localStorage` ni `console`; flujo completo funciona con la app real. |
| T10 | devops-release | `ci.yml` de §4.7 (con `ci-ok`, concurrencia, cachés y `timeout-minutes`), `codeql.yml` y `dependabot.yml`, acciones fijadas por SHA. Lista de configuración del repositorio público de §4.7 y §7 (aprobación de PR de forks, `GITHUB_TOKEN` de solo lectura, secret scanning + push protection, Dependabot alerts, CodeQL, protección de `main` con `ci-ok` requerido, sin runners propios) documentada en el README con los comandos `gh` equivalentes; **la aplica el usuario o la aprueba antes de ejecutarla**. | T2–T7 | Un PR de prueba pasa todos los trabajos y `ci-ok`; un error de formato, lint, tipos o prueba en cualquier capa hace fallar el trabajo correspondiente y `ci-ok`; `contracts` falla si se edita un endpoint sin regenerar; ningún workflow usa `pull_request_target`, runners propios ni permisos de escritura salvo `security-events: write` en CodeQL; el usuario confirma la configuración del repositorio. |
| T11 | qa-pruebas | Pruebas de §10 en las tres capas, incluida `engine_real` (arranque real ignorada por defecto) y la lista de verificación manual. | T3, T5, T6, T8, T9 (por capa) | Tabla de resultados según `qa-pruebas`; umbrales de cobertura de TS y Python cumplidos; lista manual completa en Windows 11 y en Windows 10 22H2. |
| T12 | revisor-seguridad | Revisión con `revision-seguridad` (secciones 1, 2, 3 y 9) de T4, T5, T6, T9 y T10 (incluida la configuración del repositorio público). | T10, T11 | Veredicto `APROBADO` o `APROBADO CON CAMBIOS` con los cambios aplicados y re-revisados. |
| T13 | arquitecto | Cerrar F0: marcar esta spec como implementada y actualizar ADR si algo cambió durante la implementación. | T12 | Spec y ADR reflejan lo construido. |

---

## 9. Programas a instalar en Windows

Versiones: mínimas probadas por el diseño y recomendadas; todas **"verificar al instalar"** la última versión estable del momento. Ejecuta los comandos en PowerShell. `winget` viene con Windows 11; en Windows 10 22H2 viene con "Instalador de aplicación" (App Installer) de Microsoft Store: si `winget --version` falla, actualízalo desde la Store.

| # | Programa | Versión | Comando `winget` sugerido | Cómo verificar |
| --- | --- | --- | --- | --- |
| 1 | Git para Windows | ≥ 2.45 (recomendada: la última) | `winget install --id Git.Git -e` | `git --version` |
| 2 | Node.js LTS | Recomendada 24 LTS; mínima 22.12 (Vite 7). Verificar al instalar. | `winget install --id OpenJS.NodeJS.LTS -e` | `node -v` |
| 3 | npm (gestor elegido) | ≥ 10 (viene con Node; no se instala aparte) | — | `npm -v` |
| 4 | Rust (rustup) + toolchain `stable-x86_64-pc-windows-msvc` | Estable actual (≥ 1.85); la exacta la fija `rust-toolchain.toml` | `winget install --id Rustlang.Rustup -e` y luego `rustup default stable-msvc` y `rustup component add clippy rustfmt` | `rustc -V`, `cargo -V`, `rustup show` (debe decir `x86_64-pc-windows-msvc`) |
| 5 | Visual Studio 2022 Build Tools con la carga "Desarrollo de escritorio con C++" (incluye MSVC y Windows 11 SDK) | 17.x más reciente; Windows SDK 10.0.22621 o posterior. Verificar al instalar. | `winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` | `& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property displayName` |
| 6 | Microsoft Edge WebView2 Runtime | Evergreen (ya viene en Windows 11; en Windows 10 22H2 puede faltar y hay que instalarlo) | `winget install --id Microsoft.EdgeWebView2Runtime -e` (solo si falta) | `Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' \| Select-Object pv` |
| 7 | uv | ≥ 0.8 (verificar al instalar) | `winget install --id astral-sh.uv -e` | `uv --version` |
| 8 | Python 3.12 | 3.12.x, último parche | Recomendado vía uv: `uv python install 3.12`. Alternativa: `winget install --id Python.Python.3.12 -e` | `uv python find 3.12` (o `py -3.12 --version`) |
| 9 | GitHub CLI (opcional) | ≥ 2.60 | `winget install --id GitHub.cli -e` | `gh --version`, `gh auth status` |
| 10 | Visual Studio Code (editor recomendado) | Última | `winget install --id Microsoft.VisualStudioCode -e` | `code --version` |

Después de instalar:
- Cierra y vuelve a abrir la terminal (para que el `PATH` se actualice).
- `git config --global core.longpaths true` (rutas largas de `node_modules` y `target`).
- `git config --global core.autocrlf false` (el repositorio fija LF con `.gitattributes`).
- Opcional para auditoría local: `cargo install cargo-audit --locked`.
- En la raíz del repo: `npm run setup` y luego `npm run dev`.

**Extensiones de VS Code** (quedan en `.vscode/extensions.json`): `rust-lang.rust-analyzer`, `tauri-apps.tauri-vscode`, `vadimcn.vscode-lldb`, `dbaeumer.vscode-eslint`, `esbenp.prettier-vscode`, `bradlc.vscode-tailwindcss`, `lokalise.i18n-ally`, `ms-python.python`, `charliermarsh.ruff`, `ms-python.mypy-type-checker`, `EditorConfig.EditorConfig`, `github.vscode-github-actions`.

Versiones de librerías de referencia (fijar la exacta al crear, verificar al instalar): Tauri 2.x, React 19, Vite 7, TypeScript 5.9, Tailwind CSS 4, React Router 7, TanStack Query 5, i18next 25, FastAPI ≥ 0.115, Pydantic 2, keyring 3, reqwest 0.13 (la spec original decía 0.12; §13). Dependencias Rust añadidas en la implementación: tokio, secrecy, zeroize, getrandom, base64, dotenvy, win32job (solo Windows), keyring 3, chrono.

---

## 10. Pruebas (para qa-pruebas)

Sin servicios reales ni claves reales. `TZ=UTC`. Sin `sleep`: esperas por condición o reloj/tiempos inyectados (`SupervisorConfig`).

### 10.1 Interfaz (Vitest + Testing Library + `mockIPC`)
- La barra lateral muestra las 8 secciones en orden, con `aria-current` en la activa; navegación con teclado; colapso a 64 px conserva tooltips.
- Cada una de las 8 páginas renderiza su estado vacío con las claves i18n correctas (sin claves faltantes: prueba que recorre `es` y falla si `t()` devuelve la clave).
- `EngineStatusCard`: `starting`, `ready` (con "Motor conectado"), `restarting`, `error` para `engine.start_failed` y `engine.restart_limit`; **Reintentar conexión** invoca `engine_restart`; reacciona a eventos `engine://status` simulados.
- Bóveda: cargando, error `vault.keyring_unavailable` con **Intentar de nuevo**, sin claves (estado vacío), con claves en cada estado; agregar OK (diálogo se cierra, campo vacío, lista se refresca); agregar con `vault.invalid_key` (diálogo abierto, mensaje traducido); `already_exists` → reemplazar envía `replace: true`; probar → `valid` e `invalid`; borrar con confirmación; cada error de guardar, probar, borrar y listar muestra su mensaje traducido (y `internal.unexpected` para un `code` desconocido).
- Ayuda del diálogo por proveedor; la de Gemini dice exactamente "Encuéntrala en Google AI Studio, en la sección de claves de API."
- Prueba automática (`useAutoTestKeys`):
  - Con dos claves `untested` y una `valid`, al montar se llama a `vault_test_key` exactamente una vez por cada `untested` y nunca para la `valid`/`invalid`.
  - Durante la llamada la fila muestra "Probando…" y sus botones están deshabilitados.
  - Resultado `valid` → "Conectada"; resultado `invalid` con `vault.invalid_key` → "Falla" + mensaje traducido.
  - Error `vault.provider_unreachable` → "Sin probar" + mensaje traducido de conexión + **Probar clave** habilitado; no se reintenta sola.
  - Desmontar y volver a montar la sección no vuelve a llamar a `vault_test_key` (una vez por sesión).
  - Si `vault_list_keys` falla, no se llama a `vault_test_key`.
  - Sin toasts por la prueba automática.
- `invoke.ts`: un rechazo sin forma `{code,message,details}` se convierte en `internal.unexpected`; un `code` fuera del catálogo muestra el `message` del backend y, si está vacío, el mensaje genérico (ADR 0002). En TS, el objeto que llega por IPC se llama `FaroErrorData` (`types.ts`) y la clase, `FaroError` (`errors.ts`).
- Ningún test ni snapshot contiene una clave con formato real (usar `test-key-000000000000000000001a2B`).

### 10.2 Núcleo Rust (`cargo test`)
Cobertura de Rust no medida en F0; desde F1 se exige con `cargo-llvm-cov`.
- Supervisor con launcher falso (pruebas en `src/engine/supervisor_tests.rs`, dentro del crate, para no exponer el launcher falso en la API pública): `ready` válido → `ready`; sin `ready` antes del límite → `engine.start_failed` (`timeout`); `ready` malformado o puerto fuera de rango → `bad_ready`; proceso que sale → `exited`; 3 fallos de health → reinicio; 4.º reinicio dentro de la ventana → `engine.restart_limit`; `engine_restart` desde `error` reinicia contadores; apagado envía `shutdown` y mata tras el límite.
- El token enviado por stdin mide 43 caracteres base64url y cambia en cada arranque; no aparece en ningún log capturado (`tracing` con subscriber de prueba).
- Líneas de stdout no reconocidas se ignoran sin registrar su contenido.
- Bóveda con `MemoryStore` y servidor simulado: cada mapeo de §4.3 (2xx, 401, 400 `API_KEY_INVALID`, 403, 429, 5xx, timeout); `add` no guarda si la prueba falla; `already_exists` sin `replace`; `delete` idempotente; `list` solo devuelve proveedores con clave; ningún valor de retorno ni `Debug` de las entradas contiene el secreto; la petición a Gemini no lleva la clave en la URL.
- Estado de prueba en memoria: tras `vault_test_key` con veredicto, `vault_list_keys` devuelve `valid`/`invalid` (ya no `untested`); con error de red, 429 o 5xx, el estado anterior no cambia (`untested` sigue `untested`, `valid` sigue `valid`); `vault_delete_key` elimina el estado; un `SecretStore` nuevo con claves precargadas lista todas como `untested`.
- Tres `vault_test_key` simultáneas (una por proveedor) terminan todas con el resultado correcto y sin mezclar estados (serialización por el mutex).
- `KeyringStore` usa el servicio `app.faro.desktop` (constante única, sin posibilidad de configurarlo).
- Validación de formato: vacía, con espacios internos, < 20, > 512, no ASCII.
- `AppError` serializa exactamente `{code, message, details}`.
- `engine_real` (`#[ignore]`, corre en CI tras `uv sync`): lanza el motor real desde `.venv`, llega a `ready`, `/health` con token = 200, sin token = 401, y el apagado deja el proceso terminado.

### 10.3 Motor (pytest)
- `/health`: 200 con token y Host correctos; 401 sin token y con token incorrecto; 403 con `Host: localhost:<port>` y con Host de otro puerto; formato de error común en ambos.
- Rutas inexistentes → 404 `engine.not_found` con Bearer; `/docs` y `/openapi.json` no disponibles.
- Excepción no controlada → 500 `internal.unexpected` sin traza en el cuerpo.
- Protocolo (`__main__` en subproceso): primera línea de stdout es `ready` con puerto real; token ausente o inválido → código 2; `{"event":"shutdown"}` → sale en < 10 s; cerrar stdin → sale; `--host 0.0.0.0` → código 2; `--dev` rechazado con `sys.frozen` simulado.
- Los logs (stderr) no contienen el token ni la cabecera `Authorization`.

### 10.4 Contratos
- `npm run contracts` dos veces seguidas no genera diferencias; `engine-operations.json` contiene exactamente `getHealth`.

### 10.5 Verificación manual en Windows 11 y Windows 10 22H2 (lista para cerrar F0)

Se ejecuta completa en las dos versiones (Windows 10 22H2 en máquina física o VM, porque GitHub no tiene runners de Windows 10). En Windows 10, anotar si hubo que instalar WebView2. La lista detallada para marcar está en [docs/qa/2026-09-29-f0-verificacion-manual.md](../qa/2026-09-29-f0-verificacion-manual.md). `faro.log` = archivos `faro.AAAA-MM-DD.log`; la credencial aparece como `llm/<provider>/default.app.faro.desktop`.

1. `npm run setup` y `npm run dev` en una copia limpia siguiendo solo el README.
2. Inicio pasa de "Encendiendo el motor de Faro…" a "Motor conectado" en menos de 5 s.
3. Terminar `python.exe` del motor desde el Administrador de tareas → "reconectando" → "Motor conectado".
4. Agregar una clave real de un proveedor → "Conectada"; aparece en el Administrador de credenciales de Windows (Credenciales de Windows → genéricas) con el nombre de la referencia; la clave no aparece en `faro.log`.
5. Agregar una clave inventada → mensaje de clave rechazada y nada guardado.
6. Sin internet, probar una clave → mensaje de conexión.
7. Con una clave válida guardada, cerrar y volver a abrir Faro, ir a Configuración → Claves de IA: la fila muestra "Probando…" y pasa sola a "Conectada" sin pulsar nada. Ir a otra sección y volver: no se repite la prueba (en `faro.log` hay un solo registro de prueba por proveedor en la sesión, sin la clave).
8. Cerrar Faro, desconectar internet, abrir Faro e ir a Claves de IA: la fila queda en "Sin probar" con el mensaje "No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo."; al reconectar, **Probar clave** la deja en "Conectada".
9. La credencial aparece en el Administrador de credenciales con el servicio `app.faro.desktop`.
10. Borrar la clave → desaparece del Administrador de credenciales.
11. Cerrar la ventana → no queda ningún proceso `python.exe` del motor.
12. Modo externo: `npm run dev:engine` + `FARO_ENGINE_DEV_URL` en `.env.local` → "Motor conectado"; detener el motor externo → error `engine.dev_unreachable`.

---

## 11. Decisiones tomadas (2026-09-28)

1. **Identificador de la app**: `app.faro.desktop`, definitivo. No se cambia nunca (servicio del llavero y carpeta de datos). §4.3, §6.
2. **Agentes**: el agente principal crea las definiciones que faltaban en `.claude/agents/` (`motor-python`, `ingeniero-ia`, `seo-datos`, `google-ads`, `nube-backend`, `wordpress-php`). T3 y la parte de `export_openapi` de T7 las hace `motor-python`.
3. **Skills**: el agente principal las actualiza a las decisiones de los ADR 0001–0004 (rutas `apps/engine/faro_engine/core`, `npm run test:engine`, modos de `npm run dev`, prueba de claves en el núcleo, namespace `home`).
4. **Claves siempre probadas**: ninguna clave se guarda sin prueba exitosa, y las claves `untested` se prueban automáticamente una vez por sesión al abrir Configuración → Claves de IA, llamando a `vault_test_key` por clave (sin comando nuevo). §3.4, §4.2, §4.3, §5.2, ADR 0003.
5. **Gemini**: solo claves de Google AI Studio; Vertex AI fuera de alcance. §2, §3.4.
6. **Repositorio**: GitHub **público**, plan gratuito, sin licencia open source (`LICENSE` "Todos los derechos reservados"), con las medidas de seguridad de §7. CI completa (incluido Windows) en cada PR y push a `main`. §4.7, ADR 0005.
7. **Windows mínimo**: Windows 10 22H2 y Windows 11; se revisa en octubre de 2027; bootstrapper de WebView2 pendiente para release; verificación manual en ambas versiones. §2, §9, §10.5.
8. **Titular del copyright**: Concersa. `LICENSE`: "Copyright © 2026 Concersa. Todos los derechos reservados." (T1).

**Decisiones de la implementación (2026-09-29):**

9. **CSP endurecida** con `base-uri`, `form-action`, `object-src` y `frame-src` en `'none'`; relajarla requiere ADR. §4.3, §7, ADR 0006.
10. **EOF en modo `--dev`** no apaga el motor. §2, §4.4, ADR 0004.
11. **ACL explícito**: `build.rs` declara los comandos con `AppManifest`. §4.3.
12. Resto de diferencias menores (logs, ubicación de pruebas, versiones, nombres TS, credencial) en §13.

## 12. Preguntas abiertas

Ninguna.

---

## 13. Diferencias con lo implementado (cierre T13, 2026-09-29)

Lo construido cumple la spec salvo estas diferencias, ya reflejadas en sus secciones:

| # | Tema | Spec original | Implementado | Sección |
| --- | --- | --- | --- | --- |
| 1 | Archivo de log del núcleo | `<app_log_dir>/faro.log` | `faro.AAAA-MM-DD.log` (tracing-appender añade la fecha), en `%LOCALAPPDATA%\app.faro.desktop\logs\` | §4.3, §6 |
| 2 | Pruebas del supervisor | `src-tauri/tests/engine_supervisor.rs` | `src/engine/supervisor_tests.rs`, para no exponer el launcher falso en la API pública del crate | §4.1, §10.2 |
| 3 | Versión de reqwest | 0.12 | 0.13 | §4.3, §9 |
| 4 | Dependencias Rust | No listadas | tokio, secrecy, zeroize, getrandom, base64, dotenvy, win32job (solo Windows), keyring 3, chrono | §9 |
| 5 | Error en TS | `FaroError` como interfaz | Objeto por IPC: `FaroErrorData` (`types.ts`); clase: `FaroError` (`errors.ts`) | §4.2, §5.2 |
| 6 | ACL de comandos | Solo `permissions/*.toml` | `build.rs` declara los comandos con `AppManifest` para que el ACL no dependa de que existan los `.toml` (hallazgo T12) | §4.3, §5.3 |
| 7 | CSP | La de la skill | Endurecida con `base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'` (hallazgo T12, ADR 0006) | §4.3, §7 |
| 8 | `core:default` en la capability | Aceptado sin nota | Se mantiene en F0; se reducirá a los permisos de eventos necesarios en F1 (hallazgo T12, pendiente) | §5.3, §14 |
| 9 | Carrera `spawn` / Job Object | No contemplada | Mitigada en desarrollo; en release el lanzador debe crear el proceso suspendido, asignarlo al job y reanudarlo (pendiente) | §4.3, §14 |
| 10 | Nombre de la credencial en Windows | "nombre de la referencia" | `llm/<provider>/default.app.faro.desktop` (formato del crate keyring; servicio `app.faro.desktop`) | §4.3, §10.5 |
| 11 | `cargo audit` en CI | Sin ignorados | Archivo de ignorados solo para crates exclusivos de Linux, cada uno justificado (lo prepara devops-release) | §4.7 |
| 12 | Cobertura de Rust | "Se mide desde F1" | Confirmado: no medida en F0; desde F1 se exige con `cargo-llvm-cov` | §2, §10.2 |
| 13 | EOF de stdin en el motor | Siempre apaga el motor | En modo `--dev` el EOF no apaga el motor (se detiene con Ctrl+C); sin `--dev` no cambia (hallazgo QA, ADR 0004) | §2, §4.4 |
| 14 | Barra lateral contraída | Solo iconos con tooltip y `aria-current` en la activa | Error visual encontrado en la verificación manual (2026-09-29): con la barra contraída se veían formas oscuras detrás de los iconos, porque el `Slot` de Radix convertía en texto el `className` en forma de función del `NavLink`. Corregido aplicando las clases de la sección activa mediante `aria-current` (selector CSS) y añadiendo pruebas | §3.1, §10.1 |

## 14. Pendientes para F1 y release

**Antes de dar F0 por cerrada del todo (usuario / devops-release):**
1. Abrir el **primer PR en GitHub** para validar la CI de verdad, en especial el trabajo `core` en `windows-latest` (incluida `engine_real`) y el trabajo `audit` (con su archivo de ignorados). — devops-release.
2. **Aplicar la configuración del repositorio** de ADR 0005 y §4.7 (aprobación de PR de forks, `GITHUB_TOKEN` de solo lectura, secret scanning + push protection, Dependabot alerts, CodeQL, protección de `main` con `ci-ok`, sin runners propios). La aplica el usuario o aprueba los comandos `gh` preparados. — usuario.
3. **Verificación manual** de [docs/qa/2026-09-29-f0-verificacion-manual.md](../qa/2026-09-29-f0-verificacion-manual.md):
   - [x] **Windows 11**: hecha el 2026-09-29, aprobada por el usuario (todo corre bien y estable). Se encontró y corrigió un error visual de la barra lateral contraída (§13, fila 14).
   - [ ] **Windows 10 22H2**: pendiente. — usuario.
3b. ~~Aplicar la paleta del [ADR 0007](../adr/0007-paleta-grises-y-turquesa.md)~~ **Hecho (2026-09-29):** `tokens.css` con la paleta nueva (más los tokens `input`, `overlay` y `shadow`, y `border` claro en #D2D2D2 por contraste), `tokens.test.ts` (contraste WCAG en ambos modos, límite #333333, sin morado) y `palette.test.ts` (prohíbe colores fuera de los tokens). Pendiente: repetir el paso 3 de la verificación manual (tema claro y oscuro) con la paleta nueva.

**F1:**
4. Sustituir `core:default` en `capabilities/main.json` por los permisos de eventos que la interfaz use realmente (hallazgo T12). — tauri-rust, revisión de revisor-seguridad.
5. Medir y exigir cobertura de Rust con `cargo-llvm-cov` en la CI. — devops-release + qa-pruebas.
6. Pruebas extremo a extremo automáticas (WebdriverIO + tauri-driver), ya previstas en §2.

**Fase de release:**
7. Lanzador del sidecar que **cree el proceso suspendido, lo asigne al Job Object y lo reanude**, para cerrar la carrera `spawn`/job. — tauri-rust, revisión de revisor-seguridad.
8. Lo ya anotado en §2: PyInstaller + `externalBin` + integridad SHA-256, bootstrapper de WebView2.

**Continuo:**
9. Revisar el archivo de ignorados de `cargo audit` en cada actualización de dependencias: solo crates exclusivos de Linux y con justificación; cualquier otro caso se corrige, no se ignora.
10. Cualquier relajación de la CSP requiere ADR (ADR 0006).
