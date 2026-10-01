# Faro

Faro es una app de escritorio de SEO y Google Ads para personas y pequeñas empresas con sitios WordPress o WooCommerce. Sus agentes de IA investigan, revisan el sitio y preparan contenido y anuncios en la computadora del usuario; el usuario aprueba todo lo que se publica o gasta dinero.

La app combina una interfaz React, un núcleo Rust (Tauri 2) y un motor Python local que el núcleo lanza y supervisa.

> Estado: fase F0 (esqueleto) en construcción. Ver la especificación en [`docs/specs/2026-09-28-f0-esqueleto.md`](docs/specs/2026-09-28-f0-esqueleto.md) y las decisiones en [`docs/adr/`](docs/adr/).

## Estructura del monorepo

```
Faro_App/
├─ apps/
│  ├─ desktop/          # App de escritorio: interfaz (React + TS) y núcleo Tauri en src-tauri/ (Rust)
│  ├─ engine/           # Motor local (Python 3.12 + FastAPI), proyecto uv independiente
│  └─ cloud/            # Nube de Faro (fase posterior)
├─ packages/
│  ├─ shared/           # Contratos generados del motor (npm run contracts)
│  └─ wp-plugin/        # Plugin de WordPress/WooCommerce (PHP, licencia GPL-2.0-or-later)
├─ scripts/             # Scripts de build y generación de contratos
├─ docs/                # Especificaciones (specs/) y decisiones de arquitectura (adr/)
├─ .github/             # CI de GitHub Actions
├─ package.json         # npm workspaces y scripts comunes para todas las capas
├─ .node-version        # Versión de Node
└─ rust-toolchain.toml  # Versión exacta de Rust
```

El gestor de paquetes de JavaScript es **npm workspaces** (ADR 0001). Python usa **uv** con un proyecto independiente por desplegable. Rust es un único crate en `apps/desktop/src-tauri`.

## Instalación en Windows

Sistemas soportados: **Windows 11** y **Windows 10 22H2** (x64).

Ejecuta los comandos en PowerShell. `winget` viene con Windows 11; en Windows 10 22H2 viene con "Instalador de aplicación" (App Installer) de Microsoft Store: si `winget --version` falla, actualízalo desde la Store. Las versiones son las mínimas probadas; al instalar, usa la última versión estable del momento.

| # | Programa | Versión | Comando `winget` sugerido | Cómo verificar |
| --- | --- | --- | --- | --- |
| 1 | Git para Windows | ≥ 2.45 (recomendada: la última) | `winget install --id Git.Git -e` | `git --version` |
| 2 | Node.js LTS | Recomendada 24 LTS (el repositorio fija la exacta en `.node-version`); mínima 22.12 | `winget install --id OpenJS.NodeJS.LTS -e` | `node -v` |
| 3 | npm | ≥ 10 (viene con Node; no se instala aparte) | — | `npm -v` |
| 4 | Rust (rustup) + toolchain `stable-x86_64-pc-windows-msvc` | Estable actual (≥ 1.85); la exacta la fija `rust-toolchain.toml` | `winget install --id Rustlang.Rustup -e` y luego `rustup default stable-msvc` y `rustup component add clippy rustfmt` | `rustc -V`, `cargo -V`, `rustup show` (debe decir `x86_64-pc-windows-msvc`) |
| 5 | Visual Studio 2022 Build Tools con la carga "Desarrollo de escritorio con C++" (incluye MSVC y Windows 11 SDK) | 17.x más reciente; Windows SDK 10.0.22621 o posterior | `winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"` | `& "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property displayName` |
| 6 | Microsoft Edge WebView2 Runtime | Evergreen (ya viene en Windows 11; en Windows 10 22H2 puede faltar) | `winget install --id Microsoft.EdgeWebView2Runtime -e` (solo si falta) | `Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' \| Select-Object pv` |
| 7 | uv | ≥ 0.8 | `winget install --id astral-sh.uv -e` | `uv --version` |
| 8 | Python 3.12 | 3.12.x, último parche | Recomendado vía uv: `uv python install 3.12`. Alternativa: `winget install --id Python.Python.3.12 -e` | `uv python find 3.12` (o `py -3.12 --version`) |
| 9 | GitHub CLI (opcional) | ≥ 2.60 | `winget install --id GitHub.cli -e` | `gh --version`, `gh auth status` |
| 10 | Visual Studio Code (editor recomendado) | Última | `winget install --id Microsoft.VisualStudioCode -e` | `code --version` |

