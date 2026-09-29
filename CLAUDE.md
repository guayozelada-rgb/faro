# Faro — instrucciones para Claude Code

Faro es una app de escritorio (Windows y macOS) de marketing digital para usuarios de WordPress/WooCommerce.
Agentes de IA investigan, auditan, escriben contenido y optimizan Google Ads; el usuario aprueba.

## Arquitectura en una línea
Interfaz React (ventana Tauri) → núcleo Rust (Tauri 2) → motor Python (sidecar, FastAPI local) → SQLite cifrado.
Nube mínima (FastAPI + Postgres) solo para licencias, actualizaciones y relay de Google Ads.

## Repositorio
- `apps/desktop/src` — interfaz React + TypeScript
- `apps/desktop/src-tauri` — núcleo Rust
- `apps/engine` — motor Python (sidecar)
- `apps/cloud` — nube mínima
- `packages/wp-plugin` — plugin WordPress/WooCommerce (PHP)
- `packages/shared` — tipos generados y esquemas compartidos
- `docs/` — especificaciones y decisiones de arquitectura (ADR)

## Reglas que nunca se rompen
1. Las claves de API y tokens viven solo en el llavero del sistema operativo. Nunca en texto plano, logs, SQLite sin cifrar ni en el frontend.
2. El frontend nunca habla directo con el motor: todo pasa por comandos del núcleo Rust.
3. Nada que publique contenido o gaste dinero se ejecuta sin pasar por las reglas de autonomía y la Bandeja de aprobación.
4. Las campañas de Google Ads se crean siempre en PAUSA.
5. El developer token de Google Ads nunca va dentro de la app.
6. Textos de interfaz en español, tuteo, sin jerga (término técnico solo en tooltip).

## Cómo trabajamos
1. `arquitecto` escribe la especificación en `docs/specs/` antes de programar algo nuevo.
2. El agente de la capa programa siguiendo las skills del proyecto.
3. `qa-pruebas` agrega y ejecuta pruebas.
4. `revisor-seguridad` revisa todo cambio que toque claves, licencias, OAuth, el plugin, el sidecar o la nube.

Antes de programar en una capa, carga la skill correspondiente (ver `.claude/skills/`). Empieza siempre por `faro-arquitectura`.
