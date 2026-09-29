# ADR 0005 — Repositorio público en GitHub y CI

- **Fecha:** 2026-09-28
- **Estado:** aceptado
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md)

## Contexto

Faro necesita un repositorio en GitHub con CI que valide TS, Rust y Python, con trabajos en Windows porque la app se ejecuta allí. El plan es gratuito. Hay que elegir entre repositorio público o privado. Comparación (límites a verificar al configurar):

| Aspecto | Público | Privado (plan gratuito) |
| --- | --- | --- |
| Minutos de Actions | Ilimitados y gratis en runners estándar, incluido Windows. Límites: ≈20 trabajos simultáneos (≈5 de macOS) y 6 h por trabajo. | 2.000 min/mes; Windows cuenta ×2 y macOS ×10. Con la CI de F0 (≈25–40 min contados por ejecución completa) alcanza para pocas decenas de PR al mes; habría que recortar trabajos de Windows. |
| Licencia y copia | El código es visible. Sin licencia open source sigue siendo "Todos los derechos reservados" (nadie puede reutilizarlo legalmente), pero cualquiera puede leerlo y compilarlo, incluso quitando la verificación de licencias del cliente. | El código no es visible fuera del equipo. |
| Secretos filtrados | Un secreto subido por error queda expuesto al instante y los bots lo recolectan en minutos; hay que revocarlo aunque se borre del historial. Secret scanning y push protection son gratis. | La exposición es menor, pero un secreto filtrado igual debe revocarse. Secret scanning con push protection no está incluido en el plan gratuito. |
| CI con forks | Cualquiera puede abrir un PR desde un fork y hacer que se ejecuten workflows: riesgo alto con `pull_request_target` y con runners propios. Mitigable con aprobación obligatoria y permisos de solo lectura. | No hay PR de forks externos. |
| Protección de rama | Disponible gratis (PR obligatorio, checks requeridos). | En el plan gratuito, las reglas de protección de ramas privadas no están disponibles (requieren plan de pago). |
| Exposición de estrategia | Specs, ADR, prompts de agentes y estrategia SEO/Ads quedan públicos; un competidor puede ver la hoja de ruta. | Privados. |

## Decisión

1. **Repositorio público** en GitHub, plan gratuito (decisión del usuario, 2026-09-28).
2. **Sin licencia open source.** Archivo `LICENSE` con el aviso "Copyright © 2026 Concersa. Todos los derechos reservados.": el código es visible pero no reutilizable. Lo crea T1.
3. **CI completa en cada PR hacia `main` y en cada push a `main`**, incluidos los trabajos de Windows (`core` y la parte Windows de `engine`), más `workflow_dispatch`. Se mantienen `concurrency` con `cancel-in-progress`, cachés (npm, uv, Rust guardada solo desde `main`), `timeout-minutes` en cada trabajo y un trabajo agregador **`ci-ok`** como único check requerido. Sin filtros por rutas: con minutos ilimitados no compensan el riesgo de saltar trabajos por error.
4. **Seguridad obligatoria por ser público:**
   1. **Nunca runners propios (self-hosted)**: un PR de un fork podría ejecutar código arbitrario en esa máquina.
   2. **Nunca `pull_request_target`** (ni `workflow_run` que ejecute código del PR).
   3. Settings → Actions: aprobación obligatoria para workflows de PR de forks de **todos los colaboradores externos**; `GITHUB_TOKEN` en solo lectura por defecto; `permissions: contents: read` en los workflows (solo CodeQL añade `security-events: write`).
   4. Secret scanning + push protection, Dependabot alerts (y `dependabot.yml`) y CodeQL activados.
   5. Protección de `main`: PR obligatorio, `ci-ok` requerido, sin force-push ni borrado.
   6. Secretos futuros de firma y updater solo en un **Environment protegido con aprobación manual**, nunca disponibles en workflows de PR.
   7. `gitleaks` en CI sobre todo el historial.
   8. `.env.local` en `.gitignore` desde el primer commit; ningún secreto real en el repositorio.
   La configuración del repositorio la aplica el usuario, o aprueba los comandos `gh` que prepara `devops-release` (T10); los agentes no cambian la configuración de seguridad del repositorio por su cuenta.

## Consecuencias

- La CI no tiene coste y puede validar Windows en cada cambio; el único límite práctico es la concurrencia y el tiempo de cada ejecución.
- **Aceptado:** la verificación de licencias de la app se puede eliminar compilando desde el código. La protección real de los ingresos está en el lado servidor: relay de Google Ads, actualizaciones y licencias en la nube. El diseño de la nube (fases futuras) debe asumir que el cliente no es de confianza.
- Specs, ADR, definiciones de agentes y skills son públicos. No se escriben en el repositorio datos de clientes, claves, ni información comercial sensible (precios negociados, métricas reales de clientes).
- Cualquier secreto que llegue a un commit se considera comprometido: se revoca y se rota, no basta con reescribir el historial.
- Los PR de personas externas requieren que el usuario apruebe manualmente la ejecución de la CI.
- El titular del copyright es Concersa.

## Cómo ampliar en el futuro

- **Runners más grandes de GitHub** (más CPU/RAM, compilaciones de Rust más rápidas): son de pago y requieren una organización con plan Team o Enterprise.
- **Más concurrencia**: subir de plan.
- **Pasar el repositorio a privado**: entonces aplican los 2.000 min/mes con Windows ×2 (habría que limitar los trabajos de Windows a PR hacia `main`) y se pierde la protección de rama gratuita. El código ya publicado sigue existiendo en forks y cachés de terceros; hacerlo privado no lo retira.
- **Runner propio (self-hosted)**: solo si el repositorio pasa a privado; nunca mientras sea público.
- Cualquiera de estos cambios requiere un ADR nuevo.