Después de instalar:

1. Cierra y vuelve a abrir la terminal para que se actualice el `PATH`.
2. Activa las rutas largas de Git (`node_modules` y `target` las necesitan): `git config --global core.longpaths true`.
3. Desactiva la conversión de finales de línea (el repositorio fija LF con `.gitattributes`): `git config --global core.autocrlf false`.
4. Opcional, para auditar dependencias de Rust en local: `cargo install cargo-audit --locked`.

La primera vez que ejecutes `cargo` dentro del repositorio, rustup instalará automáticamente la versión de Rust fijada en `rust-toolchain.toml`.

### Primer arranque

En la raíz del repositorio:

```powershell
npm run setup   # instala dependencias de Node y crea apps/engine/.venv con uv
npm run dev     # abre la app; el núcleo lanza el motor desde apps/engine/.venv
```

Al abrir VS Code, acepta instalar las extensiones recomendadas (están en `.vscode/extensions.json`).

## Comandos npm

Todos los comandos se ejecutan desde la raíz del repositorio. Son la interfaz común para todas las capas: la CI y los agentes usan exactamente estos nombres.

| Comando | Qué hace |
| --- | --- |
| `npm run setup` | `npm ci`, `uv sync --directory apps/engine --locked` (dependencias exactas de los lockfiles) y `build:wp-plugin` |
| `npm run dev` | Abre la app en modo desarrollo (`tauri dev`); el núcleo lanza y supervisa el motor desde `apps/engine/.venv` |
| `npm run dev:engine` | Arranca solo el motor en modo externo (`--dev`), para usarlo con `FARO_ENGINE_DEV_URL` |
| `npm run lint` | Ejecuta `lint:ts`, `lint:rs` y `lint:py` |
| `npm run lint:ts` | ESLint de la interfaz (`apps/desktop`) |
| `npm run lint:rs` | `cargo clippy --all-targets -- -D warnings` del núcleo (`apps/desktop/src-tauri`) |
| `npm run lint:py` | `ruff check` del motor (`apps/engine`) |
| `npm run format` | Formatea todo: Prettier (TS, JSON, Markdown, CSS), `cargo fmt` (Rust) y `ruff format` (Python) |
| `npm run format:check` | Igual que `format`, pero solo comprueba y falla si algún archivo no tiene el formato correcto |
| `npm run typecheck` | `tsc --noEmit` de los paquetes TypeScript y `mypy` del motor |
| `npm run test` | Ejecuta `test:desktop`, `test:core`, `test:engine`, `test:contracts` y `test:scripts` |
| `npm run test:desktop` | Pruebas de la interfaz con Vitest |
| `npm run test:core` | Pruebas del núcleo con `cargo test` |
| `npm run test:engine` | Pruebas del motor con pytest (`uv run --directory apps/engine pytest`) |
| `npm run test:contracts` | Pruebas del generador de contratos |
| `npm run test:scripts` | Pruebas del comprobador de umbrales de cobertura de Rust (`scripts/check-rust-coverage.mjs`) |
| `npm run coverage:core` | Pruebas del núcleo con `cargo llvm-cov` y los umbrales de la CI: 80 % de líneas global y 95 % en `src/vault/`, `src/secrets/`, `src/profile/` y `src/engine/protocol.rs`. Requiere `cargo install cargo-llvm-cov --version 0.9.1 --locked` y `rustup component add llvm-tools-preview` |
| `npm run test:all` | En F0 es igual que `test`; desde F1 incluye las pruebas extremo a extremo |
| `npm run contracts` | Regenera los contratos del motor en `packages/shared` (`openapi.json`, `engine.d.ts`, `engine-operations.json`) |
| `npm run build:wp-plugin` | Genera el zip del plugin de WordPress en `packages/wp-plugin/dist/faro-wordpress.zip` (carpeta `faro/`, sin pruebas ni herramientas; dos builds dan el mismo SHA-256) |

## Modo de desarrollo del motor

En builds de depuración el núcleo puede usar el motor de dos formas (ADR 0004). En ambos casos el núcleo y el motor se comunican solo por `127.0.0.1` con un token de sesión.

**Gestionado (por defecto).** `npm run dev` abre la app y el núcleo lanza directamente `apps/engine/.venv/Scripts/python.exe -m faro_engine`, le envía el token por la entrada estándar, espera su señal de listo, lo vigila y lo reinicia si deja de responder. Requiere haber ejecutado antes `npm run setup`; si falta `apps/engine/.venv`, la pantalla de Inicio muestra un error de arranque del motor.

