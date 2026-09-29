---
name: i18n-es-primero
description: Cómo escribir y traducir los textos de la interfaz de Faro - archivos de mensajes, claves, pluralización, formato de números y fechas, voz y tono en español, y glosario SEO en lenguaje simple. Úsala al agregar o cambiar cualquier texto visible para el usuario.
---

# Textos e i18n (español primero)

## Técnica
- Librería: `i18next` + `react-i18next`. Idioma base `es`; preparados `en` y `pt-BR`.
- Archivos: `apps/desktop/src/locales/<idioma>/<seccion>.json` (`common`, `home`, `onboarding`, `inbox`, `audit`, `research`, `content`, `ads`, `agents`, `settings`, `errors`).
- Claves en inglés, jerárquicas y descriptivas: `audit.issues.emptyState.title`.
- Uso: `const { t } = useTranslation("audit"); t("issues.emptyState.title")`.
- Plurales con `_one` / `_other`: `"pagesAffected_one": "{{count}} página afectada"`.
- Números, monedas y fechas con `Intl` usando el idioma y la zona del usuario; nunca concatenes números en el texto a mano.
- Errores: la interfaz traduce por `code` desde `errors.json`; el `message` del backend es respaldo.
- Prohibido texto en duro en JSX. El lint (`i18next/no-literal-string`) lo detecta.
- Al agregar una clave en `es`, agrégala en `en` y `pt-BR` con el prefijo `[TODO] ` si no tienes la traducción; CI lista los pendientes.

## Voz y tono
- **Tuteo** ("Conecta tu sitio"), cercano y claro, nunca infantil.
- Frases cortas (menos de 20 palabras). Una idea por frase.
- Verbos de acción concretos en botones: "Revisar 12 páginas", "Crear campaña en pausa". Evita "Aceptar", "Enviar", "Ver resultados".
- Explica el beneficio, no la función: "Encuentra sobre qué escribir para atraer compradores".
- Errores: qué pasó + qué hacer. "No pudimos conectar con tu sitio. Revisa que el plugin esté activo e inténtalo de nuevo."
- Sin signos de exclamación salvo en logros reales. Sin culpar al usuario.
- Español neutro latinoamericano: "computadora", "celular", "ingresar", "clave".

## Glosario SEO en lenguaje simple
El término técnico va en tooltip o en modo experto.

| Técnico | En la interfaz |
| --- | --- |
| SERP | Resultados de Google |
| Keyword | Búsqueda / palabra clave |
| Search intent | Qué busca la persona (comprar, informarse, comparar) |
| Crawl | Revisión del sitio |
| 4xx / 404 | Páginas que no existen |
| 5xx | Páginas con error del servidor |
| Noindex | Páginas ocultas para Google |
| Canonical | Página principal de un duplicado |
| Meta description | Descripción en Google |
| Title tag | Título en Google |
| Backlink | Enlace desde otro sitio |
| Keyword cannibalization | Páginas tuyas que compiten entre sí |
| Content cluster | Grupo de temas |
| Pillar page | Página principal del tema |
| CTR | Porcentaje de clics |
| CPC | Costo por clic |
| Negative keyword | Búsqueda excluida |
| Quality Score | Nivel de calidad del anuncio |
| RSA | Anuncio adaptable |

## Checklist
- [ ] Sin textos en duro
- [ ] Clave agregada en `es`, `en`, `pt-BR`
- [ ] Tuteo, verbo de acción, sin jerga
- [ ] Números y fechas con `Intl`
- [ ] Mensajes de error con solución
