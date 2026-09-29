# ADR 0007 — Paleta de grises neutros y turquesa

- **Fecha:** 2026-09-29
- **Estado:** aceptado
- **Decidido por:** el usuario, tras la verificación manual de F0 (2026-09-29)
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md) (§3, §4.2)
- **Tabla de tokens definitiva:** [`.claude/skills/sistema-diseno-faro/SKILL.md`](../../.claude/skills/sistema-diseno-faro/SKILL.md), sección "Tokens"

## Contexto

F0 se construyó con la paleta inicial de `sistema-diseno-faro`: grises con tinte azulado, un color de acción y **violeta** como color exclusivo de lo que hace la IA, sin un límite de oscuridad para fondos, textos y bordes.

Al hacer la verificación manual de F0 en Windows 11, el usuario revisó la app en modo claro y oscuro y pidió cambiar la paleta: quiere una herramienta sobria, sin tinte azulado en los grises, sin morado ni violeta y sin zonas muy oscuras. Como los colores los usan todas las pantallas actuales y futuras, la decisión afecta a todas las funcionalidades y se registra aquí.

## Decisión

1. **Grises neutros** (sin tinte azulado) para fondos, superficies, bordes y textos.
2. **Turquesa** es el **único color de acción**: botones principales, enlaces, foco y sección activa (`primary`: claro `#0F766E`, oscuro `#2DD4BF`).
3. **Sin morado ni violeta** en ninguna parte de la interfaz.
4. El color exclusivo de la IA (`ai`) pasa de violeta a **azul claro** (claro `#1D4ED8`, oscuro `#93C5FD`). Sigue reservado a lo generado o propuesto por agentes.
5. **Gris más oscuro permitido: 80 % (`#333333`)** en todo: fondos, textos y bordes. Prohibido el negro y cualquier gris por debajo de `#333333`.
6. **Modo oscuro:** fondo de ventana `#333333`; texto principal blanco `#FFFFFF`; texto secundario gris 15 % `#D9D9D9` como máximo de oscuridad.
7. **Modo claro:** fondos blancos y grises muy claros; texto principal `#333333`, secundario `#595959`.
8. Se mantienen **modo claro y modo oscuro** (según el sistema), ambos con estas reglas.
9. `success`, `warning` y `critical` se mantienen (verde, ámbar, rojo): son señales de estado y siempre van acompañadas de icono o texto.
10. La tabla completa de tokens (valores claro y oscuro) vive **solo** en la skill `sistema-diseno-faro`; este ADR no la duplica. Los componentes usan siempre tokens, nunca hex.

## Consecuencias

- **Contraste verificado por prueba automática**: `tokens.test.ts` (en `apps/desktop`, por crear) comprueba ≥ 4,5:1 para texto sobre `background`, `surface`, `card` y `muted`, y ≥ 3:1 para bordes de controles y foco, en ambos modos. Si la prueba y la tabla discrepan, manda la prueba.
- **Valores ajustables solo para cumplir contraste**, y siempre dentro de las reglas de este ADR (nada más oscuro que `#333333`, sin violeta, turquesa solo para acción, azul claro solo para IA). Un ajuste así actualiza la skill, no requiere ADR nuevo.
- **Cualquier color nuevo** (un tono fuera de la tabla, un segundo color de acción, otro color de estado) **requiere un ADR nuevo**.
- **Superficies separadas por luminosidad y bordes**: como no hay grises más oscuros que `#333333`, en modo oscuro los planos se distinguen porque las superficies son algo más claras que el fondo (`surface`, `card`, `muted`) y por bordes de 1 px; no con sombras fuertes ni fondos más oscuros.
- En modo oscuro, el texto sobre `primary`, `ai` y `critical` va en `#333333` (sus `*-foreground`), no en negro.
- La paleta cambió **después** de implementar F0: a fecha de este ADR, `apps/desktop/src/styles/tokens.css` todavía tiene la paleta anterior (por ejemplo `--faro-ai: #7c3aed`, violeta) y `tokens.test.ts` aún no existe. Aplicar los tokens de la skill en `tokens.css` y crear `tokens.test.ts` es trabajo de `frontend-react` y `qa-pruebas`. Las especificaciones anteriores que hablen de colores se leen con este ADR (ver nota en §3 y §4.2 de la spec de F0).
- `revisor-seguridad` no interviene (no toca claves, permisos ni red); los cambios de paleta los revisa `qa-pruebas` con `tokens.test.ts` y una comprobación visual en ambos modos.