**Externo.** Útil para reiniciar el motor sin cerrar la app:

1. Copia la plantilla: `Copy-Item .env.local.example .env.local`.
2. En `.env.local`, define `FARO_ENGINE_DEV_URL=http://127.0.0.1:8765` (el puerto debe coincidir con `FARO_ENGINE_DEV_PORT`) y un `FARO_ENGINE_DEV_TOKEN` generado con el comando que indica la plantilla.
3. En una terminal: `npm run dev:engine`.
4. En otra terminal: `npm run dev`.

En modo externo el núcleo no reinicia el motor: si lo detienes, Inicio muestra que no encuentra el motor de desarrollo. Para volver al modo gestionado, comenta o borra `FARO_ENGINE_DEV_URL` en `.env.local`.

`.env.local` nunca se sube al repositorio (está en `.gitignore`) y no debe contener claves de proveedores de IA ni otros secretos reales. Los builds de release ignoran estos modos.

## Probar con un WordPress local (wp-env)

Estos pasos montan un WordPress con WooCommerce en tu computadora con [wp-env](https://developer.wordpress.org/block-editor/reference-guides/packages/packages-env/) y lo conectan con Faro en modo desarrollo. Ejecuta los comandos en PowerShell desde la raíz del repositorio, salvo cuando se indique otra carpeta.

### Requisitos

- Lo de [Instalación en Windows](#instalación-en-windows): Node, Rust (con Visual Studio Build Tools) y uv, y haber ejecutado `npm run setup` al menos una vez.
- **Docker Desktop con WSL 2** (wp-env levanta WordPress en contenedores):
  1. Si no tienes WSL 2: `wsl --install` en una terminal de administrador y reinicia.
  2. `winget install --id Docker.DockerDesktop -e`.
  3. Abre Docker Desktop y, en Settings → General, deja marcado **Use the WSL 2 based engine**.
  4. Comprueba que funciona: `docker run --rm hello-world` debe mostrar "Hello from Docker!".

Docker Desktop tiene que estar abierto cada vez que uses wp-env.

### 1. Generar el zip del plugin

```powershell
npm run build:wp-plugin
```

Crea `packages/wp-plugin/dist/faro-wordpress.zip`, el archivo que guarda el botón **Guardar el plugin en Descargas** de la app. `npm run setup` ya lo genera; vuelve a ejecutarlo si cambias el plugin.

### 2. Arrancar WordPress con wp-env

wp-env y sus versiones están fijados en `packages/wp-plugin/package-lock.json`. Instálalo una vez (sin scripts de instalación, igual que en la CI) y arráncalo desde la carpeta del plugin:

```powershell
npm ci --prefix packages/wp-plugin --ignore-scripts
cd packages/wp-plugin
npx --no-install wp-env start
```

La primera vez tarda varios minutos (descarga imágenes de Docker, WordPress y WooCommerce). Las versiones de PHP, WordPress y WooCommerce las fija `packages/wp-plugin/.wp-env.json`.

Cuando termine:

- Sitio: http://localhost:8888
- Administración: http://localhost:8888/wp-admin, usuario `admin` y contraseña `password` (valores por defecto de wp-env). Docker puede abrir el puerto 8888 a tu red local: no dejes wp-env arrancado en redes compartidas o públicas y apágalo al terminar (paso 7).

### 3. Activar el plugin Faro

wp-env monta el código de `packages/wp-plugin` como el plugin `faro` (no hace falta subir el zip), pero no lo activa. Elige una opción:

- En http://localhost:8888/wp-admin → **Plugins**, busca **Faro** y pulsa **Activar**.
- O, desde `packages/wp-plugin`: `npx --no-install wp-env run cli wp plugin activate faro`.

### 4. Generar el código de conexión

En wp-admin → **Ajustes → Faro**, pulsa **Generar código de conexión**. Verás un código de 6 dígitos y la dirección `http://localhost:8888`. El código caduca en 10 minutos y solo sirve una vez; si caduca, pulsa **Generar otro código**.

Funciona con `http` porque `.wp-env.json` define `WP_ENVIRONMENT_TYPE=local`. En un sitio real el plugin exige HTTPS.

### 5. Permitir sitios locales en Faro

Por seguridad, Faro solo se conecta a sitios con HTTPS en internet (ADR 0012). Para aceptar `http://localhost:8888`, vuelve a la raíz del repositorio y activa el modo de sitios locales en `.env.local`:

```powershell
cd ../..
if (-not (Test-Path .env.local)) { Copy-Item .env.local.example .env.local }
```

Abre `.env.local` y cambia la línea a:

```
FARO_ALLOW_LOCAL_SITES=1
```

Solo vale el valor exacto `1`. Con él, el núcleo de un build de depuración lanza el motor con `--allow-local-sites`, que permite `http` y direcciones de tu propia computadora (`localhost`, `127.0.0.1`, `::1`). Las redes privadas (`192.168.x.x`, `10.x.x.x`…) siguen bloqueadas. También puedes definir la variable solo para una terminal: `$env:FARO_ALLOW_LOCAL_SITES = '1'` antes de `npm run dev`.

### 6. Arrancar Faro y conectar el sitio

```powershell
npm run dev
```

Si la app ya estaba abierta, ciérrala y vuelve a ejecutar `npm run dev`: el valor se lee al lanzar el motor. En la app, ve a **Configuración → Sitios conectados → Conectar tu sitio**, escribe `http://localhost:8888` y el código del paso 4. La tarjeta del sitio debe quedar como **Conectado**.

Desde la tarjeta puedes **Comprobar conexión**, **Ver contenido** (páginas, entradas y productos) y **Desconectar sitio**. El botón **Guardar el plugin en Descargas** del asistente copia `faro-wordpress.zip` a tu carpeta Descargas, por si quieres instalarlo en otro WordPress con **Plugins → Añadir nuevo → Subir plugin**.

Si prefieres probar el flujo sin la interfaz (por ejemplo, para depurar el motor), con wp-env arrancado ejecuta `uv run --directory apps/engine pytest -m wp_env --no-cov`: conecta, comprueba, lee contenido, desconecta y quita el sitio con un llavero simulado y su propia base temporal, sin tocar tus datos de Faro.

### 7. Apagar wp-env

Desde `packages/wp-plugin`:

```powershell
npx --no-install wp-env stop      # detiene los contenedores y conserva el sitio
npx --no-install wp-env destroy   # opcional: borra el sitio y empieza de cero la próxima vez
```

Cuando termines de probar, vuelve a poner `FARO_ALLOW_LOCAL_SITES=0` en `.env.local`.

> **El modo de sitios locales nunca funciona en la app instalada.** Solo lo activa un build de depuración (`npm run dev`) o el motor en modo `--dev`. El núcleo de release nunca pasa `--allow-local-sites` y el motor empaquetado se niega a arrancar con ese argumento.

## Integración continua

Cada pull request hacia `main`, cada push a `main` y cada ejecución manual lanzan `.github/workflows/ci.yml`:

| Trabajo | Runner | Qué comprueba |
| --- | --- | --- |
| `ts` | Ubuntu | Prettier, ESLint, `tsc` y Vitest con cobertura (70 %) |
| `engine` | Ubuntu, Windows y macOS | `ruff format --check`, `ruff check`, `mypy --strict` y pytest con cobertura (80 %; 95 % en seguridad y protocolo) |
| `core` | Windows | Build de la interfaz, `cargo fmt --check`, `clippy -D warnings`, pruebas con `cargo llvm-cov` (falla si la cobertura de líneas baja del 80 % global o del 95 % en `vault/`, `secrets/`, `profile/` y `engine/protocol.rs`) y el arranque real del motor (`engine_real`) |
| `contracts` | Ubuntu | `npm run contracts` no deja diferencias en `packages/shared` |
| `wp-plugin` | Ubuntu | Plugin de WordPress: PHPCS (WordPress Coding Standards), PHPStan y el zip del plugin (contenido y hash estable) |
| `wp-plugin-integration` | Ubuntu (Docker) | PHPUnit del plugin en wp-env: configuración actual (PHP 8.3, WordPress y WooCommerce fijados, HPOS activado y desactivado, más la integración motor ↔ plugin con `pytest -m wp_env`) y mínima (PHP 8.1, WordPress 6.0, sin WooCommerce) |
| `audit` | Ubuntu | `npm audit`, `cargo audit`, `pip-audit`, `composer audit` y `gitleaks` sobre todo el historial |
| `ci-ok` | Ubuntu | Falla si cualquiera de los anteriores no terminó bien. Es el único check requerido en `main` |

Además, `.github/workflows/codeql.yml` ejecuta CodeQL (Actions, JavaScript/TypeScript, Python y Rust) y `.github/dependabot.yml` propone actualizaciones semanales de npm, Cargo, uv, Composer y GitHub Actions.

Para reproducir la CI en local antes de abrir un PR: `npm run format:check`, `npm run lint`, `npm run typecheck`, `npm run test`, `npm run coverage:core` (si cambias el núcleo Rust) y `npm run contracts` (este último no debe cambiar ningún archivo). La plantilla de PR (`.github/pull_request_template.md`) recoge esta lista y las casillas que piden la revisión de `revisor-seguridad`.

## Configuración del repositorio en GitHub

El repositorio es público (ADR 0005). Estos ajustes los aplica la persona dueña del repositorio, una sola vez, justo después de crearlo y antes de aceptar el primer PR. Ningún agente los cambia por su cuenta. En los comandos `gh`, sustituye `OWNER/REPO` por el nombre real del repositorio.

1. **Settings → Actions → General**
   - "Approval for running fork pull request workflows from contributors": elige **Require approval for all external contributors**.
   - "Workflow permissions": **Read repository contents and packages permissions** (solo lectura).
   - Desmarca **Allow GitHub Actions to create and approve pull requests**.

   ```powershell
   gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
   gh api -X PUT repos/OWNER/REPO/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
   ```

2. **Settings → Advanced Security** (gratis en repositorios públicos)
   - Activa **Secret scanning** y **Push protection**.
   - Activa **Dependabot alerts** (y, si quieres, **Dependabot security updates**).
   - **CodeQL**: no actives la "Default setup". El repositorio ya trae la configuración avanzada en `.github/workflows/codeql.yml` y las dos no pueden convivir.

   ```powershell
   gh api -X PATCH repos/OWNER/REPO -f "security_and_analysis[secret_scanning][status]=enabled" -f "security_and_analysis[secret_scanning_push_protection][status]=enabled"
   gh api -X PUT repos/OWNER/REPO/vulnerability-alerts
   ```

3. **Settings → Rules → Rulesets → New branch ruleset** para `main` (rama por defecto), en estado **Active**:
   - **Require a pull request before merging** (0 aprobaciones mientras haya una sola persona en el proyecto).
   - **Require status checks to pass**: agrega el check **`ci-ok`** (aparece después de la primera ejecución de la CI) y marca **Require branches to be up to date before merging**.
   - **Block force pushes** y **Restrict deletions**.

   ```powershell
   $ruleset = @'
   {
     "name": "proteccion-main",
     "target": "branch",
     "enforcement": "active",
     "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
     "rules": [
       { "type": "deletion" },
       { "type": "non_fast_forward" },
       { "type": "pull_request", "parameters": { "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": true, "require_code_owner_review": false, "require_last_push_approval": false, "required_review_thread_resolution": false } },
       { "type": "required_status_checks", "parameters": { "strict_required_status_checks_policy": true, "required_status_checks": [ { "context": "ci-ok", "integration_id": 15368 } ] } }
     ]
   }
   '@
   $ruleset | gh api -X POST repos/OWNER/REPO/rulesets --input -
   ```

   `15368` es el identificador de la app GitHub Actions: así solo cuenta un `ci-ok` publicado por la CI del repositorio.

4. **Nunca registres runners propios (self-hosted)** en este repositorio: un PR desde un fork podría ejecutar código en esa máquina. Settings → Actions → Runners debe quedar vacío.

5. Cuando llegue la fase de release, los secretos de firma y del updater irán solo en un **Environment `release` con aprobación obligatoria**, nunca en secretos del repositorio disponibles para workflows de PR.

Para comprobar la configuración: `gh api repos/OWNER/REPO/actions/permissions/workflow`, `gh api repos/OWNER/REPO/actions/permissions/fork-pr-contributor-approval`, `gh api repos/OWNER/REPO --jq .security_and_analysis` y `gh api repos/OWNER/REPO/rulesets`.

## Licencia

Copyright © 2026 Concersa. Todos los derechos reservados.

El código se publica solo para consulta. No se concede licencia para usarlo, copiarlo, modificarlo ni distribuirlo sin permiso escrito de Concersa. Ver [`LICENSE`](LICENSE).

Excepción: el contenido de `packages/wp-plugin/` se distribuye bajo GPL-2.0-or-later; ver [`packages/wp-plugin/LICENSE`](packages/wp-plugin/LICENSE). El resto del repositorio sigue siendo "Todos los derechos reservados" (ADR 0008).
