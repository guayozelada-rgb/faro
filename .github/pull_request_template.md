## Qué cambia

<!-- Resume el cambio y por qué. Enlaza la spec (docs/specs/), el ADR y la tarea (por ejemplo, F1a T10). -->

## Cómo se probó

<!-- Comandos ejecutados y resultado, pruebas nuevas y, si aplica, la verificación manual. -->

- [ ] `npm run format:check`
- [ ] `npm run lint`
- [ ] `npm run typecheck`
- [ ] `npm run test`
- [ ] `npm run coverage:core` si cambia el núcleo Rust (80 % global y 95 % en `vault/`, `secrets/`, `profile/` y `engine/protocol.rs`)
- [ ] `npm run contracts` ejecutado si cambian endpoints del motor (y los cambios de `packages/shared` incluidos en este PR)

## Capas que toca

- [ ] Interfaz (`apps/desktop/src`)
- [ ] Núcleo Rust (`apps/desktop/src-tauri`)
- [ ] Motor Python (`apps/engine`)
- [ ] Nube (`apps/cloud`)
- [ ] Plugin de WordPress (`packages/wp-plugin`)
- [ ] Contratos y tipos compartidos (`packages/shared`)
- [ ] CI, scripts o build (`.github/`, `scripts/`, configuración de Tauri)
- [ ] Documentación (`docs/`, `README.md`)

## Revisión de seguridad

Marca lo que aplique. Cualquier casilla marcada requiere la revisión de `revisor-seguridad` antes de integrar.

- [ ] **¿Cambia `secrets` en `packages/shared/engine-operations.json`?** → requiere revisión de `revisor-seguridad` (obligatoria, spec F1a §4.5).
- [ ] Secretos, llavero del sistema, llave de la base cifrada o logs que podrían contener valores sensibles → `revisor-seguridad`.
- [ ] Plugin de WordPress (vinculación, firma, rutas REST, permisos) → `revisor-seguridad`.
- [ ] Sidecar o protocolo núcleo ↔ motor (argumentos de arranque, stdin/stdout, `--allow-local-sites`) → `revisor-seguridad`.
- [ ] Workflows de CI, release o firma (`.github/workflows/`) → `revisor-seguridad`.
- [ ] Licencias, OAuth, actualizaciones o nube → `revisor-seguridad`.

## Comprobaciones generales

- [ ] Sin claves, tokens ni datos reales en el código, las pruebas, los logs ni este PR.
- [ ] Textos de interfaz en español, con tuteo y sin jerga; las claves nuevas en `en` y `pt-BR` llevan `[TODO] `.
- [ ] Nada publica contenido ni gasta dinero sin pasar por las reglas de autonomía y la Bandeja de aprobación.
- [ ] Spec o ADR actualizados si cambia una decisión.
