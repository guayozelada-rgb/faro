---
name: release-y-firma
description: Cómo se compila, firma, notariza y publica Faro para Windows y macOS con GitHub Actions y tauri-action, incluido el empaquetado del sidecar, las firmas del updater, el versionado y las notas de versión. Úsala al tocar workflows de CI/CD, scripts de build o al publicar una versión.
---

# Release y firma

## Workflows (`.github/workflows/`)
| Archivo | Cuándo | Qué hace |
| --- | --- | --- |
| `ci.yml` | Cada pull request | Lint, typecheck, pruebas de todas las capas, contratos actualizados, auditoría de dependencias |
| `release.yml` | Tag `v*.*.*` | Build en matriz, firma, notarización, updater, borrador de GitHub Release |
| `nightly.yml` | Diario | Pruebas lentas y extremo a extremo |

Acciones de terceros fijadas por SHA completo, no por etiqueta.

## Matriz de build
| Runner | Target | Instalador |
| --- | --- | --- |
| `windows-latest` | `x86_64-pc-windows-msvc` | NSIS `.exe` (y `.msi` opcional) |
| `macos-latest` | `aarch64-apple-darwin` | `.dmg` |
| `macos-13` | `x86_64-apple-darwin` | `.dmg` |

Pasos por plataforma:
1. Instalar Rust, Node y `uv` con versiones fijas.
2. `scripts/build-engine` → PyInstaller → copiar a `src-tauri/binaries/faro-engine-<target>` (ver `tauri-sidecar-python`).
3. Calcular el SHA-256 del sidecar y pasarlo al build de Rust (`FARO_ENGINE_SHA256`) para la verificación de integridad.
4. `tauri-apps/tauri-action` construye, firma y sube los artefactos al borrador del release.

## Firma en Windows
- Opción recomendada: **Azure Trusted Signing**, configurado con `bundle.windows.signCommand` en `tauri.conf.json` para firmar app, instalador y sidecar.
- Alternativa: certificado OV/EV en HSM o servicio en la nube.
- Secretos: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` (y los datos de la cuenta de firma).

## Firma y notarización en macOS
- Certificado **Developer ID Application**; el sidecar también se firma con hardened runtime.
- Entitlements mínimos en `src-tauri/entitlements.plist` (red saliente; Python puede requerir `com.apple.security.cs.allow-unsigned-executable-memory`: documentar cualquier entitlement extra en ADR).
- Secretos: `APPLE_CERTIFICATE` (p12 en base64), `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, y para notarizar `APPLE_API_ISSUER`, `APPLE_API_KEY`, `APPLE_API_KEY_PATH` (o `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`).
- Verificar después del build: `codesign --verify --deep --strict` y `spctl -a -vv`.
- La carpeta `--onedir` del motor lleva librerías nativas (`.so`/`.dylib` de `sqlcipher3`, `tokenizers`, `tiktoken`, `orjson`, `pydantic_core`… y, cuando se use, `vec0.dylib` de `sqlite-vec`): **todas** se firman con la misma Developer ID y hardened runtime. `sqlite-vec` se carga con `load_extension`, así que con la validación de librerías del hardened runtime solo carga si `vec0.dylib` está firmada por el mismo equipo; no añadas `com.apple.security.cs.disable-library-validation` sin ADR (F1b T2).

## Updater
- Plugin `tauri-plugin-updater` con `createUpdaterArtifacts: true`.
- Par de llaves generado una vez con `npm run tauri signer generate`; la pública va en `tauri.conf.json` (`plugins.updater.pubkey`), la privada solo en secretos de CI: `TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.
- El manifiesto `latest.json` se publica en la nube de Faro (endpoint `/updates/{target}/{arch}/{current_version}`), que puede responder distinto según canal (estable/beta) y licencia vigente de actualizaciones.
- La llave privada del updater **nunca** se rota sin plan: perderla impide actualizar a todos los usuarios. Guarda una copia cifrada fuera de CI.
- El repositorio es público (ADR 0005): todos los secretos de firma, notarización y updater van en un Environment `release` con aprobación obligatoria, y el workflow de release solo corre en tags de `main` o `workflow_dispatch`, nunca en PR. Sin runners propios.

## Versionado y notas
- SemVer. La versión vive en `apps/desktop/src-tauri/tauri.conf.json` y se sincroniza con `package.json`, `Cargo.toml` y el motor mediante `scripts/bump-version`.
- Commits con Conventional Commits; notas de versión generadas y editadas en español, orientadas al usuario ("Ahora puedes…"), no a commits.
- Migraciones de base de datos: un release nunca elimina columnas usadas por la versión anterior (permite volver atrás).

## Checklist de release
- [ ] CI en verde en `main`
- [ ] Versión actualizada en todos los manifiestos
- [ ] Build de las tres plataformas firmado (y notarizado en macOS)
- [ ] Instalación limpia y actualización desde la versión anterior probadas en Windows y macOS
- [ ] `latest.json` firmado publicado primero en canal beta
- [ ] Notas de versión en español
- [ ] `revisor-seguridad` aprobó cambios en workflows o firma
