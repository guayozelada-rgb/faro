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
│  └─ wp-plugin/        # Plugin de WordPress (fase posterior)
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
| `npm run setup` | `npm ci` y `uv sync --directory apps/engine --locked` (dependencias exactas de los lockfiles) |
| `npm run dev` | Abre la app en modo desarrollo (`tauri dev`); el núcleo lanza y supervisa el motor desde `apps/engine/.venv` |
| `npm run dev:engine` | Arranca solo el motor en modo externo (`--dev`), para usarlo con `FARO_ENGINE_DEV_URL` |
| `npm run lint` | Ejecuta `lint:ts`, `lint:rs` y `lint:py` |
| `npm run lint:ts` | ESLint de la interfaz (`apps/desktop`) |
| `npm run lint:rs` | `cargo clippy --all-targets -- -D warnings` del núcleo (`apps/desktop/src-tauri`) |
| `npm run lint:py` | `ruff check` del motor (`apps/engine`) |
| `npm run format` | Formatea todo: Prettier (TS, JSON, Markdown, CSS), `cargo fmt` (Rust) y `ruff format` (Python) |
| `npm run format:check` | Igual que `format`, pero solo comprueba y falla si algún archivo no tiene el formato correcto |
| `npm run typecheck` | `tsc --noEmit` de los paquetes TypeScript y `mypy` del motor |
| `npm run test` | Ejecuta `test:desktop`, `test:core` y `test:engine` |
| `npm run test:desktop` | Pruebas de la interfaz con Vitest |
| `npm run test:core` | Pruebas del núcleo con `cargo test` |
| `npm run test:engine` | Pruebas del motor con pytest (`uv run --directory apps/engine pytest`) |
| `npm run test:all` | En F0 es igual que `test`; desde F1 incluye las pruebas extremo a extremo |
| `npm run contracts` | Regenera los contratos del motor en `packages/shared` (`openapi.json`, `engine.d.ts`, `engine-operations.json`) |

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

## Integración continua

Cada pull request hacia `main`, cada push a `main` y cada ejecución manual lanzan `.github/workflows/ci.yml`:

| Trabajo | Runner | Qué comprueba |
| --- | --- | --- |
| `ts` | Ubuntu | Prettier, ESLint, `tsc` y Vitest con cobertura (70 %) |
| `engine` | Ubuntu y Windows | `ruff format --check`, `ruff check`, `mypy --strict` y pytest con cobertura (80 %; 95 % en seguridad y protocolo) |
| `core` | Windows | Build de la interfaz, `cargo fmt --check`, `clippy -D warnings`, `cargo test` y el arranque real del motor (`engine_real`) |
| `contracts` | Ubuntu | `npm run contracts` no deja diferencias en `packages/shared` |
| `audit` | Ubuntu | `npm audit`, `cargo audit`, `pip-audit` y `gitleaks` sobre todo el historial |
| `ci-ok` | Ubuntu | Falla si cualquiera de los anteriores no terminó bien. Es el único check requerido en `main` |

Además, `.github/workflows/codeql.yml` ejecuta CodeQL (Actions, JavaScript/TypeScript, Python y Rust) y `.github/dependabot.yml` propone actualizaciones semanales de npm, Cargo, uv y GitHub Actions.

Para reproducir la CI en local antes de abrir un PR: `npm run format:check`, `npm run lint`, `npm run typecheck`, `npm run test` y `npm run contracts` (este último no debe cambiar ningún archivo).

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
