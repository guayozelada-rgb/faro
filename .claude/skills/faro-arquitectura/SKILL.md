---
name: faro-arquitectura
description: Mapa del repositorio de Faro, cómo se comunican interfaz, núcleo Rust, motor Python, nube y plugin, y convenciones de nombres, errores y logs. Cárgala antes de cualquier tarea de código o diseño en Faro.
---

# Arquitectura de Faro

Faro es una app de escritorio (Windows y macOS) de SEO y Google Ads para usuarios de WordPress/WooCommerce. Los agentes de IA trabajan en la computadora del usuario; el usuario aprueba lo que publica o gasta dinero.

## Capas y quién habla con quién

```
Interfaz React ──invoke()──▶ Núcleo Rust (Tauri 2) ──HTTP localhost + token──▶ Motor Python (sidecar)
                                   │                                              │
                                   ├─ llavero del SO (secretos)                   ├─ SQLite cifrado (SQLCipher + sqlite-vec)
                                   ├─ licencias Ed25519                           ├─ LLMs, SerpAPI, GSC, GA4
                                   └─ updater firmado                             ├─ Sitio WordPress (plugin)
                                                                                  └─ Nube Faro (relay Google Ads)
Nube mínima (FastAPI + Postgres): licencias, relay de Google Ads, manifiesto de actualizaciones.
```

Reglas de comunicación:
- La interfaz **solo** llama comandos Tauri. Nunca abre sockets ni hace `fetch` a localhost.
- El núcleo es el único que conoce el puerto y el token del motor.
- El motor pide secretos al núcleo cuando va a usarlos (ver skill `tauri-sidecar-python`); no los guarda en disco.
- Google Ads siempre pasa por el relay de la nube; el developer token nunca está en la app.

## Repositorio

| Ruta | Contenido | Lenguaje | Agente dueño |
| --- | --- | --- | --- |
| `apps/desktop/src` | Interfaz | React + TS | frontend-react |
| `apps/desktop/src-tauri` | Núcleo | Rust | tauri-rust |
| `apps/engine/faro_engine/core` | Servidor local, cola, programador, BD | Python 3.12 | motor-python |
| `apps/engine/faro_engine/agents` | Agentes del producto | Python | ingeniero-ia |
| `apps/engine/faro_engine/seo` | Crawler, SERP, clustering | Python | seo-datos |
| `apps/engine/faro_engine/ads` | Google Ads | Python | google-ads |
| `apps/cloud` | Licencias, relay, updates | Python (FastAPI) | nube-backend |
| `packages/wp-plugin` | Plugin WordPress | PHP 8.1+ | wordpress-php |
| `packages/shared` | Tipos generados desde OpenAPI | TS | frontend-react |
| `docs/specs`, `docs/adr` | Especificaciones y decisiones | Markdown | arquitecto |
| `.github/`, `scripts/` | CI/CD y build | YAML, shell | devops-release |

## Convenciones

**Nombres**
- Rust: `snake_case`; comandos Tauri con prefijo de dominio: `vault_add_key`, `license_activate`, `engine_call`.
- Python: módulos `snake_case`, rutas del motor en plural y en inglés: `/sites`, `/crawls/{id}/issues`.
- TypeScript: componentes `PascalCase`, hooks `useAlgo`, archivos de componentes `NombreComponente.tsx`.
- Base de datos: tablas en plural `snake_case`, claves primarias `id` (UUID v7 como texto), fechas `*_at` en UTC ISO-8601.
- Código y nombres técnicos en inglés; textos de usuario en español vía i18n.

**Errores** (mismo formato en todas las capas)
```json
{ "code": "vault.invalid_key", "message": "Esta clave no es válida o no tiene saldo.", "details": {} }
```
- `code`: `dominio.motivo`, estable, se usa en la interfaz para decidir qué mostrar.
- `message`: español, para el usuario, sin jerga ni códigos HTTP.
- Nunca incluyas secretos, rutas internas ni trazas en `message` o `details`.

**Logs**
- Rust `tracing`, Python `structlog`, ambos en JSON con `request_id` y `run_id` de agente cuando exista.
- Prohibido registrar: claves, tokens, cabeceras `Authorization`, cuerpos de peticiones a proveedores.

**Tiempo y dinero**
- Fechas siempre UTC en almacenamiento; la interfaz convierte a la zona del usuario.
- Dinero en unidades menores enteras (micros para Google Ads) más código de moneda. Nunca `float`.

## Reglas de producto que afectan al código
1. Toda acción que publica o gasta pasa por `autonomy_rules` y, si corresponde, crea un registro en `approvals`.
2. Campañas de Google Ads nuevas: siempre `PAUSED`.
3. Cada acción externa lleva una clave de idempotencia y guarda el valor anterior para poder deshacer.
4. Cada llamada a LLM o SerpAPI registra tokens/créditos y costo estimado en `agent_steps`.

## Documentación
- Funcionalidad nueva → especificación en `docs/specs/` (agente `arquitecto`).
- Decisión que afecta a varias funcionalidades → ADR en `docs/adr/`.
- El plan de producto completo está en el documento "Plan de producto: App de Marketing Digital con Agentes IA".
