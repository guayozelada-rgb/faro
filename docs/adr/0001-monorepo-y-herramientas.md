# ADR 0001 — Estructura del monorepo y herramientas por lenguaje

- **Fecha:** 2026-09-28
- **Estado:** aceptado
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md)

## Contexto

Faro combina TypeScript (interfaz y tipos compartidos), Rust (núcleo Tauri) y Python (motor y, más adelante, nube), más PHP (plugin) en fases posteriores. Hay que elegir gestor de paquetes JS, cómo se organizan los proyectos Python y qué herramientas de lint, formato, tipos y pruebas usa cada lenguaje, porque todos los agentes y el CI dependen de ello. Las skills del proyecto ya usan comandos `npm run ...` y `npm run test -w apps/desktop`.

## Decisión

1. **Gestor JS: npm workspaces** (`apps/desktop`, `packages/shared`), con `package-lock.json` y versión de Node fijada en `.node-version`.
   - Coincide con los comandos que ya usan las skills (`npm run contracts`, `-w apps/desktop`).
   - Solo hay dos paquetes JS; las ventajas de pnpm (ahorro de disco, dependencias estrictas) pesan poco aquí.
   - Una herramienta menos que instalar en Windows y en CI; Tauri y shadcn/ui lo soportan de primera clase.
2. **Python: un proyecto uv independiente por desplegable** (`apps/engine` ahora, `apps/cloud` después), cada uno con su `pyproject.toml`, `uv.lock` y `.python-version` (3.12). No hay workspace uv en la raíz: el motor se empaqueta con PyInstaller desde su lock exacto y la nube se despliega aparte, así que no deben compartir resolución de dependencias.
3. **Paquete del motor: `apps/engine/faro_engine/`** con subpaquetes `core/` (servidor, seguridad, rutas, esquemas) y, en fases futuras, `agents/`, `seo/`, `ads/`. Las skills `faro-arquitectura` y `contratos-api-local` se actualizan para usar `apps/engine/faro_engine/<subpaquete>/...` (decisión del usuario del 2026-09-28; lo hace el agente principal). Evita un paquete de primer nivel llamado `core` (nombre genérico que choca con otros) y respeta el punto de entrada `faro_engine/__main__.py` de `tauri-sidecar-python`. Pruebas en `apps/engine/tests/`.
4. **Rust: un solo crate** en `apps/desktop/src-tauri` con su `Cargo.lock`; toolchain fijada en `rust-toolchain.toml` (estable MSVC, versión exacta, con `clippy` y `rustfmt`).
5. **Herramientas:**

| Lenguaje | Lint | Formato | Tipos | Pruebas |
| --- | --- | --- | --- | --- |
| TypeScript | ESLint 9 + typescript-eslint + react-hooks + jsx-a11y + eslint-plugin-i18next | Prettier 3 (+ plugin de Tailwind) | `tsc --noEmit` estricto | Vitest + Testing Library |
| Rust | clippy `-D warnings` | rustfmt | compilador | `cargo test` |
| Python | ruff | ruff format | mypy `--strict` (plugin pydantic) | pytest (+ asyncio, cov, httpx) |

   Se prefiere **ESLint + Prettier sobre Biome** porque la regla `i18next/no-literal-string` (exigida por `i18n-es-primero`) y las reglas de accesibilidad `jsx-a11y` no existen en Biome. Se prefiere **mypy sobre pyright** para no depender de Node en el entorno Python y por su plugin oficial de Pydantic.
6. **Scripts de la raíz** como interfaz única para agentes y CI: `setup`, `dev`, `dev:engine`, `lint`, `format`, `format:check`, `typecheck`, `test`, `test:desktop`, `test:core`, `test:engine`, `test:all`, `contracts`.
7. Finales de línea LF en todo el repositorio (`.gitattributes`), `.editorconfig` común.

## Consecuencias

- Todo agente ejecuta verificaciones con los mismos scripts npm, también para Rust y Python.
- El comando `uv run pytest apps/engine` de `pruebas-faro` se sustituye por `npm run test:engine` (o `uv run --directory apps/engine pytest`); el agente principal actualiza la skill.
- El identificador de la app es `app.faro.desktop` y es definitivo: no se cambia nunca (nombre del servicio en el llavero y carpeta de datos; ver ADR 0003).
- Añadir `apps/cloud` en el futuro = nuevo proyecto uv y un nuevo script `test:cloud`, sin tocar el motor.
- Cambiar a pnpm más adelante es posible sin rediseño (solo lockfile y scripts), pero requeriría ADR.
