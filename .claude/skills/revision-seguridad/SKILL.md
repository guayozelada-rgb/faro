---
name: revision-seguridad
description: Lista de verificación de seguridad de Faro para revisar cambios en secretos, OAuth, permisos de Tauri, sidecar, plugin de WordPress, nube, licencias, dependencias y acciones que publican o gastan dinero. Úsala al revisar cualquier cambio sensible antes de integrarlo.
---

# Revisión de seguridad

Revisa solo las secciones que el cambio toca. Cada hallazgo necesita archivo, línea y un escenario concreto.

## 1. Secretos (claves de API, tokens OAuth, llave de SQLCipher)
- [ ] Se guardan solo en el llavero del SO; nunca en SQLite, archivos de configuración, `localStorage` ni variables de entorno persistentes.
- [ ] Ningún comando Tauri ni endpoint devuelve el secreto; solo `last4` y estado.
- [ ] No aparecen en logs, mensajes de error, reportes de Sentry, trazas de LLM ni fixtures de pruebas.
- [ ] El motor los mantiene solo en memoria durante la tarea.
- [ ] No hay secretos en el repositorio (`git grep -nE "sk-|AIza|ghp_|-----BEGIN"` y `gitleaks` si está disponible).

## 2. Tauri y ventana
- [ ] Los permisos nuevos en `capabilities/` son los mínimos; ninguno de la lista prohibida de `tauri-comandos-y-permisos`.
- [ ] La CSP no se relajó sin ADR; no se cargan URLs ni scripts remotos.
- [ ] Contenido externo (HTML de páginas rastreadas, respuestas de LLM) nunca se renderiza como HTML sin sanitizar (`DOMPurify`) ni con `dangerouslySetInnerHTML` directo.

## 3. Sidecar y API local
- [ ] El motor escucha solo en `127.0.0.1`.
- [ ] Todas las rutas exigen el token, incluida `/health` (ADR 0004); no hay rutas nuevas sin dependencia de autenticación.
- [ ] Validación de cabecera `Host`.
- [ ] `engine_call` mantiene la lista permitida de operaciones.
- [ ] Verificación de integridad del binario sigue activa en release.

## 4. Agentes y acciones con impacto
- [ ] Toda acción que publica contenido o gasta dinero pasa por `autonomy_rules` y crea `approvals` cuando corresponde.
- [ ] Las campañas se crean en `PAUSED`; los cambios de presupuesto respetan el tope diario y el porcentaje máximo.
- [ ] Cada acción externa tiene clave de idempotencia y guarda el valor anterior.
- [ ] **Inyección de prompts**: el contenido de páginas rastreadas, SERP o competidores se trata como datos; no puede cambiar herramientas, reglas de autonomía ni destinos de publicación. Las herramientas del agente validan sus argumentos con Pydantic.
- [ ] El agente no puede ejecutar código arbitrario ni hacer peticiones a URLs arbitrarias fuera de las herramientas definidas.

## 5. Crawler y red
- [ ] Protección SSRF: el crawler no sigue redirecciones ni enlaces a IPs privadas, `localhost`, `169.254.0.0/16` ni al puerto del motor.
- [ ] Límites de tamaño de respuesta, tiempo y número de páginas.
- [ ] TLS verificado siempre.

## 6. OAuth (Google)
- [ ] Flujo para apps instaladas con PKCE y redirección a loopback con puerto aleatorio; `state` verificado.
- [ ] Alcances mínimos necesarios.
- [ ] Refresh tokens en el llavero; revocación funciona.

## 7. Plugin de WordPress
- [ ] Endpoints REST con `permission_callback` que valida la firma HMAC del token de vinculación; nunca `__return_true`.
- [ ] Entradas sanitizadas (`sanitize_text_field`, `wp_kses_post`) y salidas escapadas (`esc_html`, `esc_attr`).
- [ ] Nonces en acciones de administración; `current_user_can` en pantallas.
- [ ] El código de vinculación es de un solo uso y caduca en 10 minutos.

## 8. Nube y licencias
- [ ] Webhooks de pagos verifican la firma del proveedor.
- [ ] La llave privada de firma de licencias vive en un gestor de secretos, nunca en el repositorio ni en la app.
- [ ] Límite de peticiones en activación y relay; errores sin filtrar información interna.
- [ ] El relay de Google Ads valida la licencia y no almacena datos de campañas.

## 9. Dependencias y build
- [ ] `cargo audit`, `npm audit --omit=dev`, `pip-audit` sin vulnerabilidades altas o críticas nuevas.
- [ ] Dependencias nuevas justificadas, mantenidas y con licencia compatible.
- [ ] Artefactos firmados; secretos de CI no expuestos en logs.

## 10. CI en repositorio público (ADR 0005)
- [ ] Ningún workflow usa `pull_request_target` ni `workflow_run` para ejecutar código de un PR.
- [ ] Sin runners propios (self-hosted): un PR de un fork podría ejecutar código en esa máquina.
- [ ] `permissions: contents: read` por defecto; permisos extra solo en el trabajo que los necesita.
- [ ] Acciones de terceros fijadas por SHA completo.
- [ ] Secretos de firma y updater solo en un Environment protegido con aprobación, nunca disponibles en trabajos de PR.
- [ ] Nada en el repositorio que dé acceso: sin `.env*` reales, sin datos de usuarios, sin URLs internas de la nube con credenciales.

## Severidad
- **Crítico**: fuga de secretos, ejecución remota, acción de dinero o publicación sin aprobación, bypass de licencia en servidor.
- **Alto**: SSRF, XSS en la ventana, permisos Tauri excesivos, ruta del motor sin token.
- **Medio**: validación incompleta, logs con datos sensibles no secretos, dependencias vulnerables sin explotación clara.
- **Bajo**: endurecimiento recomendado.
